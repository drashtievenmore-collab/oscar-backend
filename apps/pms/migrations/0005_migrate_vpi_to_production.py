"""Copy jobwork VendorProcessInstruction rows into ProductionInstruction.

The jobwork VPI path duplicated the production module. Each live VPI becomes
a ProductionInstruction (its VPI progress entries become daily entries) when
its assigned employee can be matched to an hrms Employee by code or name.
Rows that cannot be matched are left untouched in jobwork_process_instructions
so no data is lost; the migration prints them for manual follow-up.
Re-running is safe: a VPI already copied (same instruction number) is skipped.
"""
from decimal import Decimal

from django.db import migrations

STATUS_MAP = {
    "Pending": "Draft",
    "Draft": "Draft",
    "In Progress": "In Progress",
    "Running": "In Progress",
    "Partially Completed": "In Progress",
    "Completed": "Completed",
    "Cancelled": "Closed",
}


def _match_employee(Employee, client_id, text):
    text = (text or "").strip()
    if not text:
        return None
    qs = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
    return (
        qs.filter(employee_code__iexact=text).first()
        or qs.filter(name__iexact=text).first()
        or qs.filter(legacy_id=text).first()
    )


def forwards(apps, schema_editor):
    VPI = apps.get_model("jobwork", "VendorProcessInstruction")
    Instruction = apps.get_model("pms", "ProductionInstruction")
    Entry = apps.get_model("pms", "DailyProductionEntry")
    Employee = apps.get_model("hrms", "Employee")

    skipped = []
    for vpi in VPI.objects.filter(deleted_at__isnull=True).order_by("created_at"):
        if Instruction.objects.filter(
            client_id=vpi.client_id, instruction_number=vpi.pi_number
        ).exists():
            continue
        employee = _match_employee(Employee, vpi.client_id, vpi.assigned_employee)
        if employee is None:
            skipped.append(vpi.pi_number)
            continue
        instruction = Instruction.objects.create(
            client_id=vpi.client_id,
            instruction_number=vpi.pi_number,
            agency_name=vpi.vendor or "",
            order_reference=vpi.po_number or vpi.pi_number,
            order_meter=vpi.assigned_qty or Decimal("0"),
            pi_date=vpi.date,
            fabric=vpi.fabric or None,
            process_type=vpi.process_type or None,
            employee=employee,
            status=STATUS_MAP.get(vpi.status, "Draft"),
            notes="\n".join(
                filter(None, [vpi.remarks, f"Migrated from vendor process instruction {vpi.pi_number}."])
            ),
            created_by_id=vpi.created_by_id,
        )
        for e in _live_entries(apps, vpi):
            if not e.produced_qty or e.produced_qty <= 0:
                continue
            Entry.objects.create(
                client_id=vpi.client_id,
                instruction=instruction,
                entry_date=e.date,
                meters=e.produced_qty,
                entered_by_employee=_match_employee(Employee, vpi.client_id, e.entered_by) or employee,
                remarks=e.remarks,
                created_by_id=e.created_by_id,
            )
    if skipped:
        print(
            "\n  VPIs not migrated (assigned employee not found in HRMS): "
            + ", ".join(skipped)
        )


def _live_entries(apps, vpi):
    VPIProgressEntry = apps.get_model("jobwork", "VPIProgressEntry")
    return VPIProgressEntry.objects.filter(
        instruction_id=vpi.id, deleted_at__isnull=True
    ).order_by("date")


class Migration(migrations.Migration):

    dependencies = [
        ("pms", "0004_production_completion"),
        ("jobwork", "0006_vpiprogressentry_photo_file"),
        ("hrms", "0003_offer_department_offer_expiry_date_offer_job_type_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
