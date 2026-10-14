"""
Tenant snapshot export / import (api.md §3.4).

Replaces ``exportDatabaseSnapshot()`` / ``importDatabaseSnapshot()``, which
today serialise the whole of ``localStorage``.

The export is a plain JSON document of the tenant's own rows, in dependency
order (db.md §14.1 step 4) so an import can replay it. Import is deliberately
**additive and non-destructive**: it will not overwrite an existing row,
because a restore that silently clobbers live financial data is worse than one
that refuses.
"""
from django.apps import apps
from django.db import transaction

from .audit import _jsonable, record_audit

#: Dependency order. A restore replays this list top to bottom; an export
#: writes it in the same order so the file is readable as a build sequence.
SNAPSHOT_MODELS = [
    "accounts.Role",
    "accounts.User",
    "accounting.Account",
    "accounting.BankAccount",
    "accounting.ExpenseCategory",
    "masters.Unit",
    "masters.Location",
    "masters.Fabric",
    "masters.ItemCategory",
    "masters.CategoryCustomField",
    "masters.Item",
    "masters.CategoryPart",
    "masters.ItemPart",
    "masters.Party",
    "masters.PartyContact",
    "masters.ItemSerial",
    "sales.Quotation",
    "sales.QuotationLine",
    "sales.SalesOrder",
    "sales.SalesOrderLine",
    "sales.ProformaInvoice",
    "sales.ProformaInvoiceLine",
    "sales.DeliveryChallan",
    "sales.DeliveryChallanLine",
    "sales.SalesInvoice",
    "sales.SalesInvoiceLine",
    "sales.PaymentIn",
    "sales.PaymentAllocation",
    "sales.SalesReturn",
    "sales.SalesReturnLine",
    "sales.WarrantyCard",
    "sales.WarrantyCardItem",
    "purchase.PurchaseOrder",
    "purchase.PurchaseOrderLine",
    "purchase.PurchaseBill",
    "purchase.PurchaseBillLine",
    "purchase.VendorBill",
    "purchase.VendorBillLine",
    "purchase.GoodsReceipt",
    "purchase.GoodsReceiptLine",
    "purchase.PaymentOut",
    "purchase.PurchaseReturn",
    "purchase.PurchaseReturnLine",
    "purchase.Expense",
    "inventory.StockMovement",
    "inventory.StockBalance",
    "inventory.StockTransfer",
    "inventory.StockTransferLine",
    "inventory.FaultyPart",
    "inventory.ServiceUsage",
    "inventory.ZoneRequest",
    "inventory.ZoneRequestLine",
    "inventory.StockAudit",
    "inventory.StockAuditLine",
    "inventory.QualityStandard",
    "accounting.JournalEntry",
    "accounting.JournalLine",
    "accounting.Budget",
    "accounting.BankTransfer",
    "crm.Stage",
    "crm.DealStage",
    "crm.Source",
    "crm.Industry",
    "crm.LostReason",
    "crm.Lead",
    "crm.MasterTask",
    "crm.StageTask",
    "crm.Task",
    "crm.Deal",
    "crm.Contract",
    "crm.CrmProject",
    "crm.Form",
    "pms.Department",
    "pms.StageConfig",
    "pms.Project",
    "pms.ProjectStage",
    "pms.Task",
    "pms.Document",
    "pms.Approval",
    "pms.Delay",
    "pms.Settings",
    "hrms.Department",
    "hrms.Designation",
    "hrms.Location",
    "hrms.SalaryStructure",
    "hrms.Employee",
    "hrms.Attendance",
    "hrms.LeaveType",
    "hrms.LeaveRequest",
    "hrms.LeaveBalance",
    "hrms.PayrollRun",
    "hrms.Payslip",
    "hrms.Holiday",
    "hrms.WorkingDay",
]

#: Never exported. Credentials, tokens and replay caches are not backup data.
EXCLUDED_FIELDS = {"password", "token_hash", "refresh_token_hash", "request_hash"}


def export_snapshot(client_id):
    """A JSON document of this tenant's rows, newest schema first."""
    from django.utils import timezone

    tables = {}
    for label in SNAPSHOT_MODELS:
        model = apps.get_model(label)
        if not hasattr(model, "client_id"):
            continue
        rows = []
        for instance in model.objects.filter(client_id=client_id).iterator(chunk_size=500):
            row = {}
            for field in instance._meta.concrete_fields:
                if field.name in EXCLUDED_FIELDS:
                    continue
                row[field.attname] = _jsonable(getattr(instance, field.attname, None))
            rows.append(row)
        if rows:
            tables[label] = rows

    return {
        "version": 1,
        "exportedAt": timezone.now().isoformat(),
        "clientId": str(client_id),
        "tableOrder": [label for label in SNAPSHOT_MODELS if label in tables],
        "tables": tables,
    }


@transaction.atomic
def import_snapshot(client_id, snapshot, *, user=None):
    """Replay a snapshot into this tenant.

    Additive only: existing primary keys are skipped rather than overwritten,
    and every row is re-stamped with the target tenant so a snapshot cannot be
    used to write into someone else's data.
    """
    tables = snapshot.get("tables") or {}
    order = snapshot.get("tableOrder") or SNAPSHOT_MODELS

    created = {}
    skipped = {}
    for label in order:
        rows = tables.get(label)
        if not rows:
            continue
        model = apps.get_model(label)
        field_names = {field.attname for field in model._meta.concrete_fields}

        existing_ids = set(
            str(value) for value in model.objects.values_list("pk", flat=True)
        )
        to_create = []
        for row in rows:
            payload = {key: value for key, value in row.items() if key in field_names}
            payload["client_id"] = client_id  # never trust the snapshot's tenant
            pk = str(payload.get("id") or "")
            if pk and pk in existing_ids:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            to_create.append(model(**payload))

        if to_create:
            model.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            created[label] = len(to_create)

    record_audit(
        client=client_id,
        actor=user,
        action="restore",
        entity_type="Client",
        entity_id=None,
        description=f"Snapshot restored: {sum(created.values())} rows created",
        after={"created": created, "skipped": skipped},
    )
    return {"created": created, "skipped": skipped}
