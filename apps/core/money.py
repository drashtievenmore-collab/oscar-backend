"""
The canonical document-totals algorithm (api.md §5.7) and money helpers.

api.md §5.7 ports this verbatim from ``ERPContext.createInvoice`` and requires
the same algorithm on quotations, orders, proformas and bills. The server is
authoritative: client-supplied totals are recomputed and rejected on a mismatch
beyond a 0.01 rounding tolerance.

Everything here is :class:`~decimal.Decimal`. ``float`` is never used for money
(db.md §1.5) -- not in storage, not in transit, not in an intermediate.
"""
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")
CENT = Decimal("0.01")
QTY_EXP = Decimal("0.0001")
RATE_EXP = Decimal("0.0001")

#: api.md §5.7 -- "line `tax` = 18% when not supplied".
DEFAULT_TAX_PCT = Decimal("18")

#: api.md §5.7 -- recomputed totals may differ from the client's by at most this.
ROUNDING_TOLERANCE = Decimal("0.01")


def D(value, default=ZERO):
    """Coerce anything the wire may carry into a Decimal.

    Tolerates None, "", and numeric strings. Rejects nothing -- validation
    belongs in the serializer; this is the arithmetic layer.
    """
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


def round2(value):
    """Money rounding: 2 decimals, half-up (what an Indian invoice expects)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def round4(value):
    """Quantity / unit-cost rounding: 4 decimals (db.md §1.5)."""
    return D(value).quantize(QTY_EXP, rounding=ROUND_HALF_UP)


def non_negative(value):
    value = D(value)
    return value if value > ZERO else ZERO


class LineTotals:
    """The computed numbers for one document line."""

    __slots__ = ("line_sub", "discount_amount", "taxable", "tax_amount", "line_total")

    def __init__(self, line_sub, discount_amount, taxable, tax_amount, line_total):
        self.line_sub = line_sub
        self.discount_amount = discount_amount
        self.taxable = taxable
        self.tax_amount = tax_amount
        self.line_total = line_total

    def as_dict(self):
        return {
            "amount": self.line_sub,
            "discount_amount": self.discount_amount,
            "taxable": self.taxable,
            "tax_amount": self.tax_amount,
            "line_total": self.line_total,
        }


def compute_line(qty, rate, discount_pct=None, tax_pct=None):
    """api.md §5.7, the per-line half.

        lineSub = rate * qty
        discAmt = lineSub * (discount% / 100)        -- discount is a percentage
        taxable = max(0, lineSub - discAmt)
        lineTax = round2(taxable * (taxRate / 100))  -- taxRate defaults to 18
    """
    qty = D(qty)
    rate = D(rate)
    discount_pct = D(discount_pct)
    tax_pct = DEFAULT_TAX_PCT if tax_pct is None or tax_pct == "" else D(tax_pct)

    line_sub = round2(rate * qty)
    discount_amount = round2(line_sub * (discount_pct / Decimal("100")))
    taxable = non_negative(line_sub - discount_amount)
    tax_amount = round2(taxable * (tax_pct / Decimal("100")))
    line_total = round2(taxable + tax_amount)
    return LineTotals(line_sub, discount_amount, taxable, tax_amount, line_total)


class DocumentTotals:
    """The computed numbers for a document header."""

    __slots__ = (
        "subtotal",
        "total_discount",
        "taxable_value",
        "total_tax",
        "cgst",
        "sgst",
        "igst",
        "cess",
        "freight_charges",
        "other_charges",
        "round_off",
        "total",
        "amount_paid",
        "balance_due",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, ZERO))

    def as_header_fields(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<DocumentTotals total={self.total} tax={self.total_tax}>"


def compute_document_totals(
    lines,
    *,
    discount_total_override=None,
    freight_charges=None,
    other_charges=None,
    round_off=None,
    amount_paid=None,
    is_intra_state=True,
    cess=None,
):
    """api.md §5.7, the document half, plus the GST split.

    ``lines`` is an iterable of :class:`LineTotals` (or anything exposing the
    same attributes).

        subtotal      = sum(lineSub)
        discountTotal = document-level override, else sum(discAmt)
        taxableAmount = max(0, subtotal - discountTotal)
        totalTax      = sum(lineTax)
        grandTotal    = round2(taxableAmount + totalTax + freight + other + roundOff)
        balanceDue    = max(0, grandTotal - amountPaid)

    ``is_intra_state`` comes from comparing the party's ``place_of_supply``
    against ``company_profile.state`` (api.md §5.7) -- never from a hardcoded
    home state, which is the bug this replaces.
    """
    lines = list(lines)
    subtotal = round2(sum((line.line_sub for line in lines), ZERO))
    summed_discount = round2(sum((line.discount_amount for line in lines), ZERO))
    total_discount = (
        summed_discount if discount_total_override is None else round2(discount_total_override)
    )
    taxable_value = non_negative(subtotal - total_discount)
    total_tax = round2(sum((line.tax_amount for line in lines), ZERO))

    freight_charges = round2(freight_charges)
    other_charges = round2(other_charges)
    round_off = round2(round_off)
    cess = round2(cess)

    total = round2(
        taxable_value + total_tax + freight_charges + other_charges + round_off
    )
    amount_paid = round2(amount_paid)
    balance_due = non_negative(total - amount_paid)

    cgst = sgst = igst = ZERO
    if is_intra_state:
        # api.md §5.7: split half/half, with the remainder on SGST so the two
        # halves always add back to total_tax exactly.
        cgst = round2(total_tax / Decimal("2"))
        sgst = round2(total_tax - cgst)
    else:
        igst = total_tax

    return DocumentTotals(
        subtotal=subtotal,
        total_discount=total_discount,
        taxable_value=taxable_value,
        total_tax=total_tax,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        cess=cess,
        freight_charges=freight_charges,
        other_charges=other_charges,
        round_off=round_off,
        total=total,
        amount_paid=amount_paid,
        balance_due=balance_due,
    )


def totals_match(computed, claimed, tolerance=ROUNDING_TOLERANCE):
    """api.md §5.7 rule 2 -- reject a client total that drifts beyond 0.01."""
    if claimed is None:
        return True
    return abs(D(computed) - D(claimed)) <= tolerance


def derive_payment_status(total, amount_paid, *, finalized=True):
    """api.md §5.7 derived payment status.

    ``Overdue`` is deliberately absent: it is layered on at read time from
    ``due_date`` plus ``balance_due > 0`` (db.md §12 -- never stored).
    """
    if not finalized:
        return "Draft"
    total = D(total)
    amount_paid = D(amount_paid)
    if amount_paid <= ZERO:
        return "Unpaid"
    if amount_paid >= total - ROUNDING_TOLERANCE:
        return "Paid"
    return "Partially Paid"


#: The single AR/AP ageing bucket set used everywhere (backend + frontend).
#: Days are counted past the due date; a not-yet-due document sits in "0-30".
AGEING_BUCKETS = ["0-30", "31-60", "61-90", "91-120", "120+"]


def ageing_bucket(days_overdue):
    """0-30 / 31-60 / 61-90 / 91-120 / 120+ -- computed on outstanding amounts.

    ``days_overdue`` <= 0 (not yet due) falls in ``0-30``.
    """
    days = int(days_overdue or 0)
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    if days <= 120:
        return "91-120"
    return "120+"


def effective_due_date(doc_date, due_date=None, credit_days=None):
    """Due date for ageing: the stored due date, else document date + credit days."""
    if due_date:
        return due_date
    if doc_date is None:
        return None
    from datetime import timedelta

    return doc_date + timedelta(days=int(credit_days or 0))


def days_overdue(as_of, doc_date, due_date=None, credit_days=None):
    """Whole days past the effective due date (never negative)."""
    effective = effective_due_date(doc_date, due_date, credit_days)
    if effective is None or as_of is None:
        return 0
    return max((as_of - effective).days, 0)


def empty_ageing_buckets():
    return {bucket: ZERO for bucket in AGEING_BUCKETS}


_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(number):
    if number < 20:
        return _ONES[number]
    tens, ones = divmod(number, 10)
    return _TENS[tens] + (" " + _ONES[ones] if ones else "")


def _three_digits(number):
    hundreds, rest = divmod(number, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_two_digits(rest))
    return " ".join(parts)


def amount_in_words(amount, currency="INR"):
    """Totals in words for the print payload (api.md §12.1).

    Indian numbering (crore / lakh / thousand) because every print template in
    the app renders a rupee document.
    """
    amount = round2(amount)
    negative = amount < ZERO
    amount = abs(amount)
    rupees = int(amount)
    paise = int((amount - Decimal(rupees)) * 100)

    if rupees == 0:
        words = "Zero"
    else:
        crore, rest = divmod(rupees, 10_000_000)
        lakh, rest = divmod(rest, 100_000)
        thousand, rest = divmod(rest, 1_000)
        chunks = []
        if crore:
            chunks.append(f"{_three_digits(crore)} Crore")
        if lakh:
            chunks.append(f"{_two_digits(lakh)} Lakh")
        if thousand:
            chunks.append(f"{_two_digits(thousand)} Thousand")
        if rest:
            chunks.append(_three_digits(rest))
        words = " ".join(chunks)

    unit = "Rupees" if currency == "INR" else currency
    subunit = "Paise" if currency == "INR" else "Cents"
    text = f"{unit} {words}"
    if paise:
        text += f" and {_two_digits(paise)} {subunit}"
    text += " Only"
    return ("Minus " + text) if negative else text
