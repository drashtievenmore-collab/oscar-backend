"""Data migration to safely copy existing records from duplicate HRMS tables to canonical PMS/Sales tables (P12).

Pairs migrated:
1. hrms.SalespersonIncentiveScheme -> pms.IncentiveScheme
   hrms.SalespersonIncentiveAccrual -> pms.IncentiveCalculation
2. hrms.BrokerCommissionRecord -> sales.BrokerCommission
3. hrms.DailyAgencyProductionEntry -> pms.DailyProductionEntry

The HRMS and PMS/Sales models do not share field names or choice values, so
every field is mapped explicitly. A row the target cannot hold (a required
link the source never had) is skipped and reported, never half-copied; the
source rows are left untouched either way.
"""
from django.db import migrations

SCHEME_PERIOD = {"MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "CUSTOM_3_MONTH": "Custom"}
SCHEME_BASIS = {"INVOICE_VALUE": "INVOICE", "COLLECTIONS": "COLLECTION", "GROSS_PROFIT": "GROSS_PROFIT"}
ACCRUAL_STATUS = {"DRAFT": "Calculated", "ACCRUED": "Calculated", "HELD": "Calculated", "APPROVED": "Approved", "PAID": "Paid"}
BROKER_STATUS = {"accrued": "Accrued", "approved": "Approved", "payable": "Payable", "paid": "Paid", "hold": "Hold", "cancelled": "Cancelled"}


def _slabs(rows):
    """HRMS {min_amount, max_amount, rate_percent} -> PMS {min, max, rate_pct}."""
    out = []
    for slab in rows or []:
        if not isinstance(slab, dict):
            continue
        out.append({
            "min": slab.get("min_amount", slab.get("min", 0)) or 0,
            "max": slab.get("max_amount", slab.get("max")),
            "rate_pct": slab.get("rate_percent", slab.get("rate_pct", 0)) or 0,
        })
    return out


def copy_duplicate_hrms_records(apps, schema_editor):
    report = {"copied": [], "skipped": []}

    # 1. SalespersonIncentiveScheme -> pms.IncentiveScheme
    HrmsScheme = apps.get_model("hrms", "SalespersonIncentiveScheme")
    PmsScheme = apps.get_model("pms", "IncentiveScheme")
    for h in HrmsScheme.objects.all():
        if PmsScheme.objects.filter(id=h.id).exists():
            continue
        PmsScheme.objects.create(
            id=h.id,
            client_id=h.client_id,
            name=h.name,
            description=h.description,
            period_type=SCHEME_PERIOD.get(h.frequency, "Quarterly"),
            basis=SCHEME_BASIS.get(h.basis, "INVOICE"),
            slab_type=h.slab_type if h.slab_type in ("FLAT", "TIERED") else "FLAT",
            rate_pct=h.flat_rate_percent or 0,
            slabs=_slabs(h.slabs),
            hold_unpaid_invoices=bool(h.deduct_bad_debts),
            is_active=h.is_active,
            created_by_id=h.created_by_id,
            deleted_at=h.deleted_at,
        )
        report["copied"].append(f"scheme {h.name}")

    # 1b. SalespersonIncentiveAccrual -> pms.IncentiveCalculation
    HrmsAccrual = apps.get_model("hrms", "SalespersonIncentiveAccrual")
    PmsCalc = apps.get_model("pms", "IncentiveCalculation")
    for h in HrmsAccrual.objects.all():
        if PmsCalc.objects.filter(id=h.id).exists():
            continue
        if not h.scheme_id or not PmsScheme.objects.filter(id=h.scheme_id).exists():
            report["skipped"].append(f"incentive {h.period_name} ({h.id}): no scheme")
            continue
        label = h.period_name or "MIGRATED"
        if PmsCalc.objects.filter(client_id=h.client_id, scheme_id=h.scheme_id, employee_id=h.employee_id,
                                  period_label=label, deleted_at__isnull=True).exists():
            report["skipped"].append(f"incentive {label} ({h.id}): PMS already has this period")
            continue
        amount = h.calculated_incentive or 0
        status = ACCRUAL_STATUS.get(h.status, "Calculated")
        PmsCalc.objects.create(
            id=h.id,
            client_id=h.client_id,
            scheme_id=h.scheme_id,
            employee_id=h.employee_id,
            salesperson_id=h.employee_id,
            period_label=label,
            period_start=h.start_date,
            period_end=h.end_date,
            net_sales=h.net_eligible_sales or 0,
            returns_total=h.returns_deduction or 0,
            incentive_amount=amount,
            hold_amount=amount if h.status == "HELD" else 0,
            paid_amount=amount if status == "Paid" else 0,
            status=status,
            approved_by_id=h.approved_by_id,
            approved_at=h.approved_at,
            paid_at=h.paid_at,
            journal_entry_id=h.journal_entry_id,
            detail={
                "migratedFrom": "hrms.SalespersonIncentiveAccrual",
                "grossSales": str(h.gross_sales or 0),
                "applicableRate": str(h.applicable_rate or 0),
                "holdReason": h.hold_reason or "",
            },
            created_by_id=h.created_by_id,
            deleted_at=h.deleted_at,
        )
        report["copied"].append(f"incentive {label}")

    # 2. BrokerCommissionRecord -> sales.BrokerCommission (needs invoice + party)
    HrmsBcr = apps.get_model("hrms", "BrokerCommissionRecord")
    SalesBc = apps.get_model("sales", "BrokerCommission")
    for h in HrmsBcr.objects.all():
        if SalesBc.objects.filter(id=h.id).exists():
            continue
        if not (h.broker_id and h.invoice_id and h.party_id and h.invoice_date):
            report["skipped"].append(f"broker commission {h.id}: no broker/invoice/party")
            continue
        SalesBc.objects.create(
            id=h.id,
            client_id=h.client_id,
            broker_id=h.broker_id,
            invoice_id=h.invoice_id,
            party_id=h.party_id,
            salesperson_id=h.salesperson_id,
            commission_number=h.commission_number,
            invoice_date=h.invoice_date,
            invoice_amount=h.invoice_amount or 0,
            commission_basis=h.commission_basis if h.commission_basis in ("INVOICE", "COLLECTION", "QUANTITY") else "INVOICE",
            commission_type=h.commission_type if h.commission_type in ("percent", "per_meter", "per_taka", "fixed") else "percent",
            rate=h.rate or 0,
            commission_amount=h.commission_amount or 0,
            paid_amount=h.paid_amount or 0,
            status=BROKER_STATUS.get(str(h.status or "").lower(), "Accrued"),
            approved_by_id=h.approved_by_id,
            approved_at=h.approved_at,
            paid_at=h.paid_at,
            notes=h.notes,
            created_by_id=h.created_by_id,
            deleted_at=h.deleted_at,
        )
        report["copied"].append(f"broker commission {h.commission_number or h.id}")

    # 3. DailyAgencyProductionEntry -> pms.DailyProductionEntry (needs a PMS instruction)
    HrmsEntry = apps.get_model("hrms", "DailyAgencyProductionEntry")
    PmsEntry = apps.get_model("pms", "DailyProductionEntry")
    Instruction = apps.get_model("pms", "ProductionInstruction")
    for h in HrmsEntry.objects.all():
        if PmsEntry.objects.filter(id=h.id).exists():
            continue
        instruction = None
        if h.work_instruction_no:
            instruction = Instruction.objects.filter(
                client_id=h.client_id, instruction_number=h.work_instruction_no, deleted_at__isnull=True
            ).first()
        if instruction is None or not h.employee_id:
            report["skipped"].append(
                f"agency entry {h.entry_date} ({h.id}): no PMS instruction '{h.work_instruction_no or '-'}'"
            )
            continue
        PmsEntry.objects.create(
            id=h.id,
            client_id=h.client_id,
            instruction_id=instruction.id,
            entry_date=h.entry_date,
            opening_pending=h.opening_pending_quantity or 0,
            meters=h.produced_quantity or 0,
            good_meter=h.good_quantity or 0,
            reject_meter=h.reject_quantity or 0,
            reprocess_meter=h.reprocess_quantity or 0,
            cumulative_meter=h.cumulative_produced or 0,
            balance_meter=h.remaining_balance or 0,
            entered_by_employee_id=h.employee_id,
            status="Submitted",
            remarks=h.remarks,
            created_by_id=h.created_by_id,
            deleted_at=h.deleted_at,
        )
        report["copied"].append(f"agency entry {h.entry_date}")

    if report["copied"] or report["skipped"]:
        print(f"\n  hrms.0007: copied {len(report['copied'])}, skipped {len(report['skipped'])}")
        for line in report["skipped"]:
            print(f"    skipped {line}")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hrms", "0006_employeeaward_employeepromotion_employeetransfer_and_more"),
        ("pms", "0003_production_monitoring"),
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(copy_duplicate_hrms_records, noop_reverse),
    ]
