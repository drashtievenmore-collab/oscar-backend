"""
Authentication and identity (api.md §2) and Administration (api.md §3).
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.audit import record_audit, request_ip
from apps.core.exceptions import (
    Codes,
    Conflict,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.pagination import envelope
from apps.core.permissions import AllowPublic, HasModulePermission
from apps.core.throttling import LoginThrottle
from apps.core.viewsets import TenantModelViewSet
from apps.core.tenancy import set_current_client_id

from .authentication import build_tokens
from .models import (
    Client,
    PasswordResetToken,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
    UserSession,
)
from .permission_catalogue import grouped_catalogue
from .serializers import (
    ChangePasswordSerializer,
    ClientSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    MeUserSerializer,
    PermissionSerializer,
    RefreshSerializer,
    ResetPasswordSerializer,
    RoleSerializer,
    TenantSerializer,
    UpdateMeSerializer,
    UserPermissionOverrideSerializer,
    UserSerializer,
    UserSessionSerializer,
)

MAX_FAILED_LOGINS = 8
LOCKOUT = timedelta(minutes=15)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _me_payload(user, request=None):
    """The ``/auth/me/`` body (api.md §2)."""
    return {
        "user": MeUserSerializer(user).data,
        "permissions": sorted(user.effective_permissions()),
        "tenant": TenantSerializer(user.client).data,
    }


# ---------------------------------------------------------------------------
# api.md §2 -- Authentication
# ---------------------------------------------------------------------------
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        password = serializer.validated_data["password"]

        candidates = list(
            User.objects.filter(email=email, deleted_at__isnull=True).select_related(
                "client", "role"
            )
        )
        # A tenant slug disambiguates when the same address exists in more than
        # one tenant. Without it, an ambiguous address is treated as a failure
        # rather than guessing which tenant the caller meant.
        slug = (request.data.get("tenant") or request.data.get("clientSlug") or "").strip()
        if slug:
            candidates = [u for u in candidates if u.client.slug == slug]

        invalid = NotAuthenticated(
            "Incorrect email or password.", code="INVALID_CREDENTIALS"
        )

        if len(candidates) != 1:
            raise invalid

        user = candidates[0]

        if user.is_locked():
            raise NotAuthenticated(
                "This account is temporarily locked. Try again shortly.",
                code="ACCOUNT_LOCKED",
            )
        if user.status != "Active":
            raise NotAuthenticated(
                "This account is not active. Contact your administrator.",
                code="ACCOUNT_INACTIVE",
            )
        if user.client.status != "Active":
            raise NotAuthenticated(
                "This workspace is not active.", code="TENANT_INACTIVE"
            )

        if not user.check_password(password):
            user.failed_login_count += 1
            fields = ["failed_login_count"]
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = timezone.now() + LOCKOUT
                fields.append("locked_until")
            user.save(update_fields=fields)
            raise invalid

        set_current_client_id(user.client_id)
        with transaction.atomic():
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = timezone.now()
            user.save(update_fields=["failed_login_count", "locked_until", "last_login_at"])

            refresh = RefreshToken.for_user(user)
            session = UserSession.objects.create(
                user=user,
                client=user.client,
                refresh_token_hash=_hash(str(refresh)),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                ip=request_ip(request),
                device_label=_device_label(request),
                expires_at=timezone.now() + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"],
            )
            tokens = build_tokens(user, session=session)
            # The stored hash must match the token actually handed out.
            session.refresh_token_hash = _hash(tokens["refresh"])
            session.save(update_fields=["refresh_token_hash"])

            record_audit(
                client=user.client_id,
                actor=user,
                action="login",
                entity_type="User",
                entity_id=user.id,
                entity_label=user.email,
                description="Signed in",
                ip=request_ip(request),
            )

        return Response({**tokens, **_me_payload(user, request)})


def _device_label(request):
    agent = request.META.get("HTTP_USER_AGENT", "")
    for needle, label in (
        ("Edg/", "Edge"),
        ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"),
        ("Safari/", "Safari"),
    ):
        if needle in agent:
            platform = "Windows" if "Windows" in agent else (
                "macOS" if "Mac OS" in agent else ("Android" if "Android" in agent else "")
            )
            return f"{label}{' on ' + platform if platform else ''}"
    return "Unknown device"


class RefreshView(APIView):
    """Single-flight silent refresh (api-integration.md §5.2)."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw = serializer.validated_data["refresh"]

        try:
            token = RefreshToken(raw)
        except TokenError as exc:
            raise NotAuthenticated(
                "Your session has expired. Please sign in again.",
                code="TOKEN_INVALID",
                detail=str(exc),
            ) from exc

        session = UserSession.objects.filter(refresh_token_hash=_hash(raw)).first()
        if session is None or not session.is_active:
            raise NotAuthenticated(
                "Your session has been signed out.", code="TOKEN_REVOKED"
            )

        user = User.objects.select_related("client", "role").filter(
            pk=token.get("user_id"), deleted_at__isnull=True
        ).first()
        if user is None or user.status != "Active":
            raise NotAuthenticated("This account is not active.", code="ACCOUNT_INACTIVE")

        set_current_client_id(user.client_id)
        session.last_seen_at = timezone.now()
        session.save(update_fields=["last_seen_at"])
        return Response({"access": build_tokens(user, session=session)["access"]})


class LogoutView(APIView):
    def post(self, request):
        raw = request.data.get("refresh")
        session_id = getattr(request.user, "session_id", None)
        sessions = UserSession.objects.filter(user=request.user, revoked_at__isnull=True)
        if raw:
            sessions = sessions.filter(refresh_token_hash=_hash(raw))
        elif session_id:
            sessions = sessions.filter(pk=session_id)
        sessions.update(revoked_at=timezone.now(), revoked_reason="logout")
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(_me_payload(request.user, request))

    def patch(self, request):
        serializer = UpdateMeSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(_me_payload(request.user, request))


class ChangePasswordView(APIView):
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"user": request.user})
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data["currentPassword"]):
            raise ValidationFailed(
                "Your current password is incorrect.",
                field_errors={"currentPassword": ["Incorrect password."]},
            )
        request.user.set_password(serializer.validated_data["newPassword"])
        request.user.save(update_fields=["password"])
        # Every other session is signed out; the current one keeps working.
        UserSession.objects.filter(user=request.user, revoked_at__isnull=True).exclude(
            pk=getattr(request.user, "session_id", None) or ""
        ).update(revoked_at=timezone.now(), revoked_reason="password_changed")
        record_audit(
            client=request.client_id,
            actor=request.user,
            action="change_password",
            entity_type="User",
            entity_id=request.user.id,
            entity_label=request.user.email,
            description="Password changed",
            ip=request_ip(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgotPasswordView(APIView):
    """api.md §2 -- always 204, never leak whether the address exists."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = User.objects.filter(
            email=email, deleted_at__isnull=True, status="Active"
        ).first()
        if user is not None:
            token = secrets.token_urlsafe(48)
            PasswordResetToken.objects.create(
                user=user,
                token_hash=_hash(token),
                expires_at=timezone.now() + timedelta(hours=2),
            )
            # Delivery is the notification service's job; in dev the token is
            # logged rather than emailed.
            import logging

            logging.getLogger(__name__).info(
                "Password reset token for %s: %s", email, token
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ResetPasswordView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = PasswordResetToken.objects.filter(
            token_hash=_hash(serializer.validated_data["token"])
        ).select_related("user").first()

        if record is None or not record.is_usable:
            raise ValidationFailed(
                "That reset link is no longer valid. Request a new one.",
                code=Codes.TOKEN_EXPIRED,
                field_errors={"token": ["Expired or already used."]},
            )

        with transaction.atomic():
            user = record.user
            user.set_password(serializer.validated_data["newPassword"])
            user.failed_login_count = 0
            user.locked_until = None
            user.save(update_fields=["password", "failed_login_count", "locked_until"])
            record.used_at = timezone.now()
            record.save(update_fields=["used_at"])
            UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now(), revoked_reason="password_reset"
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionListView(APIView):
    """``GET /auth/sessions/`` -- the topbar security panel."""

    def get(self, request):
        sessions = UserSession.objects.filter(user=request.user).order_by("-last_seen_at")
        data = UserSessionSerializer(
            sessions,
            many=True,
            context={"current_session_id": getattr(request.user, "session_id", None)},
        ).data
        return Response(envelope(data))


class SessionDetailView(APIView):
    def delete(self, request, pk):
        session = UserSession.objects.filter(pk=pk, user=request.user).first()
        if session is None:
            raise NotFound("That session no longer exists.")
        session.revoked_at = timezone.now()
        session.revoked_reason = "revoked_by_user"
        session.save(update_fields=["revoked_at", "revoked_reason"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# api.md §3.1 -- Users
# ---------------------------------------------------------------------------
class UserViewSet(TenantModelViewSet):
    queryset = User.objects.select_related(
        "role", "reporting_manager", "employee", "employee__designation", "employee__department"
    ).all()
    serializer_class = UserSerializer
    audit_entity_type = "User"
    audit_label_field = "email"
    search_fields = ["name", "email", "phone", "department"]
    ordering_fields = ["name", "email", "created_at", "last_login_at", "status"]
    ordering = ["name"]
    filter_map = {
        "role": "role__name",
        "roleId": "role_id",
        "department": "department",
        "status": "status",
    }
    status_field = "status"
    default_date_field = "joined_date"
    permission_map = {
        "list": ["view_staff"],
        "retrieve": ["view_staff"],
        "create": ["create_staff"],
        "update": ["edit_staff"],
        "partial_update": ["edit_staff"],
        "destroy": ["delete_staff"],
        "activate": ["edit_staff"],
        "deactivate": ["edit_staff"],
        "reset_password": ["reset_staff_password"],
        "permissions": ["manage_roles"],
    }

    def get_queryset(self):
        try:
            from apps.hrms.services import sync_all_users_and_employees
            sync_all_users_and_employees(self.get_client_id())
        except Exception:
            pass
        return super().get_queryset()

    @staticmethod
    def _sync_employee_for_user(user, raw_data=None):
        """Connect user in Administrator with HRMS Employee."""
        if not user:
            return None
        from apps.hrms.services import sync_employee_from_user
        return sync_employee_from_user(user, raw_data)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        user = serializer.instance
        self._sync_employee_for_user(user, getattr(serializer, "initial_data", None))
        return user

    def perform_update(self, serializer):
        super().perform_update(serializer)
        user = serializer.instance
        self._sync_employee_for_user(user, getattr(serializer, "initial_data", None))
        return user

    def get_aggregates(self, queryset):
        """api.md §3.1 -- "KPI tiles: total, active, inactive, admins"."""
        rows = queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            inactive=Count("id", filter=Q(status="Inactive")),
            admins=Count("id", filter=Q(role__code="AD")),
        )
        return rows

    def perform_destroy(self, instance):
        """api.md §3.1 -- soft delete (``status: Deleted``)."""
        instance.status = "Deleted"
        instance.is_active = False
        instance.save(update_fields=["status", "is_active"])
        UserSession.objects.filter(user=instance, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="user_deleted"
        )
        if instance.employee_id:
            try:
                emp = instance.employee
                if emp and not emp.deleted_at:
                    emp.status = "Terminated"
                    emp.last_working_day = timezone.localdate()
                    emp.save(update_fields=["status", "last_working_day", "updated_at"])
            except Exception:
                pass
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        return Response(self.get_aggregates(self.filter_queryset(self.get_queryset())))

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        user.status = "Active"
        user.is_active = True
        user.save(update_fields=["status", "is_active"])
        if user.employee_id:
            try:
                emp = user.employee
                if emp and emp.status != "Active":
                    emp.status = "Active"
                    emp.save(update_fields=["status", "updated_at"])
            except Exception:
                pass
        self.write_audit("activate", user, description="Account activated")
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """api.md §3.1 -- ``status: Inactive``, revoke sessions."""
        user = self.get_object()
        if user.id == request.user.id:
            raise Conflict(
                "You cannot deactivate your own account.", code="SELF_DEACTIVATION"
            )
        with transaction.atomic():
            user.status = "Inactive"
            user.is_active = False
            user.save(update_fields=["status", "is_active"])
            UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now(), revoked_reason="user_deactivated"
            )
            if user.employee_id:
                try:
                    emp = user.employee
                    if emp and emp.status == "Active":
                        emp.status = "Inactive"
                        emp.save(update_fields=["status", "updated_at"])
                except Exception:
                    pass
            self.write_audit("deactivate", user, description="Account deactivated")
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()
        token = secrets.token_urlsafe(48)
        PasswordResetToken.objects.create(
            user=user, token_hash=_hash(token), expires_at=timezone.now() + timedelta(hours=24)
        )
        self.write_audit("reset_password", user, description="Admin-triggered password reset")
        import logging

        logging.getLogger(__name__).info("Admin reset token for %s: %s", user.email, token)
        return Response({"message": "A password reset email has been sent."})

    @action(detail=True, methods=["get", "post"], url_path="permissions")
    def permissions(self, request, pk=None):
        """Per-user overrides on top of the role (api.md §3.1)."""
        user = self.get_object()
        if request.method == "GET":
            overrides = UserPermission.objects.filter(user=user)
            return Response(
                {
                    "effective": sorted(user.effective_permissions()),
                    "role": sorted(user.role.permission_ids()) if user.role_id else [],
                    "grant": sorted(
                        o.permission_id for o in overrides if o.effect == "grant"
                    ),
                    "deny": sorted(o.permission_id for o in overrides if o.effect == "deny"),
                }
            )

        serializer = UserPermissionOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            UserPermission.objects.filter(user=user).delete()
            UserPermission.objects.bulk_create(
                [
                    UserPermission(user=user, permission_id=p, effect="grant")
                    for p in serializer.validated_data.get("grant", [])
                ]
                + [
                    UserPermission(user=user, permission_id=p, effect="deny")
                    for p in serializer.validated_data.get("deny", [])
                ]
            )
            self.write_audit(
                "permissions", user, after=serializer.validated_data,
                description="Permission overrides updated",
            )
        return Response({"effective": sorted(user.effective_permissions())})


# ---------------------------------------------------------------------------
# api.md §3.2 -- Roles and permissions
# ---------------------------------------------------------------------------
class RoleViewSet(TenantModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    audit_entity_type = "Role"
    audit_label_field = "name"
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "code", "created_at"]
    ordering = ["name"]
    status_field = None
    permission_map = {"read": ["manage_roles"], "write": ["manage_roles"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            user_count=Count("users", filter=Q(users__deleted_at__isnull=True))
        )

    def check_delete_allowed(self, role):
        """api.md §3.2 -- 409 if users are attached unless ``?reassignTo=``."""
        attached = User.objects.filter(role=role, deleted_at__isnull=True)
        count = attached.count()
        if not count:
            return

        reassign_to = self.request.query_params.get("reassignTo")
        if not reassign_to:
            raise Conflict(
                f"{count} user{'s' if count != 1 else ''} still use this role.",
                code=Codes.IN_USE,
                detail="Pass ?reassignTo=<roleId> to move them to another role first.",
                payload={"userCount": count},
            )

        target = Role.objects.filter(
            pk=reassign_to, client_id=self.get_client_id(), deleted_at__isnull=True
        ).first()
        if target is None:
            raise ValidationFailed(
                "The role to reassign to was not found.",
                field_errors={"reassignTo": ["Unknown role id."]},
            )
        if target.pk == role.pk:
            raise ValidationFailed(
                "Reassign to a different role.",
                field_errors={"reassignTo": ["Cannot reassign to the role being deleted."]},
            )
        attached.update(role=target)

    def perform_destroy(self, instance):
        if instance.is_system:
            raise Conflict(
                "System roles cannot be deleted.", code="SYSTEM_ROLE"
            )
        super().perform_destroy(instance)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """The UI's "Copy" action (api.md §3.2)."""
        source = self.get_object()
        base_code = f"{source.code}-COPY"
        code, suffix = base_code, 1
        while Role.objects.filter(
            client_id=self.get_client_id(), code=code, deleted_at__isnull=True
        ).exists():
            suffix += 1
            code = f"{base_code}{suffix}"

        with transaction.atomic():
            clone = Role.objects.create(
                client_id=self.get_client_id(),
                code=code,
                name=request.data.get("name") or f"{source.name} (Copy)",
                description=source.description,
                is_system=False,
                created_by=request.user,
                updated_by=request.user,
            )
            RolePermission.objects.bulk_create(
                [
                    RolePermission(role=clone, permission_id=permission_id)
                    for permission_id in source.permission_ids()
                ]
            )
            self.write_audit("duplicate", clone, description=f"Duplicated from {source.name}")
        clone.user_count = 0
        return Response(self.get_serializer(clone).data, status=status.HTTP_201_CREATED)


class PermissionCatalogueView(APIView):
    """``GET /admin/permissions/`` -- modules -> groups -> permissions."""

    permission_classes = [HasModulePermission]
    required_permissions = ["manage_roles"]

    def get(self, request):
        flat = request.query_params.get("flat") == "true"
        if flat:
            rows = PermissionSerializer(Permission.objects.all(), many=True).data
            return Response(envelope(rows))
        return Response({"modules": grouped_catalogue()})


# ---------------------------------------------------------------------------
# api.md §3.3 -- Clients (tenants)
# ---------------------------------------------------------------------------
class ClientViewSet(TenantModelViewSet):
    """Administration -> Clients.

    A tenant can only see and edit itself; the list is therefore its own row.
    Managing *other* tenants is a platform-operator concern and deliberately
    outside this tenant-scoped API.
    """

    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    audit_entity_type = "Client"
    audit_label_field = "name"
    search_fields = ["name", "contact_name", "contact_email", "industry"]
    ordering = ["name"]
    filter_soft_deleted = False
    permission_map = {"read": ["menu_admin"], "write": ["manage_company_profile"]}

    def get_queryset(self):
        client_id = self.get_client_id()
        if client_id is None:
            return Client.objects.none()
        return Client.objects.filter(pk=client_id)

    def create_defaults(self):
        return {}

    def create(self, request, *args, **kwargs):
        raise PermissionDenied(
            "Tenants are provisioned by the platform operator.",
            code="TENANT_PROVISIONING",
        )

    @action(detail=True, methods=["get"])
    def deals(self, request, pk=None):
        """api.md §3.3 -- linked CRM deals."""
        from apps.crm.models import Deal
        from apps.crm.serializers import DealSerializer

        self.get_object()
        rows = Deal.objects.filter(client_id=pk, deleted_at__isnull=True).select_related(
            "party", "owner"
        )
        return Response(envelope(DealSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def projects(self, request, pk=None):
        """api.md §3.3 -- linked projects with ``progress``."""
        from apps.pms.models import Project
        from apps.pms.serializers import ProjectListSerializer

        self.get_object()
        rows = Project.objects.filter(client_id=pk, deleted_at__isnull=True).select_related(
            "project_manager"
        )
        return Response(envelope(ProjectListSerializer(rows, many=True).data))
