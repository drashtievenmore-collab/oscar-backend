"""Serializers for auth and administration (api.md §2 and §3)."""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.core.exceptions import ValidationFailed
from apps.core.serializers import BaseModelSerializer, BaseSerializer

from .models import Client, Permission, Role, RolePermission, User, UserPermission, UserSession


# ---------------------------------------------------------------------------
# Identity (api.md §2)
# ---------------------------------------------------------------------------
class RoleRefSerializer(BaseModelSerializer):
    """The nested role shape in ``/auth/me/``: ``{ id, code, name }``."""

    class Meta:
        model = Role
        fields = ["id", "code", "name"]


class MeUserSerializer(BaseModelSerializer):
    """api.md §2 -- drives sidebar visibility and every permission gate."""

    role = RoleRefSerializer(read_only=True)
    employeeId = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    reportingManager = serializers.SerializerMethodField()
    lastLogin = serializers.DateTimeField(source="last_login_at", read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "phone", "avatar", "employeeId", "role",
            "department", "location", "reportingManager", "status", "lastLogin",
            "is_superuser",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["roleCode"] = instance.role.code if instance.role_id else None
        data["isSuperuser"] = bool(instance.is_superuser)
        data["is_superuser"] = bool(instance.is_superuser)
        return data

    def get_employeeId(self, user):
        return user.employee.employee_code if user.employee_id else None

    def get_avatar(self, user):
        return user.avatar_url

    def get_reportingManager(self, user):
        return user.reporting_manager.name if user.reporting_manager_id else None


class TenantSerializer(BaseModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name", "plan"]


class LoginSerializer(BaseSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RefreshSerializer(BaseSerializer):
    refresh = serializers.CharField()


class ChangePasswordSerializer(BaseSerializer):
    currentPassword = serializers.CharField(write_only=True, trim_whitespace=False)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_newPassword(self, value):
        try:
            validate_password(value, user=self.context.get("user"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class ForgotPasswordSerializer(BaseSerializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(BaseSerializer):
    token = serializers.CharField()
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_newPassword(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class UpdateMeSerializer(BaseModelSerializer):
    """``PATCH /auth/me/`` -- own profile only: name, phone, avatar."""

    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)

    class Meta:
        model = User
        fields = ["name", "phone", "avatar"]


class UserSessionSerializer(BaseModelSerializer):
    isCurrent = serializers.SerializerMethodField()
    device = serializers.CharField(source="device_label", read_only=True)

    class Meta:
        model = UserSession
        fields = [
            "id", "device", "user_agent", "ip", "issued_at", "last_seen_at",
            "expires_at", "revoked_at", "isCurrent",
        ]

    def get_isCurrent(self, session):
        return str(session.id) == str(self.context.get("current_session_id") or "")


# ---------------------------------------------------------------------------
# Administration -- users (api.md §3.1)
# ---------------------------------------------------------------------------
class UserSerializer(BaseModelSerializer):
    """The exact row shape api.md §3.1 documents.

    ``role`` is the role *name* (the UI renders it as text) while ``roleId`` is
    the writable relation -- both appear on the row in api.md.
    """

    role = serializers.SerializerMethodField()
    roleId = serializers.PrimaryKeyRelatedField(
        source="role", queryset=Role.objects.all(), allow_null=True, required=False
    )
    employeeId = serializers.SerializerMethodField()
    #: HRMS is the source of truth for a staff member's job, so the employee
    #: row is joined into the admin user row read-only. Administration is the
    #: main staff list; these are what make an HR-sourced account readable
    #: there without a second lookup (api.md §3.1).
    isFromHrms = serializers.SerializerMethodField()
    designation = serializers.SerializerMethodField()
    employeeStatus = serializers.SerializerMethodField()
    reportingManager = serializers.SerializerMethodField()
    reportingManagerId = serializers.PrimaryKeyRelatedField(
        source="reporting_manager",
        queryset=User.objects.all(),
        allow_null=True,
        required=False,
    )
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)
    joinedDate = serializers.DateField(source="joined_date", required=False, allow_null=True)
    lastLogin = serializers.DateTimeField(source="last_login_at", read_only=True)
    permissions = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "phone", "role", "roleId", "department", "status",
            "joinedDate", "lastLogin", "employeeId", "isFromHrms", "designation",
            "employeeStatus", "location", "reportingManager",
            "reportingManagerId", "avatar", "permissions", "crm_roles", "password",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_role(self, user):
        return user.role.name if user.role_id else None

    def get_employeeId(self, user):
        return user.employee.employee_code if user.employee_id else None

    def get_isFromHrms(self, user):
        return user.employee_id is not None

    def get_designation(self, user):
        employee = user.employee
        if employee is None or employee.designation_id is None:
            return None
        return employee.designation.name

    def get_employeeStatus(self, user):
        """The HR record's status, which is what drives the linked account."""
        return user.employee.status if user.employee_id else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["roleCode"] = instance.role.code if instance.role_id else None
        # An HR-sourced account inherits the employee photo and joining date
        # when nobody set them on the user itself -- the user row is a login
        # identity, the employee row is the person.
        employee = instance.employee
        if employee is not None:
            if not data.get("avatar") and employee.avatar_url:
                data["avatar"] = employee.avatar_url
            if not data.get("joinedDate") and employee.joining_date:
                data["joinedDate"] = employee.joining_date.isoformat()
            if not data.get("department") and employee.department_id:
                data["department"] = employee.department.name
        return data

    def get_reportingManager(self, user):
        return user.reporting_manager.name if user.reporting_manager_id else None

    def get_permissions(self, user):
        """Role-derived plus overrides (api.md §3.1: "permissions[]" on detail)."""
        return sorted(user.effective_permissions())

    def validate_email(self, value):
        value = value.lower()
        client_id = self.context.get("client_id")
        existing = User.objects.filter(client_id=client_id, email=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        # If roleId was not provided, but role name was in raw input, resolve it
        if not validated_data.get("role"):
            raw_role = self.initial_data.get("role") or self.initial_data.get("roleName")
            if raw_role:
                client_id = self.context.get("client_id")
                matched_role = Role.objects.filter(client_id=client_id, name__iexact=str(raw_role).strip()).first()
                if not matched_role:
                    matched_role = Role.objects.filter(client_id=client_id, code__iexact=str(raw_role).strip()).first()
                if matched_role:
                    validated_data["role"] = matched_role
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            # api.md §3.1 -- create "triggers the invite email"; until the
            # invite is accepted the account cannot be logged into.
            user.set_unusable_password()
            user.status = validated_data.get("status") or "Invited"
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        if "role" not in validated_data and ("role" in self.initial_data or "roleName" in self.initial_data):
            raw_role = self.initial_data.get("role") or self.initial_data.get("roleName")
            if raw_role:
                client_id = self.context.get("client_id") or getattr(instance, "client_id", None)
                matched_role = Role.objects.filter(client_id=client_id, name__iexact=str(raw_role).strip()).first()
                if not matched_role:
                    matched_role = Role.objects.filter(client_id=client_id, code__iexact=str(raw_role).strip()).first()
                if matched_role:
                    validated_data["role"] = matched_role
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user


class UserPermissionOverrideSerializer(BaseSerializer):
    """``POST /admin/users/{id}/permissions/`` -- overrides on top of the role."""

    grant = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    deny = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    def validate(self, attrs):
        known = set(Permission.objects.values_list("id", flat=True))
        unknown = [p for p in attrs.get("grant", []) + attrs.get("deny", []) if p not in known]
        if unknown:
            raise ValidationFailed(
                "Unknown permission ids.",
                field_errors={"permissions": [f"Not in the catalogue: {', '.join(unknown)}"]},
            )
        overlap = set(attrs.get("grant", [])) & set(attrs.get("deny", []))
        if overlap:
            raise ValidationFailed(
                "A permission cannot be both granted and denied.",
                field_errors={"deny": [", ".join(sorted(overlap))]},
            )
        return attrs


# ---------------------------------------------------------------------------
# Administration -- roles (api.md §3.2)
# ---------------------------------------------------------------------------
class RoleSerializer(BaseModelSerializer):
    userCount = serializers.SerializerMethodField()
    selectedPermissions = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True
    )
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            "id", "code", "name", "description", "is_system", "userCount",
            "permissions", "selectedPermissions", "created_at", "updated_at",
        ]
        read_only_fields = ["is_system", "created_at", "updated_at"]

    def get_userCount(self, role):
        cached = getattr(role, "user_count", None)
        if cached is not None:
            return cached
        return role.users.filter(deleted_at__isnull=True).count()

    def get_permissions(self, role):
        return sorted(
            RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)
        )

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        existing = Role.objects.filter(client_id=client_id, code=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A role with this code already exists.")
        return value

    def _apply_permissions(self, role, permission_ids):
        known = set(Permission.objects.values_list("id", flat=True))
        unknown = [p for p in permission_ids if p not in known]
        if unknown:
            raise ValidationFailed(
                "Unknown permission ids.",
                field_errors={
                    "selectedPermissions": [f"Not in the catalogue: {', '.join(unknown)}"]
                },
            )
        RolePermission.objects.filter(role=role).delete()
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, permission_id=p) for p in permission_ids]
        )

    def create(self, validated_data):
        permission_ids = validated_data.pop("selectedPermissions", [])
        role = super().create(validated_data)
        self._apply_permissions(role, permission_ids)
        return role

    def update(self, instance, validated_data):
        permission_ids = validated_data.pop("selectedPermissions", None)
        role = super().update(instance, validated_data)
        if permission_ids is not None:
            self._apply_permissions(role, permission_ids)
        return role


class PermissionSerializer(BaseModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "module", "group", "label", "description", "sort_order"]


# ---------------------------------------------------------------------------
# Administration -- clients / tenants (api.md §3.3)
# ---------------------------------------------------------------------------
class ClientSerializer(BaseModelSerializer):
    userCount = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id", "name", "slug", "plan", "status", "timezone", "currency",
            "fy_start_month", "onboarded_on", "contact_name", "contact_email",
            "contact_phone", "industry", "notes", "is_demo", "userCount",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_userCount(self, client):
        return client.users.filter(deleted_at__isnull=True).count()
