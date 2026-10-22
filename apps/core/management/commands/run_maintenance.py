"""
The scheduled jobs of db.md §13.

Run from cron / a scheduler:

    python manage.py run_maintenance --job hourly     # every hour
    python manage.py run_maintenance --job nightly    # nightly
    python manage.py run_maintenance --job all

db.md §13 is explicit that reconciliation jobs **alert, not auto-correct**:
"A silent correction hides the bug that caused it." So the reconcilers here
report drift and exit non-zero; they never write.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

MONEY = DecimalField(max_digits=18, decimal_places=2)
QTY = DecimalField(max_digits=18, decimal_places=4)


class Command(BaseCommand):
    help = "Run the scheduled maintenance and reconciliation jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--job",
            default="all",
            choices=["hourly", "nightly", "all"] + list(JOBS),
            help="Which job (or group) to run.",
        )

    def handle(self, *args, **options):
        requested = options["job"]
        if requested in ("hourly", "nightly", "all"):
            names = [
                name
                for name, (group, _) in JOBS.items()
                if requested == "all" or group == requested
            ]
        else:
            names = [requested]

        drift = 0
        for name in names:
            _, handler = JOBS[name]
            result = handler(self)
            status = self.style.SUCCESS("ok")
            if result.get("drift"):
                drift += result["drift"]
                status = self.style.ERROR("DRIFT")
            self.stdout.write(f"  {status}  {name}: {result['message']}")

        if drift:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{drift} inconsistenc{'y' if drift == 1 else 'ies'} found. "
                    "Nothing was auto-corrected (db.md §13)."
                )
            )
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Hourly
# ---------------------------------------------------------------------------
def expire_shares(command):
    """``quotation_shares`` and ``pms_proof_shares`` past ``expires_at``."""
    from apps.pms.models import ProofShare

    now = timezone.now()
    proofs = ProofShare.objects.filter(
        status="Active", expires_at__lt=now, deleted_at__isnull=True
    ).update(status="Expired")
    return {"message": f"{proofs} proof share(s) expired"}


def sweep_pending_files(command):
    from apps.core.files import sweep_pending

    removed = sweep_pending(older_than_hours=24)
    return {"message": f"{removed} orphaned upload(s) removed"}


def sweep_idempotency_keys(command):
    from apps.core.idempotency import sweep_expired

    removed = sweep_expired()
    return {"message": f"{removed} expired idempotency key(s) removed"}


# ---------------------------------------------------------------------------
# Nightly reconciliation -- alert, never correct
# ---------------------------------------------------------------------------
def reconcile_stock(command):
    """Re-derive on-hand from ``stock_movements`` and compare with balances."""
    from apps.inventory.models import StockBalance, StockMovement

    mismatches = []
    derived = {}
    rows = StockMovement.objects.exclude(type="FAULTY").values(
        "client_id", "item_id", "location_id", "quantity", "weighed_qty"
    )
    for row in rows.iterator(chunk_size=2000):
        key = (row["client_id"], row["item_id"], row["location_id"])
        effective = row["weighed_qty"] if row["weighed_qty"] is not None else row["quantity"]
        derived[key] = derived.get(key, Decimal("0")) + effective

    for balance in StockBalance.objects.all().iterator(chunk_size=1000):
        key = (balance.client_id, balance.item_id, balance.location_id)
        expected = derived.pop(key, Decimal("0"))
        if abs((balance.on_hand or Decimal("0")) - expected) > Decimal("0.0001"):
            mismatches.append(
                f"item {balance.item_id} @ {balance.location_id}: "
                f"stored {balance.on_hand} vs ledger {expected}"
            )

    for key, expected in derived.items():
        if abs(expected) > Decimal("0.0001"):
            mismatches.append(f"missing balance row for {key}: ledger {expected}")

    for line in mismatches[:20]:
        command.stdout.write(command.style.WARNING(f"      {line}"))
    return {
        "message": f"{len(mismatches)} stock mismatch(es)",
        "drift": len(mismatches),
    }


def reconcile_party_balances(command):
    """Re-derive ``parties.balance`` from ``journal_lines``."""
    from apps.accounting.models import JournalLine
    from apps.masters.models import Party

    mismatches = []
    for party in Party.objects.filter(deleted_at__isnull=True).iterator(chunk_size=500):
        rows = JournalLine.objects.filter(
            client_id=party.client_id, party_id=party.id, deleted_at__isnull=True
        ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False).aggregate(
            debit=Coalesce(Sum("debit"), Value(Decimal("0.00")), output_field=MONEY),
            credit=Coalesce(Sum("credit"), Value(Decimal("0.00")), output_field=MONEY),
        )
        expected = (party.opening_balance or Decimal("0")) + rows["debit"] - rows["credit"]
        if abs((party.balance or Decimal("0")) - expected) > Decimal("0.01"):
            mismatches.append(
                f"{party.code} {party.name}: stored {party.balance} vs ledger {expected}"
            )

    for line in mismatches[:20]:
        command.stdout.write(command.style.WARNING(f"      {line}"))
    return {
        "message": f"{len(mismatches)} party balance mismatch(es)",
        "drift": len(mismatches),
    }


def reconcile_document_totals(command):
    """Re-sum lines and compare with the stored header totals."""
    from apps.purchase.models import PurchaseBill, PurchaseOrder
    from apps.sales.models import (
        DeliveryChallan,
        Quotation,
        SalesInvoice,
        SalesOrder,
    )

    mismatches = []
    for model in (Quotation, SalesOrder, DeliveryChallan, SalesInvoice,
                  PurchaseOrder, PurchaseBill):
        for document in model.objects.filter(deleted_at__isnull=True).iterator(
            chunk_size=500
        ):
            lines = document.line_items.filter(deleted_at__isnull=True)
            subtotal = lines.aggregate(
                value=Coalesce(Sum("amount"), Value(Decimal("0.00")), output_field=MONEY)
            )["value"]
            if abs((document.subtotal or Decimal("0")) - subtotal) > Decimal("0.01"):
                mismatches.append(
                    f"{model.__name__} {document.pk}: "
                    f"header {document.subtotal} vs lines {subtotal}"
                )

    for line in mismatches[:20]:
        command.stdout.write(command.style.WARNING(f"      {line}"))
    return {
        "message": f"{len(mismatches)} document total mismatch(es)",
        "drift": len(mismatches),
    }


def reconcile_serials(command):
    """db.md §4.3 -- for a Serial-tracked item,
    ``count(available|reserved) == on_hand``."""
    from apps.inventory.models import StockBalance
    from apps.masters.models import Item, ItemSerial

    mismatches = []
    for item in Item.objects.filter(
        tracking_mode="Serial", deleted_at__isnull=True
    ).iterator(chunk_size=500):
        on_hand = StockBalance.objects.filter(item=item).aggregate(
            value=Coalesce(Sum("on_hand"), Value(Decimal("0.0000")), output_field=QTY)
        )["value"]
        counted = ItemSerial.objects.filter(
            item=item, status__in=["available", "reserved"], deleted_at__isnull=True
        ).count()
        if abs(on_hand - Decimal(counted)) > Decimal("0.0001"):
            mismatches.append(
                f"{item.sku}: {counted} serial(s) vs on_hand {on_hand}"
            )

    for line in mismatches[:20]:
        command.stdout.write(command.style.WARNING(f"      {line}"))
    return {
        "message": f"{len(mismatches)} serial count mismatch(es)",
        "drift": len(mismatches),
    }


def accrue_leave(command):
    """db.md §13 -- write ``hrms_leave_balances`` per ``leave_types.accrual``."""
    from apps.hrms.models import Employee, LeaveBalance, LeaveType

    year = timezone.localdate().year
    created = 0
    for leave_type in LeaveType.objects.filter(deleted_at__isnull=True):
        if leave_type.accrual == "None":
            continue
        employees = Employee.objects.filter(
            client_id=leave_type.client_id,
            status__in=["Active", "On Leave", "Probation"],
            deleted_at__isnull=True,
        )
        for employee in employees.iterator(chunk_size=500):
            _, was_created = LeaveBalance.objects.get_or_create(
                client_id=leave_type.client_id,
                employee=employee,
                leave_type=leave_type,
                period_year=year,
                defaults={"entitlement": leave_type.annual_entitlement},
            )
            created += int(was_created)
    return {"message": f"{created} leave balance row(s) opened for {year}"}


def retain_user_locations(command):
    """db.md §9.5 -- retain 90 days of field-user pings."""
    from apps.crm.models import UserLocation

    cutoff = timezone.now() - timedelta(days=90)
    deleted, _ = UserLocation.objects.filter(recorded_at__lt=cutoff).delete()
    return {"message": f"{deleted} location ping(s) purged"}


JOBS = {
    "expire_shares": ("hourly", expire_shares),
    "sweep_pending_files": ("hourly", sweep_pending_files),
    "sweep_idempotency_keys": ("hourly", sweep_idempotency_keys),
    "reconcile_stock": ("nightly", reconcile_stock),
    "reconcile_party_balances": ("nightly", reconcile_party_balances),
    "reconcile_document_totals": ("nightly", reconcile_document_totals),
    "reconcile_serials": ("nightly", reconcile_serials),
    "accrue_leave": ("nightly", accrue_leave),
    "retain_user_locations": ("nightly", retain_user_locations),
}
