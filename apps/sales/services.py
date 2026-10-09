"""
Sales document services (api.md §5).

Everything the frontend computes in ``ERPContext`` and must stop computing:
document totals, the GST split, stock posting, ledger posting, credit-limit
enforcement, payment status and the cancellation chain.

The rule that governs this whole module (api.md §5.7 rule 2):
**never trust client-side totals -- recompute, and reject on mismatch beyond a
0.01 rounding tolerance.**
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core.money import (
    ZERO,
    D,
    ROUNDING_TOLERANCE,
    compute_document_totals,
    compute_line,
    derive_payment_status,
    round2,
    round4,
    totals_match,
)
from apps.core.numbering import allocate_number
from apps.core.permissions import has_permission
from apps.inventory import services as stock

from .models import (
    NON_RESERVING_STAGES,
    DeliveryChallan,
    PaymentAllocation,
    PaymentIn,
    SalesInvoice,
    SalesOrder,
    SalesOrderLine,
)

#: api.md §5.7 -- dueDate defaults to invoice date + 30 days.
DEFAULT_PAYMENT_TERM_DAYS = 30


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------
def company_state(client_id):
    from apps.core.models import CompanyProfile

    return (
        CompanyProfile.objects.filter(client_id=client_id)
        .values_list("state", flat=True)
        .first()
        or ""
    )


def is_intra_state(client_id, place_of_supply):
    """api.md §5.7 -- compare the party's place of supply with the company's own
    state from ``/settings/company-profile/``.

    The frontend hardcodes Maharashtra (27); reading it from the profile is the
    bug this fixes.
    """
    home = (company_state(client_id) or "").strip().lower()
    supply = (place_of_supply or "").strip().lower()
    if not home or not supply:
        # With no profile state configured, intra-state (CGST+SGST) is the safe
        # default for a domestic tenant, and the mismatch is visible on the
        # document rather than silently becoming IGST.
        return True
    return home == supply


@transaction.atomic
def recalculate_document(document, *, lines=None, save=True):
    """Recompute every line and the header with the api.md §5.7 algorithm.

    Called on create, on every draft edit, and again at finalization. The
    header check constraint (db.md §3.1) turns a bug here into a loud failure
    rather than a wrong ledger.
    """
    line_rows = list(lines if lines is not None else document.line_items.all())

    computed = []
    for line in line_rows:
        totals = compute_line(line.qty, line.rate, line.discount_pct, line.tax_pct)
        line.amount = totals.line_sub
        line.discount_amount = totals.discount_amount
        line.tax_amount = totals.tax_amount
        line.line_total = totals.line_total
        computed.append(totals)

    header = compute_document_totals(
        computed,
        discount_total_override=document.discount_override,
        freight_charges=document.freight_charges,
        other_charges=document.other_charges,
        round_off=document.round_off,
        amount_paid=document.amount_paid,
        is_intra_state=is_intra_state(document.client_id, document.place_of_supply),
    )

    document.subtotal = header.subtotal
    document.total_discount = header.total_discount
    document.taxable_value = header.taxable_value
    document.total_tax = header.total_tax
    document.cgst = header.cgst
    document.sgst = header.sgst
    document.igst = header.igst
    document.cess = header.cess
    document.total = header.total

    if save:
        if line_rows:
            type(line_rows[0]).objects.bulk_update(
                line_rows,
                ["amount", "discount_amount", "tax_amount", "line_total", "updated_at"],
            )
        document.save(
            update_fields=[
                "subtotal", "total_discount", "taxable_value", "total_tax",
                "cgst", "sgst", "igst", "cess", "total",
                # The entered charges are written in the same statement as the
                # total they feed, or the db.md §3.1 check would see a header
                # whose total does not yet include them.
                "freight_charges", "other_charges", "round_off",
                "discount_override", "updated_at",
            ]
        )
    return header


def assert_client_totals_match(document, claimed):
    """api.md §5.7 rule 2. ``claimed`` is the client's view of the totals."""
    if not claimed:
        return
    for field, value in (
        ("subtotal", document.subtotal),
        ("totalTax", document.total_tax),
        ("grandTotal", document.total),
    ):
        if field in claimed and not totals_match(value, claimed[field]):
            raise ValidationFailed(
                "The totals on this document do not match the server's calculation.",
                code="TOTALS_MISMATCH",
                detail=(
                    f"{field}: client sent {claimed[field]}, server computed {value}."
                ),
                field_errors={field: ["Recalculated by the server."]},
            )


# ---------------------------------------------------------------------------
# Credit limit (api.md §4.1)
# ---------------------------------------------------------------------------
def assert_credit_limit(party, additional_amount, *, user=None, override=False):
    """422 CREDIT_LIMIT_EXCEEDED unless the caller holds the override permission
    **and** passes ``{"overrideCreditLimit": true}``.

    Checked against the derived balance plus open unbilled orders (db.md §4.1).
    """
    limit = party.credit_limit
    if limit is None or D(limit) <= ZERO:
        return

    open_orders = SalesOrder.objects.filter(
        client_id=party.client_id,
        party_id=party.id,
        deleted_at__isnull=True,
    ).exclude(stage__in=["Invoiced", "Cancelled"]).aggregate(
        value=Coalesce(
            Sum("total"), Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    )["value"]

    exposure = round2(D(party.balance) + D(open_orders) + D(additional_amount))
    if exposure <= D(limit):
        return

    if override and user is not None and has_permission(user, "override_credit_limit"):
        return

    raise BusinessRuleViolation(
        f"{party.name} would exceed their credit limit. "
        f"Limit {round2(limit)}, exposure would be {exposure}.",
        code=Codes.CREDIT_LIMIT_EXCEEDED,
        payload={
            "partyId": str(party.id),
            "creditLimit": str(round2(limit)),
            "currentBalance": str(round2(party.balance)),
            "openOrders": str(round2(open_orders)),
            "requested": str(round2(additional_amount)),
            "exposure": str(exposure),
        },
    )


# ---------------------------------------------------------------------------
# Payment status (api.md §5.7)
# ---------------------------------------------------------------------------
def allocated_to(client_id, document_type, document_id):
    return PaymentAllocation.objects.filter(
        client_id=client_id,
        document_type=document_type,
        document_id=document_id,
        deleted_at__isnull=True,
    ).aggregate(
        value=Coalesce(
            Sum("amount"), Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    )["value"]


def refresh_invoice_payment_status(invoice):
    """Recompute ``amount_paid`` and ``status`` from the allocations.

    ``Overdue`` is not set here -- it is layered on at read time from
    ``due_date`` plus ``balance_due > 0`` (db.md §12, never stored).
    """
    if invoice.status == "Cancelled":
        return invoice

    invoice.amount_paid = round2(allocated_to(invoice.client_id, "SalesInvoice", invoice.id))
    finalized = invoice.posted_at is not None
    invoice.status = derive_payment_status(
        invoice.total, invoice.amount_paid, finalized=finalized
    )
    invoice.save(update_fields=["amount_paid", "status", "updated_at"])

    if invoice.sales_order_id:
        refresh_order_payment_status(invoice.sales_order)
    return invoice


def refresh_order_payment_status(order):
    """``sales_orders.payment_status`` is derived from its invoices (db.md §12)."""
    rows = SalesInvoice.objects.filter(
        sales_order_id=order.id, deleted_at__isnull=True
    ).exclude(status__in=["Cancelled", "Draft"]).aggregate(
        total=Coalesce(Sum("total"), Value(Decimal("0.00")),
                       output_field=DecimalField(max_digits=18, decimal_places=2)),
        paid=Coalesce(Sum("amount_paid"), Value(Decimal("0.00")),
                      output_field=DecimalField(max_digits=18, decimal_places=2)),
    )
    status = derive_payment_status(rows["total"], rows["paid"], finalized=True)
    if rows["total"] == ZERO:
        status = "Unpaid"
    if order.payment_status != status:
        order.payment_status = status
        order.save(update_fields=["payment_status", "updated_at"])
    return order


def invoice_outstanding(invoice):
    """``GET /sales/invoices/{id}/outstanding/`` (api.md §5.7)."""
    from apps.core.money import ageing_bucket

    paid = round2(allocated_to(invoice.client_id, "SalesInvoice", invoice.id))
    outstanding = round2(D(invoice.total) - paid)
    outstanding = outstanding if outstanding > ZERO else ZERO
    today = timezone.localdate()
    days_overdue = (
        (today - invoice.due_date).days
        if invoice.due_date and outstanding > ZERO
        else 0
    )
    return {
        "total": round2(invoice.total),
        "paid": paid,
        "outstanding": outstanding,
        "dueDate": invoice.due_date,
        "daysOverdue": max(days_overdue, 0),
        "ageingBucket": ageing_bucket(days_overdue),
    }


def display_status(invoice):
    """``Overdue`` layered on at read time (api.md §5.7)."""
    if invoice.status in ("Cancelled", "Draft", "Paid"):
        return invoice.status
    if (
        invoice.due_date
        and invoice.due_date < timezone.localdate()
        and D(invoice.total) - D(invoice.amount_paid) > ROUNDING_TOLERANCE
    ):
        return "Overdue"
    return invoice.status


# ---------------------------------------------------------------------------
# Finalization (api.md §5.7)
# ---------------------------------------------------------------------------
@transaction.atomic
def finalize_invoice(invoice, *, user=None, override_credit_limit=False):
    """Draft -> Unpaid, in one transaction (api.md §5.7).

    1. Allocate the invoice number.
    2. Recompute all totals.
    3. Post SALE movements for lines not already dispatched via a challan.
    4. Post the accounting entry: Dr Debtors, Cr Sales, Cr GST payable.
    5. Update party balance and the order's ``invoiced_qty``.
    """
    invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)

    if invoice.status == "Cancelled":
        raise Conflict(
            "This invoice has been cancelled.", code=Codes.ALREADY_CANCELLED
        )
    if invoice.posted_at is not None:
        raise Conflict(
            "This invoice has already been finalized.", code=Codes.ALREADY_FINALIZED
        )

    lines = list(invoice.line_items.select_related("item").all())
    if not lines:
        raise ValidationFailed(
            "An invoice needs at least one line.",
            field_errors={"lineItems": ["Add at least one item."]},
        )

    recalculate_document(invoice, lines=lines)
    assert_credit_limit(
        invoice.party, invoice.total, user=user, override=override_credit_limit
    )

    # 1. Allocate the number.
    if not invoice.invoice_number:
        invoice.invoice_number = allocate_number(invoice.client, "INV", invoice.doc_date)

    if invoice.due_date is None:
        invoice.due_date = invoice.doc_date + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)

    # 3. Post SALE movements, skipping anything a challan already depleted.
    _post_invoice_stock(invoice, lines, user=user)

    invoice.posted_at = timezone.now()
    invoice.posted_by = user if getattr(user, "is_authenticated", False) else None
    invoice.status = derive_payment_status(invoice.total, invoice.amount_paid, finalized=True)
    invoice.save()

    # 4. Ledger.
    entry = ledger.post_sales_invoice(invoice, user=user)
    if entry is not None:
        invoice.journal_entry = entry
        invoice.save(update_fields=["journal_entry", "updated_at"])

    # 5. Order rollup.
    _bump_order_invoiced_qty(lines)
    if invoice.sales_order_id:
        _advance_order_stage(invoice.sales_order)

    return invoice


def _post_invoice_stock(invoice, lines, *, user=None):
    """api.md §5.7 rule 3 -- avoid double depletion.

    A line already dispatched on a challan has moved its stock; matching on the
    source order line is what makes that check reliable.
    """
    location_id = invoice.location_id or _default_location_id(invoice.client_id)

    for line in lines:
        if line.item_id is None or not line.item.holds_stock:
            continue
        if line.delivery_challan_line_id is not None:
            continue  # dispatched already
        if line.sales_order_line_id and _already_dispatched(line.sales_order_line_id):
            continue

        stock.assert_not_qc_blocked(invoice.client_id, line.item_id, line.item_name)
        stock.assert_sufficient_stock(
            invoice.client_id, line.item, line.qty, location_id, line.item_name
        )

        serials = stock.serials_for_lines(
            invoice.client_id, "sales_invoice_lines", [line.id]
        ).get(line.id, [])
        resolved = stock.resolve_serials(invoice.client_id, line.item_id, serials)
        stock.assert_serial_count(line.item, line.qty, serials, line.item_name)

        movement = stock.post_movement(
            client_id=invoice.client_id,
            item=line.item_id,
            location=location_id,
            type="SALE",
            quantity=-D(line.qty),
            unit_cost=line.item.cost_price,
            reference_type="SalesInvoice",
            reference_id=invoice.id,
            reference_number=invoice.invoice_number,
            movement_date=invoice.doc_date,
            user=user,
        )
        if resolved:
            stock.set_serial_status(resolved, "sold", movement=movement)


def _already_dispatched(sales_order_line_id):
    from .models import DeliveryChallanLine

    return DeliveryChallanLine.objects.filter(
        sales_order_line_id=sales_order_line_id,
        deleted_at__isnull=True,
        delivery_challan__status__in=["Dispatched", "In Transit", "Delivered"],
    ).exists()


def _default_location_id(client_id):
    from apps.masters.models import Location

    location_id = (
        Location.objects.filter(
            client_id=client_id, is_active=True, deleted_at__isnull=True, type="Warehouse"
        )
        .values_list("id", flat=True)
        .first()
    )
    if location_id is None:
        raise BusinessRuleViolation(
            "No warehouse is configured for this workspace.",
            code="NO_LOCATION",
            detail="Create a location under Inventory before posting stock.",
        )
    return location_id


def _bump_order_invoiced_qty(lines):
    from .models import SalesOrderLine

    for line in lines:
        if line.sales_order_line_id is None:
            continue
        order_line = SalesOrderLine.objects.select_for_update().get(
            pk=line.sales_order_line_id
        )
        new_value = round4(D(order_line.invoiced_qty) + D(line.qty))
        if new_value > D(order_line.qty) + ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                f"Invoicing {round4(line.qty)} of {line.item_name} would exceed the "
                f"ordered quantity ({round4(order_line.qty)}).",
                code=Codes.OVER_INVOICE,
                payload={
                    "orderLineId": str(order_line.id),
                    "ordered": str(round4(order_line.qty)),
                    "alreadyInvoiced": str(round4(order_line.invoiced_qty)),
                    "requested": str(round4(line.qty)),
                },
            )
        order_line.invoiced_qty = new_value
        order_line.save(update_fields=["invoiced_qty", "updated_at"])


def _advance_order_stage(order):
    """Move an order to ``Invoiced`` once every line is fully invoiced."""
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.stage in ("Cancelled", "Invoiced"):
        return order
    outstanding = order.line_items.filter(
        deleted_at__isnull=True, invoiced_qty__lt=F("qty")
    ).exists()
    if not outstanding:
        order.stage = "Invoiced"
        order.save(update_fields=["stage", "updated_at"])
    refresh_order_payment_status(order)
    return order


# ---------------------------------------------------------------------------
# Challan dispatch (api.md §5.6)
# ---------------------------------------------------------------------------
@transaction.atomic
def dispatch_challan(challan, *, user=None):
    """Posts ``SALE`` movements (negative qty) and consumes the selected serials."""
    challan = DeliveryChallan.objects.select_for_update().get(pk=challan.pk)
    if challan.status == "Cancelled":
        raise Conflict("This challan has been cancelled.", code=Codes.ALREADY_CANCELLED)
    if challan.posted_at is not None:
        raise Conflict("This challan has already been dispatched.", code=Codes.ALREADY_DONE)

    lines = list(challan.line_items.select_related("item").all())
    if not lines:
        raise ValidationFailed(
            "A challan needs at least one line.",
            field_errors={"lineItems": ["Add at least one item."]},
        )

    recalculate_document(challan, lines=lines)
    if not challan.challan_number:
        challan.challan_number = allocate_number(challan.client, "DC", challan.doc_date)

    location_id = challan.location_id or _default_location_id(challan.client_id)
    serial_map = stock.serials_for_lines(
        challan.client_id, "delivery_challan_lines", [line.id for line in lines]
    )

    for line in lines:
        if line.item_id is None or not line.item.holds_stock:
            continue

        stock.assert_not_qc_blocked(challan.client_id, line.item_id, line.item_name)
        stock.assert_sufficient_stock(
            challan.client_id, line.item, line.qty, location_id, line.item_name
        )

        serial_numbers = serial_map.get(line.id, [])
        stock.assert_serial_count(line.item, line.qty, serial_numbers, line.item_name)
        resolved = stock.resolve_serials(challan.client_id, line.item_id, serial_numbers)

        movement = stock.post_movement(
            client_id=challan.client_id,
            item=line.item_id,
            location=location_id,
            type="SALE",
            quantity=-D(line.qty),
            unit_cost=line.item.cost_price,
            reference_type="DeliveryChallan",
            reference_id=challan.id,
            reference_number=challan.challan_number,
            movement_date=challan.dispatch_date or challan.doc_date,
            user=user,
        )
        if resolved:
            stock.set_serial_status(resolved, "sold", movement=movement)

        if line.sales_order_line_id:
            _bump_order_dispatched_qty(line)

    challan.status = "Dispatched"
    challan.dispatch_date = challan.dispatch_date or timezone.localdate()
    challan.posted_at = timezone.now()
    challan.posted_by = user if getattr(user, "is_authenticated", False) else None
    challan.save()

    if challan.sales_order_id:
        _advance_order_after_dispatch(challan.sales_order)
    return challan


def _bump_order_dispatched_qty(line):
    order_line = SalesOrderLine.objects.select_for_update().get(pk=line.sales_order_line_id)
    new_value = round4(D(order_line.dispatched_qty) + D(line.qty))
    if new_value > D(order_line.qty) + ROUNDING_TOLERANCE:
        raise BusinessRuleViolation(
            f"Dispatching {round4(line.qty)} of {line.item_name} would exceed the "
            f"ordered quantity ({round4(order_line.qty)}).",
            code=Codes.OVER_DISPATCH,
            payload={
                "orderLineId": str(order_line.id),
                "ordered": str(round4(order_line.qty)),
                "alreadyDispatched": str(round4(order_line.dispatched_qty)),
                "requested": str(round4(line.qty)),
            },
        )
    order_line.dispatched_qty = new_value
    order_line.save(update_fields=["dispatched_qty", "updated_at"])


def _advance_order_after_dispatch(order):
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.stage in ("Cancelled", "Invoiced", "Delivered"):
        return order
    pending = order.line_items.filter(
        deleted_at__isnull=True, dispatched_qty__lt=F("qty")
    ).exists()
    order.stage = "Dispatched" if pending else "Delivered"
    order.save(update_fields=["stage", "updated_at"])
    return order


# ---------------------------------------------------------------------------
# Cancellation (api.md §6.9)
# ---------------------------------------------------------------------------
def assert_no_dependents(document, dependents):
    """api.md §6.9 step 1 -- 409 HAS_DEPENDENTS with the blocking ids."""
    blocking = [
        {"type": label, "id": str(obj.id), "number": number_of(obj)}
        for label, queryset in dependents
        for obj in queryset
    ]
    if blocking:
        labels = ", ".join(
            f"{item['type']} {item['number'] or item['id']}" for item in blocking
        )
        raise Conflict(
            f"Cancel or delete the linked document(s) first: {labels}.",
            code=Codes.HAS_DEPENDENTS,
            payload={"dependents": blocking},
        )


def number_of(document):
    for field in (
        "invoice_number", "challan_number", "order_number", "quotation_number",
        "proforma_number", "return_number", "bill_number",
        "po_number", "payment_number", "card_number",
    ):
        value = getattr(document, field, None)
        if value:
            return value
    return None


@transaction.atomic
def cancel_invoice(invoice, *, reason=None, user=None):
    """Full reversal (api.md §5.7, §6.9)."""
    invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == "Cancelled":
        raise Conflict("This invoice is already cancelled.", code=Codes.ALREADY_CANCELLED)

    # Payments block a cancellation -- the frontend's message, kept verbatim.
    payments = PaymentAllocation.objects.filter(
        client_id=invoice.client_id,
        document_type="SalesInvoice",
        document_id=invoice.id,
        deleted_at__isnull=True,
    )
    if payments.exists():
        total = payments.aggregate(
            value=Coalesce(Sum("amount"), Value(Decimal("0.00")),
                           output_field=DecimalField(max_digits=18, decimal_places=2))
        )["value"]
        raise BusinessRuleViolation(
            "Cannot cancel an invoice that has recorded payments.",
            code="INVOICE_HAS_PAYMENTS",
            detail=(
                f"Invoice {invoice.invoice_number} has {payments.count()} "
                f"payment(s) totalling {round2(total)}."
            ),
        )

    assert_no_dependents(
        invoice,
        [("Credit note", invoice.returns.filter(deleted_at__isnull=True).exclude(status="Cancelled"))],
    )

    _reverse_document(invoice, "SalesInvoice", user=user, reason=reason)

    # Release the order rollup.
    for line in invoice.line_items.all():
        if line.sales_order_line_id:
            order_line = SalesOrderLine.objects.select_for_update().get(
                pk=line.sales_order_line_id
            )
            order_line.invoiced_qty = max(
                round4(D(order_line.invoiced_qty) - D(line.qty)), ZERO
            )
            order_line.save(update_fields=["invoiced_qty", "updated_at"])

    if invoice.sales_order_id:
        order = SalesOrder.objects.select_for_update().get(pk=invoice.sales_order_id)
        if order.stage == "Invoiced":
            order.stage = "Delivered"
            order.save(update_fields=["stage", "updated_at"])
        refresh_order_payment_status(order)

    return invoice


@transaction.atomic
def cancel_challan(challan, *, reason=None, user=None):
    """Posts ``SALE_REVERSAL`` movements and returns the serials to stock."""
    challan = DeliveryChallan.objects.select_for_update().get(pk=challan.pk)
    if challan.status == "Cancelled":
        raise Conflict("This challan is already cancelled.", code=Codes.ALREADY_CANCELLED)

    assert_no_dependents(
        challan,
        [("Invoice", challan.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled"))],
    )

    _reverse_document(challan, "DeliveryChallan", user=user, reason=reason)

    for line in challan.line_items.all():
        if line.sales_order_line_id:
            order_line = SalesOrderLine.objects.select_for_update().get(
                pk=line.sales_order_line_id
            )
            order_line.dispatched_qty = max(
                round4(D(order_line.dispatched_qty) - D(line.qty)), ZERO
            )
            order_line.save(update_fields=["dispatched_qty", "updated_at"])

    return challan


def _reverse_document(document, reference_type, *, user, reason):
    """Shared cancellation body: movements, serials, ledger, status stamps."""
    client_id = document.client_id

    # Serials go back to stock before the movements are reversed, so the two
    # halves of "this unit is available again" land together.
    _restore_serials(client_id, reference_type, document.id)

    stock.reverse_movements(
        reference_type=reference_type,
        reference_id=document.id,
        client_id=client_id,
        user=user,
        notes=reason or f"{reference_type} cancelled",
    )
    ledger.reverse_document_entries(
        client_id=client_id,
        source_document_type=reference_type,
        source_document_id=document.id,
        user=user,
    )

    document.status = "Cancelled"
    document.cancelled_at = timezone.now()
    document.cancelled_by = user if getattr(user, "is_authenticated", False) else None
    document.cancellation_reason = reason
    document.save(
        update_fields=[
            "status", "cancelled_at", "cancelled_by", "cancellation_reason", "updated_at",
        ]
    )
    ledger.recalculate_party_balance(client_id, document.party_id)
    return document


def _restore_serials(client_id, reference_type, reference_id):
    from apps.inventory.models import StockMovement
    from apps.masters.models import ItemSerial

    movement_ids = StockMovement.objects.filter(
        client_id=client_id, reference_type=reference_type, reference_id=reference_id
    ).values_list("id", flat=True)
    ItemSerial.objects.filter(
        client_id=client_id, sold_movement_id__in=list(movement_ids)
    ).update(status="available", sold_movement=None)


# ---------------------------------------------------------------------------
# Payments in (api.md §5.8)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_payment_in(
    *, client, party, amount, payment_date, mode, bank_account=None,
    reference_number=None, notes=None, allocations=None, invoice=None, user=None,
):
    """api.md §5.8 -- all five validations moved server-side.

    The frontend credits the first bank account when none is given; api.md is
    explicit that the server must require an explicit ``bankAccountId`` for
    non-cash modes instead.
    """
    amount = round2(amount)
    if amount <= ZERO:
        raise BusinessRuleViolation(
            "Payment amount must be greater than zero.",
            code="PAYMENT_AMOUNT_INVALID",
        )

    if mode != "Cash" and bank_account is None:
        raise ValidationFailed(
            "Choose the bank account this payment was received into.",
            field_errors={"bankAccountId": ["Required for non-cash payments."]},
        )

    payment = PaymentIn.objects.create(
        client=client,
        payment_number=allocate_number(client, "PAY-IN", payment_date),
        party=party,
        payment_date=payment_date,
        amount=amount,
        mode=mode,
        bank_account=bank_account,
        reference_number=reference_number,
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    requested = list(allocations or [])
    if not requested and invoice is not None:
        requested = [{"invoiceId": invoice.id, "amount": amount}]

    allocate_payment_in(payment, requested, user=user)

    entry = ledger.post_payment_in(payment, user=user)
    if entry is not None:
        payment.journal_entry = entry
        payment.save(update_fields=["journal_entry", "updated_at"])

    ledger.recalculate_party_balance(client.id, party)
    return payment


@transaction.atomic
def allocate_payment_in(payment, allocations, *, user=None):
    """Apply a payment against invoices, enforcing the api.md §5.8 table."""
    allocated = ZERO
    for row in allocations or []:
        invoice_id = row.get("invoiceId") or row.get("invoice_id") or row.get("documentId")
        requested = round2(row.get("amount"))
        if not invoice_id or requested <= ZERO:
            continue

        invoice = (
            SalesInvoice.objects.select_for_update()
            .filter(pk=invoice_id, client_id=payment.client_id, deleted_at__isnull=True)
            .first()
        )
        if invoice is None:
            raise NotFound("That invoice no longer exists.")

        if invoice.status == "Cancelled":
            raise BusinessRuleViolation(
                "Cannot record payment against a cancelled invoice.",
                code=Codes.PAYMENT_ON_CANCELLED,
            )
        if invoice.posted_at is None or invoice.status == "Draft":
            raise BusinessRuleViolation(
                "Cannot record payment against a draft invoice. Finalize it first.",
                code=Codes.PAYMENT_ON_DRAFT,
            )

        already = round2(allocated_to(payment.client_id, "SalesInvoice", invoice.id))
        balance = round2(D(invoice.total) - already)
        if balance <= ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                "This invoice is already fully paid.", code=Codes.ALREADY_SETTLED
            )
        if requested > balance + ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                f"Payment amount ({requested}) exceeds the remaining invoice "
                f"balance ({balance}).",
                code=Codes.PAYMENT_EXCEEDS_BALANCE,
                payload={"requested": str(requested), "balance": str(balance)},
            )

        PaymentAllocation.objects.create(
            client_id=payment.client_id,
            payment_id=payment.id,
            payment_side="in",
            document_type="SalesInvoice",
            document_id=invoice.id,
            amount=requested,
            allocated_by=user if getattr(user, "is_authenticated", False) else None,
        )
        allocated += requested
        refresh_invoice_payment_status(invoice)

    total_allocated = round2(
        PaymentAllocation.objects.filter(
            client_id=payment.client_id, payment_side="in", payment_id=payment.id,
            deleted_at__isnull=True,
        ).aggregate(
            value=Coalesce(Sum("amount"), Value(Decimal("0.00")),
                           output_field=DecimalField(max_digits=18, decimal_places=2))
        )["value"]
    )
    if total_allocated > D(payment.amount) + ROUNDING_TOLERANCE:
        raise BusinessRuleViolation(
            "Allocations exceed the payment amount.",
            code=Codes.PAYMENT_EXCEEDS_BALANCE,
        )
    payment.allocated_amount = total_allocated
    payment.save(update_fields=["allocated_amount", "updated_at"])
    return payment


@transaction.atomic
def cancel_payment_in(payment, *, reason=None, user=None):
    payment = PaymentIn.objects.select_for_update().get(pk=payment.pk)
    if payment.status == "Cancelled":
        raise Conflict("This payment is already cancelled.", code=Codes.ALREADY_CANCELLED)

    invoice_ids = list(
        PaymentAllocation.objects.filter(
            client_id=payment.client_id, payment_side="in", payment_id=payment.id,
            document_type="SalesInvoice", deleted_at__isnull=True,
        ).values_list("document_id", flat=True)
    )
    PaymentAllocation.objects.filter(
        client_id=payment.client_id, payment_side="in", payment_id=payment.id
    ).update(deleted_at=timezone.now())

    payment.status = "Cancelled"
    payment.allocated_amount = ZERO
    payment.cancelled_at = timezone.now()
    payment.cancellation_reason = reason
    payment.save(
        update_fields=["status", "allocated_amount", "cancelled_at",
                       "cancellation_reason", "updated_at"]
    )

    ledger.reverse_document_entries(
        client_id=payment.client_id,
        source_document_type="PaymentIn",
        source_document_id=payment.id,
        user=user,
    )
    for invoice in SalesInvoice.objects.filter(pk__in=invoice_ids):
        refresh_invoice_payment_status(invoice)
    ledger.recalculate_party_balance(payment.client_id, payment.party_id)
    return payment


def unallocated_payments(client_id, party_id=None):
    """``GET /sales/payments/unallocated/`` -- customer advances.

    The unallocated remainder *is* the advance; there is no balance column
    (db.md §5.4).
    """
    queryset = PaymentIn.objects.filter(
        client_id=client_id, status="Active", deleted_at__isnull=True
    ).filter(allocated_amount__lt=F("amount"))
    if party_id:
        queryset = queryset.filter(party_id=party_id)
    return queryset.select_related("party")


# ---------------------------------------------------------------------------
# Order fulfilment (api.md §5.4)
# ---------------------------------------------------------------------------
def order_fulfilment(order):
    """``GET /sales/orders/{id}/fulfilment/`` -- per-line ordered / dispatched /
    invoiced / pending."""
    lines = order.line_items.filter(deleted_at__isnull=True).order_by("line_no")
    rows = []
    for line in lines:
        ordered = D(line.qty)
        dispatched = D(line.dispatched_qty)
        invoiced = D(line.invoiced_qty)
        rows.append(
            {
                "lineId": str(line.id),
                "sku": line.sku,
                "itemName": line.item_name,
                "orderedQty": ordered,
                "dispatchedQty": dispatched,
                "invoicedQty": invoiced,
                "pendingQty": max(ordered - dispatched, ZERO),
                "pendingInvoiceQty": max(ordered - invoiced, ZERO),
            }
        )
    return {
        "orderId": str(order.id),
        "orderNumber": order.order_number,
        "stage": order.stage,
        "lines": rows,
    }


# ---------------------------------------------------------------------------
# Warranty coverage (api.md §5.10)
# ---------------------------------------------------------------------------
def coverage_status(card, today=None):
    """Derived on every read (api.md §5.10, from ``utils/warrantyUtils.js``).

    ``documentStatus`` of Cancelled/Suspended wins outright; a future or
    missing ``startDate`` -> Pending Activation; past ``expiryDate`` ->
    Expired; inside the expiring-soon window -> Expiring Soon; else Active.
    """
    today = today or timezone.localdate()

    if card.document_status == "Cancelled":
        return "Cancelled"
    if card.document_status == "Suspended":
        return "Suspended"
    if card.document_status in ("Draft", "Void"):
        return "Pending Activation"

    if card.start_date is None or card.start_date > today:
        return "Pending Activation"
    if card.expiry_date is None:
        return "Active"
    if card.expiry_date < today:
        return "Expired"
    if (card.expiry_date - today).days <= (card.expiring_soon_days or 30):
        return "Expiring Soon"
    return "Active"


def compute_expiry(start_date, period, unit):
    """``warrantyPeriod`` + ``warrantyUnit`` -> an expiry date."""
    if start_date is None:
        return None
    period = int(period or 0)
    if unit == "Months":
        month = start_date.month - 1 + period
        year = start_date.year + month // 12
        month = month % 12 + 1
        day = min(start_date.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                                   else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        return start_date.replace(year=year, month=month, day=day)
    try:
        return start_date.replace(year=start_date.year + period)
    except ValueError:  # 29 Feb
        return start_date.replace(year=start_date.year + period, day=28)
