"""Alerts and Exception Management Service Engine (PDF §19).

Evaluates the 13 business exception rules, manages AlertSetting thresholds,
and logs AlertIncident rows with in-app notification dispatch.

Each rule is its own ``check_*`` function. ``run_all_alert_checks`` runs them
one by one: a rule that raises is logged and skipped, so one broken rule never
stops the others and never fails silently.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.core.models import AlertIncident, AlertSetting, Notification
from apps.core.money import ZERO, round2, D

logger = logging.getLogger(__name__)

DEFAULT_ALERT_CONFIGS = [
    {
        "alert_key": "production_update_missing",
        "name": "Production Update Not Entered by EOD",
        "description": "External agency production work instruction has no daily entry entered today.",
        "threshold_days": 1,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
    {
        "alert_key": "job_delayed",
        "name": "Production / Job Order Delayed",
        "description": "Production instruction or Job Work Order is past its expected completion date.",
        "threshold_days": 0,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "agency_pending_aging",
        "name": "Material Pending with Agency Beyond Threshold",
        "description": "Fabric material dispatched to processor/agency has been pending for over N days.",
        "threshold_days": 15,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
    {
        "alert_key": "loss_tolerance_exceeded",
        "name": "Process Loss / Wastage Above Tolerance",
        "description": "Job work or production loss % exceeds configured process tolerance.",
        "threshold_days": 0,
        "threshold_pct": 3.0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
    {
        "alert_key": "qc_rejection_high",
        "name": "QC Rejection / Reprocess Above Threshold",
        "description": "Quality check rejection or reprocess rate exceeds acceptable defect limit.",
        "threshold_days": 0,
        "threshold_pct": 5.0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "negative_stock",
        "name": "Negative or Insufficient Stock",
        "description": "Item or lot stock balance is zero or negative.",
        "threshold_days": 0,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "credit_limit_exceeded",
        "name": "Customer Credit Limit / Overdue Payment",
        "description": "Customer invoice payment is overdue beyond credit days or credit limit exceeded.",
        "threshold_days": 30,
        "threshold_pct": 0,
        "threshold_amount": 10000,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
    {
        "alert_key": "below_cost_sale",
        "name": "Sale Below Cost / Minimum Margin",
        "description": "Quotation, sales order, or invoice priced below landed lot cost.",
        "threshold_days": 0,
        "threshold_pct": 5.0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
    {
        "alert_key": "large_discount",
        "name": "Large Discount / Commission Override",
        "description": "Discount exceeds salesperson authority or manual brokerage override applied.",
        "threshold_days": 0,
        "threshold_pct": 3.0,
        "threshold_amount": 5000,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "sample_overdue",
        "name": "Returnable Sample Overdue",
        "description": "Returnable fabric sample is past due date and not yet returned.",
        "threshold_days": 0,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "vendor_bill_mismatch",
        "name": "Vendor Bill Three-Way Mismatch",
        "description": "Vendor bill quantity or rate mismatch vs GRN and Purchase Order.",
        "threshold_days": 0,
        "threshold_pct": 2.0,
        "threshold_amount": 1000,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "slow_moving_stock",
        "name": "Slow-Moving Finished Inventory",
        "description": "Finished lot or taka aged over 90 days with remaining available stock.",
        "threshold_days": 90,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app"],
    },
    {
        "alert_key": "combined_grey_stock_low",
        "name": "Combined Total Grey Stock Level",
        "description": "Combined available grey fabric stock across all qualities falls below configured minimum threshold.",
        "threshold_days": 0,
        "threshold_pct": 0,
        "threshold_amount": 0,
        "is_enabled": True,
        "channels": ["in_app", "email"],
    },
]

_DEFAULTS_BY_KEY = {conf["alert_key"]: conf for conf in DEFAULT_ALERT_CONFIGS}

# Production instruction statuses where production is still expected to run.
_PI_ACTIVE_STATUSES = ["Not Started", "Running", "In Progress", "Partially Completed"]
# Production is finished from "Completed" on; later steps are verification and PO.
_PI_DONE_STATUSES = [
    "Completed", "Verification Pending", "Verified", "PO Pending", "PO Created", "Closed", "Cancelled",
]
# Job work: material is back with the company from "Received" on.
_JWO_RETURNED_STATUSES = ["Received", "QC Pending", "Completed", "Closed", "Cancelled"]
_JWO_OUT_STATUSES = ["Sent", "In-Process", "Partially Received", "Reprocess"]


def ensure_alert_settings(client):
    """Ensure all default alert settings exist for a tenant."""
    for conf in DEFAULT_ALERT_CONFIGS:
        AlertSetting.objects.get_or_create(
            client=client,
            alert_key=conf["alert_key"],
            defaults={
                "name": conf["name"],
                "description": conf["description"],
                "threshold_days": conf["threshold_days"],
                "threshold_pct": Decimal(str(conf["threshold_pct"])),
                "threshold_amount": Decimal(str(conf["threshold_amount"])),
                "is_enabled": conf["is_enabled"],
                "channels": conf["channels"],
            },
        )


def get_thresholds(client_id, alert_key):
    """The tenant's configured thresholds for a rule, falling back to the defaults.

    Returns ``(days, pct, amount)`` as ``(int, Decimal, Decimal)``.
    """
    conf = _DEFAULTS_BY_KEY.get(alert_key, {})
    days = conf.get("threshold_days", 0)
    pct = D(str(conf.get("threshold_pct", 0)))
    amount = D(str(conf.get("threshold_amount", 0)))
    setting = AlertSetting.objects.filter(client_id=client_id, alert_key=alert_key).first()
    if setting is not None:
        if setting.threshold_days is not None:
            days = setting.threshold_days
        if setting.threshold_pct is not None:
            pct = D(setting.threshold_pct)
        if setting.threshold_amount is not None:
            amount = D(setting.threshold_amount)
    return int(days or 0), pct, amount


def trigger_incident(
    client_id,
    alert_key,
    severity,
    title,
    message,
    entity_type="",
    entity_id="",
    entity_number="",
    metadata=None,
):
    """Create an AlertIncident and send in-app notification."""
    setting = AlertSetting.objects.filter(client_id=client_id, alert_key=alert_key).first()
    if setting and not setting.is_enabled:
        return None

    # Check if active incident already exists for this exact entity to avoid duplicate spam
    existing = AlertIncident.objects.filter(
        client_id=client_id,
        alert_key=alert_key,
        entity_type=entity_type,
        entity_id=str(entity_id),
        status="Active",
    ).first()
    if existing:
        existing.severity = severity
        existing.title = title
        existing.message = message
        if metadata:
            existing.metadata = metadata
        existing.save(update_fields=["severity", "title", "message", "metadata", "updated_at"])
        return existing

    incident = AlertIncident.objects.create(
        client_id=client_id,
        alert_key=alert_key,
        severity=severity,
        title=title,
        message=message,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_number=str(entity_number),
        status="Active",
        metadata=metadata or {},
    )

    # In-app notification fanout
    try:
        from apps.accounts.models import User
        recipients = User.objects.filter(client_id=client_id, status="Active", is_staff=True)[:10]
        for user in recipients:
            Notification.objects.create(
                client_id=client_id,
                recipient=user,
                type="system_alert",
                category="system",
                title=f"[{severity}] {title}",
                body=message,
                entity_type=entity_type,
                entity_id=incident.id,
            )
    except Exception:
        # The incident itself is saved; a failed notification must not undo it.
        logger.exception("Alert %s: notification fanout failed for incident %s", alert_key, incident.id)

    return incident


def _collect(incidents, inc):
    if inc:
        incidents.append(inc)


# ---------------------------------------------------------------------------
# The rules (PDF §19). Each returns the incidents it created or refreshed.
# ---------------------------------------------------------------------------
def check_production_update_missing(client_id, as_of):
    """1. External agency production update not entered by end of day."""
    from apps.pms.models import ProductionInstruction, DailyProductionEntry

    incidents = []
    active_instructions = ProductionInstruction.objects.filter(
        client_id=client_id,
        status__in=_PI_ACTIVE_STATUSES,
        deleted_at__isnull=True,
    ).select_related("agency")
    for pi in active_instructions:
        has_entry_today = DailyProductionEntry.objects.filter(
            client_id=client_id,
            instruction=pi,
            entry_date=as_of,
            deleted_at__isnull=True,
        ).exists()
        if not has_entry_today:
            _collect(incidents, trigger_incident(
                client_id=client_id,
                alert_key="production_update_missing",
                severity="Warning",
                title=f"No Production Entry Today for {pi.instruction_number}",
                message=f"Agency {pi.agency.name if pi.agency else pi.agency_name} has not submitted a daily production entry for {as_of}.",
                entity_type="ProductionInstruction",
                entity_id=pi.id,
                entity_number=pi.instruction_number,
            ))
    return incidents


def check_job_delayed(client_id, as_of):
    """2. Production/job order delayed beyond expected date."""
    from apps.pms.models import ProductionInstruction
    from apps.jobwork.models import JobWorkOrder

    incidents = []
    grace_days, _, _ = get_thresholds(client_id, "job_delayed")
    cutoff = as_of - timedelta(days=grace_days)

    delayed_pi = ProductionInstruction.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        expected_completion_date__lt=cutoff,
    ).exclude(status__in=_PI_DONE_STATUSES)
    for pi in delayed_pi:
        days = (as_of - pi.expected_completion_date).days
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="job_delayed",
            severity="Critical" if days > 7 else "Warning",
            title=f"Production Instruction Delayed: {pi.instruction_number}",
            message=f"Instruction is {days} days past expected completion date ({pi.expected_completion_date}). Current status: {pi.status}.",
            entity_type="ProductionInstruction",
            entity_id=pi.id,
            entity_number=pi.instruction_number,
        ))

    delayed_jwo = JobWorkOrder.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        expected_completion__lt=cutoff,
    ).exclude(status__in=_JWO_RETURNED_STATUSES).select_related("vendor")
    for jwo in delayed_jwo:
        days = (as_of - jwo.expected_completion).days
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="job_delayed",
            severity="Critical" if days > 7 else "Warning",
            title=f"Job Work Order Delayed: {jwo.jwo_no}",
            message=f"Order with processor {jwo.vendor.name if jwo.vendor else jwo.vendor_name or 'Unknown'} is {days} days overdue (due {jwo.expected_completion}).",
            entity_type="JobWorkOrder",
            entity_id=jwo.id,
            entity_number=jwo.jwo_no,
        ))
    return incidents


def check_agency_pending_aging(client_id, as_of):
    """3. Material pending with agency/processor beyond configured days."""
    from apps.jobwork.models import JobWorkOrder

    incidents = []
    max_days, _, _ = get_thresholds(client_id, "agency_pending_aging")
    aging_jwo = JobWorkOrder.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        status__in=_JWO_OUT_STATUSES,
    ).select_related("vendor")
    for jwo in aging_jwo:
        order_date = jwo.order_date or jwo.created_at.date()
        days_pending = (as_of - order_date).days
        if days_pending >= max_days:
            _collect(incidents, trigger_incident(
                client_id=client_id,
                alert_key="agency_pending_aging",
                severity="Warning",
                title=f"Material Pending with Processor {days_pending} Days: {jwo.jwo_no}",
                message=f"Material on JWO {jwo.jwo_no} has been with {jwo.vendor.name if jwo.vendor else jwo.vendor_name or 'Processor'} for {days_pending} days.",
                entity_type="JobWorkOrder",
                entity_id=jwo.id,
                entity_number=jwo.jwo_no,
            ))
    return incidents


def _jwo_quantities(jwo):
    """Sent, received and approved-loss metres for one job work order."""
    sent = sum((D(o.qty or 0) for o in jwo.outwards.filter(deleted_at__isnull=True)), ZERO)
    received = sum((D(i.qty or 0) for i in jwo.inwards.filter(deleted_at__isnull=True)), ZERO)
    approved_loss = sum(
        (D(l.qty or 0) for l in jwo.loss_entries.filter(deleted_at__isnull=True, status="Approved")),
        ZERO,
    )
    return sent, received, approved_loss


def check_loss_tolerance_exceeded(client_id, as_of):
    """4. Loss/wastage higher than tolerance.

    Loss is only known once nothing is still out with the processor, so a
    partial inward never raises this. An order is evaluated on the day it gets
    an inward, once it is fully returned (by status, or received + approved
    loss covers what was sent). PDF §08: Loss % = (Sent - Returned) / Sent.
    """
    from apps.jobwork.models import JobWorkOrder

    incidents = []
    _, default_pct, _ = get_thresholds(client_id, "loss_tolerance_exceeded")
    orders = JobWorkOrder.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        inwards__date=as_of,
        inwards__deleted_at__isnull=True,
    ).distinct().select_related("process_master")
    for jwo in orders:
        sent, received, approved_loss = _jwo_quantities(jwo)
        if sent <= ZERO:
            continue
        fully_returned = jwo.status in _JWO_RETURNED_STATUSES or received + approved_loss >= sent
        if not fully_returned:
            continue
        loss_pct = round2((sent - received) / sent * D(100))
        tolerance = D(jwo.expected_loss_pct or 0)
        if tolerance <= ZERO and jwo.process_master is not None:
            tolerance = D(jwo.process_master.tolerance_pct or jwo.process_master.expected_loss_pct or 0)
        if tolerance <= ZERO:
            tolerance = default_pct
        if loss_pct > tolerance:
            _collect(incidents, trigger_incident(
                client_id=client_id,
                alert_key="loss_tolerance_exceeded",
                severity="Critical",
                title=f"Excess Process Loss on JWO {jwo.jwo_no} ({loss_pct}%)",
                message=(
                    f"Sent {sent}m, received {received}m: loss {loss_pct}% exceeds the "
                    f"tolerance of {tolerance}% on JWO {jwo.jwo_no}."
                ),
                entity_type="JobWorkOrder",
                entity_id=jwo.id,
                entity_number=jwo.jwo_no,
                metadata={
                    "sent": str(sent), "received": str(received),
                    "approvedLoss": str(approved_loss), "lossPct": str(loss_pct),
                    "tolerancePct": str(tolerance),
                },
            ))
    return incidents


def check_qc_rejection_high(client_id, as_of):
    """5. QC rejection or reprocess above threshold."""
    from apps.inventory.models import QCInspection

    incidents = []
    _, max_pct, _ = get_thresholds(client_id, "qc_rejection_high")
    inspections = QCInspection.objects.filter(
        client_id=client_id, inspection_date=as_of, deleted_at__isnull=True
    )
    for qc in inspections:
        lines = qc.lines.filter(deleted_at__isnull=True)
        total_takas = lines.count()
        if total_takas > 0:
            failed = lines.filter(disposition__in=["Reject", "Reprocess"]).count()
            reject_pct = round2(D(failed) / D(total_takas) * D(100))
            detail = f"{failed} of {total_takas} takas"
        else:
            # Inspections recorded in bulk carry quantities, not lines.
            inspected = D(qc.total_inspected_qty or 0)
            if inspected <= ZERO:
                continue
            failed_qty = D(qc.rejected_qty or 0) + D(qc.reprocess_qty or 0)
            reject_pct = round2(failed_qty / inspected * D(100))
            detail = f"{failed_qty}m of {inspected}m"
        if reject_pct > max_pct:
            _collect(incidents, trigger_incident(
                client_id=client_id,
                alert_key="qc_rejection_high",
                severity="Critical",
                title=f"High QC Rejection/Reprocess ({reject_pct}%): {qc.inspection_number}",
                message=f"{detail} failed inspection (Reject/Reprocess) on {qc.inspection_number}; limit is {max_pct}%.",
                entity_type="QCInspection",
                entity_id=qc.id,
                entity_number=qc.inspection_number,
            ))
    return incidents


def check_negative_stock(client_id, as_of):
    """6. Negative or insufficient stock."""
    from apps.inventory.models import StockBalance

    incidents = []
    negative_balances = StockBalance.objects.filter(
        client_id=client_id, on_hand__lt=0, deleted_at__isnull=True
    ).select_related("item", "location")
    for b in negative_balances[:10]:
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="negative_stock",
            severity="Critical",
            title=f"Negative Stock: {b.item.name} at {b.location.name}",
            message=f"On-hand inventory is {b.on_hand} {b.item.uom or 'm'}.",
            entity_type="StockBalance",
            entity_id=b.id,
            entity_number=b.item.sku or b.item.name,
        ))
    return incidents


def check_credit_limit_exceeded(client_id, as_of):
    """7. Customer payment overdue, or credit limit exceeded."""
    from apps.masters.models import Party
    from apps.sales.models import SalesInvoice
    from apps.sales.services import party_credit_exposure

    incidents = []
    critical_days, _, _ = get_thresholds(client_id, "credit_limit_exceeded")

    overdue_invoices = SalesInvoice.objects.filter(
        client_id=client_id,
        status__in=["Unpaid", "Partially Paid"],
        deleted_at__isnull=True,
        due_date__lt=as_of,
    ).select_related("party")
    for inv in overdue_invoices[:10]:
        days = (as_of - inv.due_date).days
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="credit_limit_exceeded",
            severity="Critical" if days > critical_days else "Warning",
            title=f"Payment Overdue: {inv.invoice_number} ({days} days)",
            message=f"Customer {inv.party.name if inv.party else inv.party_name} has outstanding invoice {inv.invoice_number} of {inv.total} overdue by {days} days.",
            entity_type="SalesInvoice",
            entity_id=inv.id,
            entity_number=inv.invoice_number,
        ))

    parties = Party.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        credit_limit__gt=0,
    ).exclude(type__in=["Vendor", "Processor"])
    for party in parties:
        exposure, open_orders = party_credit_exposure(party)
        limit = D(party.credit_limit)
        if exposure > limit:
            _collect(incidents, trigger_incident(
                client_id=client_id,
                alert_key="credit_limit_exceeded",
                severity="Critical",
                title=f"Credit Limit Exceeded: {party.name}",
                message=(
                    f"Exposure {exposure} (balance {round2(party.balance)} + open orders "
                    f"{round2(open_orders)}) is above the credit limit of {round2(limit)}."
                ),
                entity_type="Party",
                entity_id=party.id,
                entity_number=party.code or party.name,
                metadata={"exposure": str(exposure), "creditLimit": str(round2(limit))},
            ))
    return incidents


def check_below_cost_sale(client_id, as_of):
    """8. Sales below cost / minimum margin."""
    from apps.sales.models import SalesOrderLine

    incidents = []
    recent_order_lines = SalesOrderLine.objects.filter(
        Q(below_cost=True) | Q(below_margin=True),
        sales_order__client_id=client_id,
        sales_order__doc_date__gte=as_of - timedelta(days=7),
        deleted_at__isnull=True,
    ).select_related("sales_order", "item")
    for sol in recent_order_lines[:5]:
        item_name = sol.item.name if sol.item else (sol.item_name or "Item")
        if sol.below_cost:
            severity = "Critical"
            title = f"Below Landed Cost Sale on Order {sol.sales_order.order_number}"
        else:
            severity = "Warning"
            title = f"Below Minimum Margin Sale on Order {sol.sales_order.order_number}"
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="below_cost_sale",
            severity=severity,
            title=title,
            message=f"Item {item_name} sold at rate ₹{sol.rate} against landed cost ₹{sol.landed_cost} (margin {sol.margin_pct}%).",
            entity_type="SalesOrder",
            entity_id=sol.sales_order_id,
            entity_number=sol.sales_order.order_number,
        ))
    return incidents


def check_large_discount(client_id, as_of):
    """9. Large discount on an invoice."""
    from apps.sales.models import SalesInvoice

    incidents = []
    _, max_pct, _ = get_thresholds(client_id, "large_discount")
    discounted = SalesInvoice.objects.filter(
        client_id=client_id,
        doc_date__gte=as_of - timedelta(days=7),
        total_discount__gt=0,
        deleted_at__isnull=True,
    )
    for inv in discounted[:5]:
        sub = D(inv.subtotal or 0)
        disc = D(inv.total_discount or 0)
        if sub > ZERO:
            pct = round2(disc / sub * D(100))
            if pct >= max_pct:
                _collect(incidents, trigger_incident(
                    client_id=client_id,
                    alert_key="large_discount",
                    severity="Warning",
                    title=f"Large Discount ({pct}%) on Invoice {inv.invoice_number}",
                    message=f"Total discount ₹{disc} represents {pct}% of subtotal ₹{sub}; limit is {max_pct}%.",
                    entity_type="SalesInvoice",
                    entity_id=inv.id,
                    entity_number=inv.invoice_number,
                ))
    return incidents


def check_sample_overdue(client_id, as_of):
    """10. Returnable sample overdue."""
    from apps.inventory.models import Sample

    incidents = []
    grace_days, _, _ = get_thresholds(client_id, "sample_overdue")
    overdue_samples = Sample.objects.filter(
        client_id=client_id,
        is_returnable=True,
        status__in=["Issued", "Partially Returned", "Overdue"],
        due_date__lt=as_of - timedelta(days=grace_days),
        deleted_at__isnull=True,
    ).select_related("party")
    for sm in overdue_samples:
        if sm.status == "Issued":
            sm.status = "Overdue"
            sm.save(update_fields=["status", "updated_at"])
        days = (as_of - sm.due_date).days
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="sample_overdue",
            severity="Warning",
            title=f"Sample Overdue ({days} days): {sm.sample_number}",
            message=f"Sample {sm.sample_number} issued to {sm.party.name if sm.party else 'Customer'} was due on {sm.due_date}.",
            entity_type="Sample",
            entity_id=sm.id,
            entity_number=sm.sample_number,
        ))
    return incidents


def check_vendor_bill_mismatch(client_id, as_of):
    """11. Vendor bill mismatch against PO and GRN (three-way match)."""
    from apps.purchase.models import VendorBill
    from apps.purchase.services import compute_vendor_bill_matching

    incidents = []
    _, _, critical_amount = get_thresholds(client_id, "vendor_bill_mismatch")
    pending_bills = VendorBill.objects.filter(
        client_id=client_id,
        status="Pending",
        deleted_at__isnull=True,
    )
    for vb in pending_bills[:5]:
        result = compute_vendor_bill_matching(client_id, vb)
        if result.get("overallStatus") != "Mismatch":
            continue
        summary = result.get("summary") or {}
        amount_diff = D(str(summary.get("amountDiff", 0)))
        reasons = result.get("reasons") or []
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="vendor_bill_mismatch",
            severity="Critical" if abs(amount_diff) > critical_amount else "Warning",
            title=f"Vendor Bill Mismatch on {vb.vendor_bill_number}",
            message=(
                f"Bill {vb.vendor_bill_number} does not match PO {result.get('poNumber') or '-'} / "
                f"GRN {result.get('grnNumber') or '-'}. "
                + ("; ".join(reasons[:3]) if reasons else f"Amount difference ₹{amount_diff}.")
            ),
            entity_type="VendorBill",
            entity_id=vb.id,
            entity_number=vb.vendor_bill_number,
            metadata={"summary": summary, "reasons": reasons},
        ))
    return incidents


def check_slow_moving_stock(client_id, as_of):
    """12. Slow-moving finished stock."""
    from apps.inventory.models import LotTaka

    incidents = []
    max_age, _, _ = get_thresholds(client_id, "slow_moving_stock")
    old_takas = LotTaka.objects.filter(
        client_id=client_id,
        status="Finished",
        available_quantity__gt=0,
        received_date__lte=as_of - timedelta(days=max_age),
        deleted_at__isnull=True,
    )[:5]
    for t in old_takas:
        age = (as_of - (t.received_date or t.created_at.date())).days
        _collect(incidents, trigger_incident(
            client_id=client_id,
            alert_key="slow_moving_stock",
            severity="Info",
            title=f"Slow-Moving Stock ({age} days): Taka {t.taka_number} (Lot {t.lot_number})",
            message=f"Finished fabric has {t.available_quantity}m remaining, received {age} days ago.",
            entity_type="LotTaka",
            entity_id=t.id,
            entity_number=t.taka_number or t.lot_number,
        ))
    return incidents


def check_combined_grey_stock_low(client_id, as_of):
    """13. Combined grey fabric stock below minimum threshold."""
    from apps.inventory.services import evaluate_combined_grey_stock_alert

    incidents = []
    result = evaluate_combined_grey_stock_alert(client_id)
    _collect(incidents, result.get("incident"))
    return incidents


ALERT_CHECKS = [
    ("production_update_missing", check_production_update_missing),
    ("job_delayed", check_job_delayed),
    ("agency_pending_aging", check_agency_pending_aging),
    ("loss_tolerance_exceeded", check_loss_tolerance_exceeded),
    ("qc_rejection_high", check_qc_rejection_high),
    ("negative_stock", check_negative_stock),
    ("credit_limit_exceeded", check_credit_limit_exceeded),
    ("below_cost_sale", check_below_cost_sale),
    ("large_discount", check_large_discount),
    ("sample_overdue", check_sample_overdue),
    ("vendor_bill_mismatch", check_vendor_bill_mismatch),
    ("slow_moving_stock", check_slow_moving_stock),
    ("combined_grey_stock_low", check_combined_grey_stock_low),
]


def run_all_alert_checks(client_id, as_of=None, errors=None):
    """Execute all 13 alert checks for a given tenant.

    A check that raises is logged with its traceback and skipped. Pass a list
    as ``errors`` to get back ``{"check": key, "error": text}`` for each one.
    """
    as_of = as_of or timezone.localdate()
    incidents = []
    for key, check in ALERT_CHECKS:
        try:
            incidents.extend(check(client_id, as_of))
        except Exception as exc:
            logger.exception("Alert check %s failed for client %s", key, client_id)
            if errors is not None:
                errors.append({"check": key, "error": f"{type(exc).__name__}: {exc}"})
    return incidents
