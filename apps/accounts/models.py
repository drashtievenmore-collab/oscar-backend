"""
Tenants, users, roles and permissions (db.md §2.1 - §2.3).

``clients`` is the tenant record behind Administration -> Clients (api.md §3.3).
db.md §2.1 is explicit that the tenant notion stays separate from ``parties``:
a tenant is who is logged in, a party is who they trade with.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import AuditedModel, TenantModel, UUIDModel


# ---------------------------------------------------------------------------
# db.md §2.1 -- Tenants
# ---------------------------------------------------------------------------
class Client(UUIDModel):
    STATUSES = [("Active", "Active"), ("Suspended", "Suspended"), ("Cancelled", "Cancelled")]

    name = models.TextField()
    slug = models.SlugField(max_length=120, unique=True)
    plan = models.TextField(default="Standard")
    status = models.TextField(choices=STATUSES, default="Active")
    timezone = models.TextField(default="Asia/Kolkata")
    currency = models.CharField(max_length=3, default="INR")
    fy_start_month = models.SmallIntegerField(default=4)
    onboarded_on = models.DateField(null=True, blank=True)

    # The 360 view (api.md §3.3) also renders these contact fields.
    contact_name = models.TextField(null=True, blank=True)
    contact_email = models.EmailField(null=True, blank=True)
    contact_phone = models.TextField(null=True, blank=True)
    industry = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    is_demo = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    legacy_id = models.TextField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "clients"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(fy_start_month__gte=1, fy_start_month__lte=12),
                name="ck_clients_fy_start_month",
            )
        ]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# db.md §2.2 -- Roles and permissions
# ---------------------------------------------------------------------------
class Permission(models.Model):
    """The global catalogue -- NOT tenant-scoped (db.md §2.2).

    Seeded from api.md Appendix B including the gap-filling Sales / Purchase /
    Inventory / PMS ids. ``GET /admin/permissions/`` reads this table grouped
    by module, group, sort_order, so the UI's grouping comes from data rather
    than from the hardcoded ``DEFAULT_MODULE_PERMISSIONS`` constant it uses now.
    """

    id = models.TextField(primary_key=True)
    module = models.TextField()
    group = models.TextField()
    label = models.TextField()
    description = models.TextField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "permissions"
        ordering = ["module", "sort_order", "id"]

    def __str__(self):
        return self.id


class Role(TenantModel):
    code = models.TextField()
    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    is_system = models.BooleanField(default=False)
    permissions = models.ManyToManyField(
        Permission, through="RolePermission", related_name="roles"
    )
    legacy_id = models.TextField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "roles"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_roles_code",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def permission_ids(self):
        return set(
            RolePermission.objects.filter(role=self).values_list("permission_id", flat=True)
        )


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="role_permissions"
    )

    class Meta:
        db_table = "role_permissions"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="pk_role_permissions")
        ]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("status", "Active")
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra.get("name") is None:
            extra["name"] = email.split("@")[0]
        if extra.get("client_id") is None and extra.get("client") is None:
            client = Client.objects.order_by("created_at").first()
            if client is None:
                client = Client.objects.create(name="Default Tenant", slug="default")
            extra["client"] = client
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, AuditedModel):
    """db.md §2.2.

    ``employee_id`` is the owning side of the user <-> employee relationship;
    db.md §2.2 warns that a bidirectional pair of FKs will drift, so
    ``hrms_employees`` exposes the reverse as a join, not a column.
    """

    STATUSES = [
        ("Active", "Active"),
        ("Inactive", "Inactive"),
        ("Invited", "Invited"),
        ("Deleted", "Deleted"),
    ]

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="users")
    email = models.EmailField()
    name = models.TextField()
    phone = models.TextField(null=True, blank=True)
    avatar_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    avatar_url = models.TextField(null=True, blank=True)
    role = models.ForeignKey(
        Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    employee = models.ForeignKey(
        "hrms.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    department = models.TextField(null=True, blank=True)
    location = models.TextField(null=True, blank=True)
    reporting_manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="direct_reports"
    )
    status = models.TextField(choices=STATUSES, default="Active")
    joined_date = models.DateField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    failed_login_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    #: CRM stage automation resolves assignees by role + department against a
    #: team roster (api.md §9.3). The frontend hardcodes this in
    #: CRM_TEAM_MEMBERS; here it is a column on the user, exposed at
    #: GET /crm/team-roster/.
    crm_roles = models.JSONField(default=list, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    legacy_id = models.TextField(null=True, blank=True, db_index=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        db_table = "users"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "email"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_users_email",
            )
        ]
        indexes = [models.Index(fields=["client", "status"], name="ix_users_status")]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)

    # -- permissions -------------------------------------------------------
    def effective_permissions(self):
        if self.is_superuser:
            all_perms = set(Permission.objects.values_list("id", flat=True))
            all_perms.add("*")
            return all_perms
        granted = set()
        if self.role_id:
            granted |= set(
                RolePermission.objects.filter(role_id=self.role_id).values_list(
                    "permission_id", flat=True
                )
            )
        overrides = UserPermission.objects.filter(user_id=self.id).values_list(
            "permission_id", "effect"
        )
        for permission_id, effect in overrides:
            if effect == "grant":
                granted.add(permission_id)
            else:
                granted.discard(permission_id)
        return granted

    @property
    def display_role(self):
        return self.role.name if self.role_id else None

    def is_locked(self):
        return self.locked_until is not None and self.locked_until > timezone.now()


class UserPermission(models.Model):
    """Per-user overrides on top of the role (db.md §2.2)."""

    EFFECTS = [("grant", "grant"), ("deny", "deny")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="permission_overrides")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="+")
    effect = models.TextField(choices=EFFECTS)

    class Meta:
        db_table = "user_permissions"
        constraints = [
            models.UniqueConstraint(fields=["user", "permission"], name="pk_user_permissions")
        ]


# ---------------------------------------------------------------------------
# db.md §2.3 -- Sessions and tokens
# ---------------------------------------------------------------------------
class UserSession(models.Model):
    """Backs ``/auth/sessions/`` and ``/auth/logout/``.

    The refresh token is stored as a sha256 hash and never in the clear.
    Deactivating a user (api.md §3.1) revokes every row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="sessions")
    refresh_token_hash = models.TextField(unique=True)
    user_agent = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    device_label = models.TextField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "user_sessions"
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(
                fields=["user"],
                name="ix_user_sessions_active",
                condition=models.Q(revoked_at__isnull=True),
            )
        ]

    @property
    def is_active(self):
        return self.revoked_at is None and self.expires_at > timezone.now()


class PasswordResetToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reset_tokens")
    token_hash = models.TextField(unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "password_reset_tokens"

    @property
    def is_usable(self):
        return self.used_at is None and self.expires_at > timezone.now()
