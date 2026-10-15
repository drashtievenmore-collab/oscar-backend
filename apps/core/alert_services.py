"""Alerts and Exception Management Service Engine (PDF §19).

Evaluates the 12 business exception rules, manages AlertSetting thresholds,
and logs AlertIncident rows with in-app notification dispatch.
"""
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from apps.core.models import AlertIncident, AlertSetting, Notification
from apps.core.money import ZERO, round2, D

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
]


def ensure_alert_settings(client):
    """Ensure all 12 default alert settings exist for a tenant."""
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
        pass

    return incident


def run_all_alert_checks(client_id, as_of=None):
    """Execute all 12 alert checks for a given tenant."""
    as_of = as_of or timezone.localdate()
    incidents = []

    # 1. External agency production update not entered by end of day
    try:
        from apps.pms.models import ProductionInstruction, DailyProductionEntry
        active_instructions = ProductionInstruction.objects.filter(
            client_id=client_id,
            status__in=["Not Started", "Running", "Partially Completed"],
            deleted_at__isnull=True,
        )
        for pi in active_instructions:
            has_entry_today = DailyProductionEntry.objects.filter(
                client_id=client_id,
                instruction=pi,
                entry_date=as_of,
                deleted_at__isnull=True,
            ).exists()
            if not has_entry_today:
                inc = trigger_incident(
                    client_id=client_id,
                    alert_key="production_update_missing",
                    severity="Warning",
                    title=f"No Production Entry Today for {pi.instruction_number}",
                    message=f"Agency {pi.agency.name if pi.agency else pi.agency_name} has not submitted a daily production entry for {as_of}.",
                    entity_type="ProductionInstruction",
                    entity_id=pi.id,
                    entity_number=pi.instruction_number,
                )
                if inc: incidents.append(inc)
    except Exception:
        pass

    # 2. Production/job order delayed beyond expected date
    try:
        from apps.pms.models import ProductionInstruction
        from apps.jobwork.models import JobWorkOrder
        delayed_pi = ProductionInstruction.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            expected_completion_date__lt=as_of,
        ).exclude(status__in=["Completed", "Closed", "Cancelled"])
        for pi in delayed_pi:
            days = (as_of - pi.expected_completion_date).days
            inc = trigger_incident(
                client_id=client_id,
                alert_key="job_delayed",
                severity="Critical" if days > 7 else "Warning",
                title=f"Production Instruction Delayed: {pi.instruction_number}",
                message=f"Instruction is {days} days past expected completion date ({pi.expected_completion_date}). Current status: {pi.status}.",
                entity_type="ProductionInstruction",
                entity_id=pi.id,
                entity_number=pi.instruction_number,
            )
            if inc: incidents.append(inc)

        delayed_jwo = JobWorkOrder.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            expected_completion__lt=as_of,
        ).exclude(status__in=["Received", "Closed", "Cancelled"])
        for jwo in delayed_jwo:
            days = (as_of - jwo.expected_completion).days
            inc = trigger_incident(
                client_id=client_id,
                alert_key="job_delayed",
                severity="Critical" if days > 7 else "Warning",
                title=f"Job Work Order Delayed: {jwo.jwo_no}",
                message=f"Order with processor {jwo.vendor.name if jwo.vendor else jwo.vendor_name or 'Unknown'} is {days} days overdue (due {jwo.expected_completion}).",
                entity_type="JobWorkOrder",
                entity_id=jwo.id,
                entity_number=jwo.jwo_no,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    # 3. Material pending with agency/processor beyond configured days (default 15)
    try:
        from apps.jobwork.models import JobWorkOrder
        aging_jwo = JobWorkOrder.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            status__in=["Sent", "In-Process", "Partially Received"],
        )
        for jwo in aging_jwo:
            order_date = jwo.order_date or jwo.created_at.date()
            days_pending = (as_of - order_date).days
            if days_pending >= 15:
                inc = trigger_incident(
                    client_id=client_id,
                    alert_key="agency_pending_aging",
                    severity="Warning",
                    title=f"Material Pending with Processor {days_pending} Days: {jwo.jwo_no}",
                    message=f"Material on JWO {jwo.jwo_no} has been with {jwo.vendor.name if jwo.vendor else jwo.vendor_name or 'Processor'} for {days_pending} days.",
                    entity_type="JobWorkOrder",
                    entity_id=jwo.id,
                    entity_number=jwo.jwo_no,
                )
                if inc: incidents.append(inc)
    except Exception:
        pass

    # 4. Loss/wastage higher than tolerance
    try:
        from apps.jobwork.models import JWOInward
        inwards = JWOInward.objects.filter(client_id=client_id, deleted_at__isnull=True, date=as_of)
        for inward in inwards:
            sent_m = D(inward.job_work_order.planned_qty or 0)
            rec_m = D(inward.qty or 0)
            if sent_m > ZERO:
                loss_pct = round2((sent_m - rec_m) / sent_m * D(100))
                expected = D(inward.job_work_order.expected_loss_pct or 3.0)
                if loss_pct > expected:
                    inc = trigger_incident(
                        client_id=client_id,
                        alert_key="loss_tolerance_exceeded",
                        severity="Critical",
                        title=f"Excess Process Loss on Inward {inward.no or inward.challan_no}",
                        message=f"Actual loss {loss_pct}% exceeded expected tolerance of {expected}% on JWO {inward.job_work_order.jwo_no}.",
                        entity_type="JWOInward",
                        entity_id=inward.id,
                        entity_number=inward.no or inward.challan_no,
                    )
                    if inc: incidents.append(inc)
    except Exception:
        pass

    # 5. QC rejection or reprocess above threshold (> 5%)
    try:
        from apps.inventory.models import QCInspection
        inspections = QCInspection.objects.filter(client_id=client_id, inspection_date=as_of, deleted_at__isnull=True)
        for qc in inspections:
            total_takas = qc.total_takas_inspected or qc.lines.count()
            if total_takas > 0:
                rejects = qc.lines.filter(disposition__in=["Reject", "Reprocess"]).count()
                reject_pct = round2(D(rejects) / D(total_takas) * D(100))
                if reject_pct > D("5.0"):
                    inc = trigger_incident(
                        client_id=client_id,
                        alert_key="qc_rejection_high",
                        severity="Critical",
                        title=f"High QC Rejection/Reprocess ({reject_pct}%): {qc.inspection_number}",
                        message=f"{rejects} of {total_takas} takas failed inspection on {qc.inspection_number}.",
                        entity_type="QCInspection",
                        entity_id=qc.id,
                        entity_number=qc.inspection_number,
                    )
                    if inc: incidents.append(inc)
    except Exception:
        pass

    # 6. Negative or insufficient stock
    try:
        from apps.inventory.models import StockBalance
        negative_balances = StockBalance.objects.filter(client_id=client_id, on_hand__lt=0, deleted_at__isnull=True).select_related("item", "location")
        for b in negative_balances[:10]:
            inc = trigger_incident(
                client_id=client_id,
                alert_key="negative_stock",
                severity="Critical",
                title=f"Negative Stock: {b.item.name} at {b.location.name}",
                message=f"On-hand inventory is {b.on_hand} {b.item.unit_of_measure or 'm'}.",
                entity_type="StockBalance",
                entity_id=b.id,
                entity_number=b.item.sku or b.item.name,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    # 7. Customer credit limit exceeded or payment overdue
    try:
        from apps.sales.models import SalesInvoice
        from apps.masters.models import Party
        overdue_invoices = SalesInvoice.objects.filter(
            client_id=client_id,
            status__in=["Unpaid", "Partially Paid"],
            deleted_at__isnull=True,
            due_date__lt=as_of,
        ).select_related("party")
        for inv in overdue_invoices[:10]:
            days = (as_of - inv.due_date).days
            inc = trigger_incident(
                client_id=client_id,
                alert_key="credit_limit_exceeded",
                severity="Critical" if days > 30 else "Warning",
                title=f"Payment Overdue: {inv.invoice_number} ({days} days)",
                message=f"Customer {inv.party.name if inv.party else inv.party_name} has outstanding invoice {inv.invoice_number} of {inv.total} overdue by {days} days.",
                entity_type="SalesInvoice",
                entity_id=inv.id,
                entity_number=inv.invoice_number,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    # 8. Sales below cost / minimum margin
    try:
        from apps.sales.models import SalesOrderLine, SalesInvoiceLine
        recent_order_lines = SalesOrderLine.objects.filter(
            sales_order__client_id=client_id,
            sales_order__doc_date__gte=as_of - timedelta(days=7),
            below_cost=True,
            deleted_at__isnull=True,
        ).select_related("sales_order", "item")
        for sol in recent_order_lines[:5]:
            inc = trigger_incident(
                client_id=client_id,
                alert_key="below_cost_sale",
                severity="Critical",
                title=f"Below Landed Cost Sale on Order {sol.sales_order.order_number}",
                message=f"Item {sol.item.name} sold at rate ₹{sol.unit_price} below landed cost ₹{sol.landed_cost} (margin {sol.margin_pct}%).",
                entity_type="SalesOrder",
                entity_id=sol.sales_order_id,
                entity_number=sol.sales_order.order_number,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    # 9. Large discount (> 5%) or broker commission override
    try:
        from apps.sales.models import SalesInvoice
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
                if pct >= D("5.0"):
                    inc = trigger_incident(
                        client_id=client_id,
                        alert_key="large_discount",
                        severity="Warning",
                        title=f"Large Discount ({pct}%) on Invoice {inv.invoice_number}",
                        message=f"Total discount ₹{disc} represents {pct}% of subtotal ₹{sub}.",
                        entity_type="SalesInvoice",
                        entity_id=inv.id,
                        entity_number=inv.invoice_number,
                    )
                    if inc: incidents.append(inc)
    except Exception:
        pass

    # 10. Returnable sample overdue
    try:
        from apps.inventory.models import Sample
        overdue_samples = Sample.objects.filter(
            client_id=client_id,
            returnable=True,
            status="Issued",
            due_date__lt=as_of,
            deleted_at__isnull=True,
        ).select_related("party")
        for sm in overdue_samples:
            sm.status = "Overdue"
            sm.save(update_fields=["status", "updated_at"])
            days = (as_of - sm.due_date).days
            inc = trigger_incident(
                client_id=client_id,
                alert_key="sample_overdue",
                severity="Warning",
                title=f"Sample Overdue ({days} days): {sm.sample_number}",
                message=f"Sample {sm.sample_number} issued to {sm.party.name if sm.party else 'Customer'} was due on {sm.due_date}.",
                entity_type="Sample",
                entity_id=sm.id,
                entity_number=sm.sample_number,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    # 11. Vendor bill mismatch
    try:
        from apps.purchase.models import VendorBill
        unmatched_bills = VendorBill.objects.filter(
            client_id=client_id,
            status="Pending",
            deleted_at__isnull=True,
        )
        for vb in unmatched_bills[:5]:
            diff = D(vb.total_amount or 0) - D(vb.po_amount or 0)
            if abs(diff) > D("100.00"):
                inc = trigger_incident(
                    client_id=client_id,
                    alert_key="vendor_bill_mismatch",
                    severity="Warning",
                    title=f"Vendor Bill Mismatch on {vb.bill_number}",
                    message=f"Bill total ₹{vb.total_amount} differs from PO amount ₹{vb.po_amount} by ₹{diff}.",
                    entity_type="VendorBill",
                    entity_id=vb.id,
                    entity_number=vb.bill_number,
                )
                if inc: incidents.append(inc)
    except Exception:
        pass

    # 12. Slow-moving finished stock (> 90 days)
    try:
        from apps.inventory.models import LotTaka
        ninety_days_ago = as_of - timedelta(days=90)
        old_takas = LotTaka.objects.filter(
            client_id=client_id,
            status="Finished",
            available_quantity__gt=0,
            inward_date__lte=ninety_days_ago,
            deleted_at__isnull=True,
        )[:5]
        for t in old_takas:
            age = (as_of - (t.inward_date or t.created_at.date())).days
            inc = trigger_incident(
                client_id=client_id,
                alert_key="slow_moving_stock",
                severity="Info",
                title=f"Slow-Moving Stock ({age} days): Taka {t.taka_number} (Lot {t.lot_number})",
                message=f"Finished fabric has {t.available_quantity}m remaining, received {age} days ago.",
                entity_type="LotTaka",
                entity_id=t.id,
                entity_number=t.taka_number or t.lot_number,
            )
            if inc: incidents.append(inc)
    except Exception:
        pass

    return incidents
