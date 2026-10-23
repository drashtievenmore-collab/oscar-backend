"""Data migration to safely copy existing records from duplicate HRMS tables to canonical PMS/Sales tables (P12).

Pairs migrated:
1. hrms.SalespersonIncentiveScheme -> pms.IncentiveScheme
   hrms.SalespersonIncentiveAccrual -> pms.IncentiveCalculation
2. hrms.BrokerCommissionRecord -> sales.BrokerCommission
3. hrms.DailyAgencyProductionEntry -> pms.DailyProductionEntry
"""
from django.db import migrations


def copy_duplicate_hrms_records(apps, schema_editor):
    # 1. SalespersonIncentiveScheme -> pms.IncentiveScheme
    try:
        HrmsScheme = apps.get_model("hrms", "SalespersonIncentiveScheme")
        PmsScheme = apps.get_model("pms", "IncentiveScheme")
        for h_scheme in HrmsScheme.objects.all():
            if not PmsScheme.objects.filter(id=h_scheme.id).exists():
                PmsScheme.objects.create(
                    id=h_scheme.id,
                    client_id=h_scheme.client_id,
                    name=h_scheme.name,
                    basis=getattr(h_scheme, "basis", "INVOICE") or "INVOICE",
                    rate_pct=getattr(h_scheme, "rate_pct", 0) or 0,
                    frequency=getattr(h_scheme, "frequency", "MONTHLY") or "MONTHLY",
                    is_active=h_scheme.is_active,
                    created_by_id=h_scheme.created_by_id,
                )
    except LookupError:
        pass

    # 1b. SalespersonIncentiveAccrual -> pms.IncentiveCalculation
    try:
        HrmsAccrual = apps.get_model("hrms", "SalespersonIncentiveAccrual")
        PmsCalc = apps.get_model("pms", "IncentiveCalculation")
        for h_acc in HrmsAccrual.objects.all():
            if not PmsCalc.objects.filter(id=h_acc.id).exists():
                PmsCalc.objects.create(
                    id=h_acc.id,
                    client_id=h_acc.client_id,
                    scheme_id=h_acc.scheme_id,
                    employee_id=h_acc.employee_id,
                    salesperson_id=h_acc.employee_id,
                    period_label=getattr(h_acc, "period_name", "") or "MIGRATED",
                    period_start=h_acc.start_date,
                    period_end=h_acc.end_date,
                    gross_sales=getattr(h_acc, "gross_sales", 0) or 0,
                    net_sales=getattr(h_acc, "net_sales", 0) or 0,
                    incentive_amount=getattr(h_acc, "calculated_incentive", 0) or 0,
                    status=getattr(h_acc, "status", "Draft") or "Draft",
                )
    except LookupError:
        pass

    # 2. BrokerCommissionRecord -> sales.BrokerCommission
    try:
        HrmsBcr = apps.get_model("hrms", "BrokerCommissionRecord")
        SalesBc = apps.get_model("sales", "BrokerCommission")
        for h_bcr in HrmsBcr.objects.all():
            if not SalesBc.objects.filter(id=h_bcr.id).exists():
                SalesBc.objects.create(
                    id=h_bcr.id,
                    client_id=h_bcr.client_id,
                    broker_id=h_bcr.broker_id,
                    invoice_id=h_bcr.invoice_id,
                    party_id=h_bcr.party_id,
                    salesperson_id=h_bcr.salesperson_id,
                    commission_number=getattr(h_bcr, "commission_number", "") or str(h_bcr.id)[:8],
                    invoice_date=h_bcr.invoice_date,
                    invoice_amount=getattr(h_bcr, "invoice_amount", 0) or 0,
                    commission_basis=getattr(h_bcr, "commission_basis", "INVOICE") or "INVOICE",
                    commission_type=getattr(h_bcr, "commission_type", "percent") or "percent",
                    rate=getattr(h_bcr, "rate", 0) or 0,
                    commission_amount=getattr(h_bcr, "commission_amount", 0) or 0,
                    status=getattr(h_bcr, "status", "Accrued") or "Accrued",
                )
    except LookupError:
        pass

    # 3. DailyAgencyProductionEntry -> pms.DailyProductionEntry
    try:
        HrmsEntry = apps.get_model("hrms", "DailyAgencyProductionEntry")
        PmsEntry = apps.get_model("pms", "DailyProductionEntry")
        for h_ent in HrmsEntry.objects.all():
            if not PmsEntry.objects.filter(id=h_ent.id).exists():
                PmsEntry.objects.create(
                    id=h_ent.id,
                    client_id=h_ent.client_id,
                    instruction_id=h_ent.instruction_id,
                    entry_date=h_ent.entry_date,
                    meters=getattr(h_ent, "meters", 0) or 0,
                    produced_takas=getattr(h_ent, "produced_takas", 0) or 0,
                    good_meter=getattr(h_ent, "good_meter", 0) or 0,
                    reject_meter=getattr(h_ent, "reject_meter", 0) or 0,
                    reprocess_meter=getattr(h_ent, "reprocess_meter", 0) or 0,
                    entered_by_employee_id=h_ent.entered_by_employee_id,
                )
    except LookupError:
        pass


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hrms", "0006_employeeaward_employeepromotion_employeetransfer_and_more"),
        ("pms", "0001_initial"),
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(copy_duplicate_hrms_records, noop_reverse),
    ]
