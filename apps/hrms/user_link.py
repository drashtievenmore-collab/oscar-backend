"""The Administration user <-> HRMS employee link (db.md §2.2).

``users.employee_id`` is the only pointer between the two; a person has at most
one live login per employee record. The fields both screens show are kept in
step here: whichever side is saved through the API pushes the fields that
actually changed to the other side, so neither screen shows stale data and an
unchanged field never overwrites a newer value.

Shared fields, as ``(user attribute, employee attribute)``:

    name, email, phone, avatar     -- copied as-is
    joined_date  <-> joining_date
    department   <-> department    -- text on the user, a Department row on HRMS
    location     <-> location      -- text on the user, a Location row on HRMS
    reporting_manager <-> manager  -- each resolved through the other's link

Status is one-way: an employee who has left (Resigned / Terminated) loses their
login. Deactivating a login does not change the employee record -- HR decides
employment, Administration decides access.

Writes use ``QuerySet.update`` so a push never re-enters the other side's save
path, which is what keeps the sync from looping.
"""
from django.utils import timezone

from apps.core.exceptions import ValidationFailed

EXITED_STATUSES = ("Resigned", "Terminated")

#: User attributes whose change is pushed to the employee.
USER_FIELDS = (
    "name", "email", "phone", "avatar_url", "joined_date",
    "department", "location", "reporting_manager_id",
)
#: Employee attributes whose change is pushed to the user.
EMPLOYEE_FIELDS = (
    "name", "email", "phone", "avatar_url", "joining_date",
    "department_id", "location_id", "manager_id", "status",
)


def capture(instance, fields):
    """The values ``fields`` hold now -- taken before a save to diff after it."""
    if instance is None or instance.pk is None:
        return {}
    return {field: getattr(instance, field, None) for field in fields}


def changed_fields(instance, before, fields):
    return {field for field in fields if before.get(field) != getattr(instance, field, None)}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def linked_user(employee):
    from apps.accounts.models import User

    if employee is None or employee.pk is None:
        return None
    return (
        User.objects.filter(employee=employee, deleted_at__isnull=True)
        .exclude(status="Deleted")
        .first()
    )


def linked_employee(user):
    if user is None or not user.employee_id:
        return None
    employee = user.employee
    return employee if employee.deleted_at is None else None


def _named(model, client_id, name):
    """The tenant's row called ``name``, created if HR has not made it yet."""
    name = (name or "").strip()
    if not name:
        return None
    row = model.objects.filter(
        client_id=client_id, name__iexact=name, deleted_at__isnull=True
    ).first()
    return row or model.objects.create(client_id=client_id, name=name)


def assert_linkable(employee, user=None):
    """An employee may back one live login only."""
    other = linked_user(employee)
    if other is not None and (user is None or other.pk != user.pk):
        raise ValidationFailed(
            "This employee is already linked to another user.",
            field_errors={
                "employeeId": [f"{employee.employee_code} is linked to {other.email}."]
            },
        )


# ---------------------------------------------------------------------------
# Pushes
# ---------------------------------------------------------------------------
def push_employee_to_user(employee, fields=EMPLOYEE_FIELDS, *, user=None):
    """Copy the employee's changed ``fields`` onto their login, if any.

    Pass ``user`` when the caller holds that login, so the object it is about
    to serialise carries the new values too.
    """
    from apps.accounts.models import User, UserSession

    user = user or linked_user(employee)
    if user is None:
        return None
    fields = set(fields)
    updates = {}

    if "name" in fields and employee.name:
        updates["name"] = employee.name
    if "phone" in fields:
        updates["phone"] = employee.phone
    if "avatar_url" in fields:
        updates["avatar_url"] = employee.avatar_url
    if "joining_date" in fields and employee.joining_date:
        updates["joined_date"] = employee.joining_date
    if "department_id" in fields:
        updates["department"] = employee.department.name if employee.department_id else None
    if "location_id" in fields:
        updates["location"] = employee.location.name if employee.location_id else None
    if "manager_id" in fields:
        manager_user = linked_user(employee.manager) if employee.manager_id else None
        if manager_user is None or manager_user.pk != user.pk:
            updates["reporting_manager"] = manager_user
    if "email" in fields and employee.email:
        email = employee.email.strip().lower()
        # The login email is unique per tenant; a clash keeps the login as-is.
        taken = (
            User.objects.filter(client_id=user.client_id, email=email, deleted_at__isnull=True)
            .exclude(pk=user.pk)
            .exists()
        )
        if not taken:
            updates["email"] = email
    if "status" in fields and employee.status in EXITED_STATUSES and user.status != "Inactive":
        updates.update(status="Inactive", is_active=False)
        UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="employee_exited"
        )

    updates = {k: v for k, v in updates.items() if getattr(user, k, None) != v}
    if updates:
        User.objects.filter(pk=user.pk).update(updated_at=timezone.now(), **updates)
        for key, value in updates.items():
            setattr(user, key, value)
    return user


def push_user_to_employee(user, fields=USER_FIELDS):
    """Copy the login's changed ``fields`` onto its employee record, if linked."""
    from apps.hrms.models import Department, Employee, Location

    employee = linked_employee(user)
    if employee is None:
        return None
    fields = set(fields)
    updates = {}

    if "name" in fields and user.name:
        updates["name"] = user.name
    if "email" in fields and user.email:
        updates["email"] = user.email
    if "phone" in fields:
        updates["phone"] = user.phone
    if "avatar_url" in fields:
        updates["avatar_url"] = user.avatar_url
    if "joined_date" in fields and user.joined_date:
        updates["joining_date"] = user.joined_date
    # A blank department / location on the login does not wipe HR's value.
    if "department" in fields and (user.department or "").strip():
        updates["department"] = _named(Department, employee.client_id, user.department)
    if "location" in fields and (user.location or "").strip():
        updates["location"] = _named(Location, employee.client_id, user.location)
    if "reporting_manager_id" in fields:
        from apps.hrms.services import assert_no_manager_cycle

        manager = linked_employee(user.reporting_manager) if user.reporting_manager_id else None
        try:
            # A login's manager that would loop HR's reporting lines is not copied.
            assert_no_manager_cycle(employee, manager.pk if manager else None)
            updates["manager"] = manager
        except ValidationFailed:
            pass

    updates = {k: v for k, v in updates.items() if getattr(employee, k, None) != v}
    if updates:
        Employee.objects.filter(pk=employee.pk).update(updated_at=timezone.now(), **updates)
        for key, value in updates.items():
            setattr(employee, key, value)
    return employee


def reconcile(user, employee):
    """A fresh link: HR's values win, and the login fills what HR left blank."""
    from apps.hrms.models import Department, Employee, Location

    fill = {}
    if not employee.phone and user.phone:
        fill["phone"] = user.phone
    if not employee.avatar_url and user.avatar_url:
        fill["avatar_url"] = user.avatar_url
    if not employee.department_id and (user.department or "").strip():
        fill["department"] = _named(Department, employee.client_id, user.department)
    if not employee.location_id and (user.location or "").strip():
        fill["location"] = _named(Location, employee.client_id, user.location)
    if fill:
        Employee.objects.filter(pk=employee.pk).update(updated_at=timezone.now(), **fill)
        for key, value in fill.items():
            setattr(employee, key, value)
    fields = [f for f in EMPLOYEE_FIELDS if f != "status"]
    # Only fields HR actually holds; an empty HR value keeps the login's.
    fields = [f for f in fields if getattr(employee, f, None) not in (None, "")]
    push_employee_to_user(employee, fields, user=user)


def create_employee_for(user, *, actor=None):
    """HR record for a login made in Administration (the reverse of HRMS's
    "Create Administration Login Account")."""
    from apps.core.numbering import allocate_number
    from apps.hrms.models import Department, Designation, Employee, Location

    designation = None
    if user.role:
        designation = _named(Designation, user.client_id, user.role.name)

    employee = Employee.objects.create(
        client_id=user.client_id,
        employee_code=allocate_number(user.client, "EMP"),
        name=user.name,
        email=user.email,
        phone=user.phone,
        avatar_url=user.avatar_url,
        joining_date=user.joined_date or timezone.localdate(),
        department=_named(Department, user.client_id, user.department),
        designation=designation,
        location=_named(Location, user.client_id, user.location),
        manager=linked_employee(user.reporting_manager) if user.reporting_manager_id else None,
        status="Active",
        created_by=actor,
        updated_by=actor,
    )
    return employee


def unlink_employee(employee):
    """An employee record went: its login stays, pointing at nothing."""
    from apps.accounts.models import User

    User.objects.filter(employee=employee).update(employee=None, updated_at=timezone.now())
