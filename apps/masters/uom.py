"""Unit-of-measure resolution and conversion -- the one place UOM rules live.

Documents and items store the unit as text (``"Mtr"``, ``"Meter"``) rather
than a foreign key, so this module turns that text into a ``Unit`` row and
decides whether two units can be converted.

Rules:

* Same unit -> quantity is preserved, factor 1.
* Length, Weight, Area and Volume convert within their own type through the
  base-unit ``conversion_factor`` (1 CM = 0.01 Meter, 1 Gram = 0.001 Kg).
* Roll, Piece and Other never convert -- a taka or a roll has no fixed length,
  so its actual metres are captured on the receipt, never derived.
* Different types never convert. In particular metres are never turned into
  kilograms: that needs a fabric-specific rule (GSM x width) the master
  does not hold.
"""
from decimal import Decimal

from apps.core.exceptions import ValidationFailed

CONVERTIBLE_TYPES = {"Length", "Weight", "Area", "Volume"}

#: Common spellings of the same unit. Existing items say "Mtr" while the
#: master row is "Meter"; both must resolve to the same unit.
ALIASES = {
    "m": "meter", "mtr": "meter", "mtrs": "meter", "meter": "meter", "meters": "meter",
    "metre": "meter", "metres": "meter",
    "cm": "centimeter", "centimeter": "centimeter", "centimetre": "centimeter",
    "yd": "yard", "yds": "yard", "yard": "yard", "yards": "yard",
    "kg": "kilogram", "kgs": "kilogram", "kilogram": "kilogram", "kilograms": "kilogram",
    "g": "gram", "gm": "gram", "gms": "gram", "gram": "gram", "grams": "gram",
    "pc": "piece", "pcs": "piece", "piece": "piece", "pieces": "piece",
    "nos": "number", "no": "number", "number": "number",
    "roll": "roll", "rolls": "roll",
    "taka": "taka", "takas": "taka",
}

ONE = Decimal("1")


def _key(text):
    cleaned = " ".join(str(text or "").strip().lower().split())
    return ALIASES.get(cleaned, cleaned)


class UnitBook:
    """A tenant's units, loaded once and looked up by code, label or alias."""

    def __init__(self, client_id):
        from apps.masters.models import Unit

        self.units = list(
            Unit.objects.filter(client_id=client_id, deleted_at__isnull=True)
        )
        self._index = {}
        for unit in self.units:
            for text in (unit.code, unit.label):
                if text:
                    self._index.setdefault(_key(text), unit)

    @property
    def empty(self):
        return not self.units

    def resolve(self, text):
        if not text:
            return None
        return self._index.get(_key(text))


def same_unit_text(a, b):
    """``"Mtr"`` and ``"Meter"`` name the same unit even without a master row."""
    return bool(a) and bool(b) and _key(a) == _key(b)


def compatible(a, b):
    """Can a quantity in unit ``a`` be expressed in unit ``b``?"""
    if a is None or b is None:
        return False
    if a.pk == b.pk:
        return True
    return a.uom_type == b.uom_type and a.uom_type in CONVERTIBLE_TYPES


def factor_between(a, b):
    """How many ``b`` one ``a`` is worth (1 Meter -> 100 CM)."""
    if a.pk == b.pk:
        return ONE
    if not compatible(a, b):
        raise ValueError(f"{a.code} cannot be converted to {b.code}.")
    return Decimal(a.conversion_factor) / Decimal(b.conversion_factor)


def convert(quantity, a, b):
    return Decimal(quantity) * factor_between(a, b)


def line_factor(client_id, item, uom_text, *, book=None, field="uom"):
    """Stock units per one line unit for ``item`` -- frozen onto document lines.

    Returns 1 when the line uses the item's own unit (or leaves it blank).
    Raises ``ValidationFailed`` when the line names a unit that is unknown,
    inactive or not convertible to the item's stock unit.
    """
    if item is None or not uom_text or same_unit_text(uom_text, item.uom):
        return ONE
    book = book or UnitBook(client_id)
    line_unit = book.resolve(uom_text)
    stock_unit = book.resolve(item.uom)
    if line_unit is not None and stock_unit is not None and line_unit.pk == stock_unit.pk:
        return ONE
    if line_unit is None:
        raise ValidationFailed(
            f"Unit {uom_text} is not in the UOM master.",
            field_errors={field: [f"Unit {uom_text} is not in the UOM master."]},
        )
    if not line_unit.is_active:
        raise ValidationFailed(
            f"Unit {line_unit.code} is inactive.",
            field_errors={field: [f"Unit {line_unit.code} is inactive."]},
        )
    if stock_unit is None:
        raise ValidationFailed(
            f"{item.name} is stocked in {item.uom}, which is not in the UOM master, "
            f"so it cannot be traded in {line_unit.code}.",
            field_errors={field: [f"Use {item.uom} for {item.name}."]},
        )
    if not compatible(line_unit, stock_unit):
        raise ValidationFailed(
            f"{line_unit.code} ({line_unit.uom_type}) cannot be converted to "
            f"{stock_unit.code} ({stock_unit.uom_type}) for {item.name}.",
            field_errors={field: [
                f"Choose a {stock_unit.uom_type.lower()} unit -- {item.name} is stocked in "
                f"{stock_unit.code}."
            ]},
        )
    return factor_between(line_unit, stock_unit)


def to_stock_qty(line_or_qty, factor=None):
    """Quantity in the item's stock unit for a document line (or qty + factor)."""
    if factor is None:
        factor = getattr(line_or_qty, "uom_factor", None) or ONE
        quantity = line_or_qty.qty
    else:
        quantity = line_or_qty
    return Decimal(quantity) * Decimal(factor or ONE)


def between_lines(quantity, from_line, to_line):
    """Re-express ``quantity`` (in ``from_line``'s unit) in ``to_line``'s unit,
    for roll-ups such as bill -> PO received or challan -> order dispatched."""
    src = Decimal(getattr(from_line, "uom_factor", None) or ONE)
    dst = Decimal(getattr(to_line, "uom_factor", None) or ONE)
    if src == dst:
        return Decimal(quantity)
    return Decimal(quantity) * src / dst


def item_unit_usage(client_id, unit):
    """How many live items and document lines name ``unit`` (by code, label or alias)."""
    from django.apps import apps as django_apps

    from apps.core.documents import DocumentLine
    from apps.masters.models import Item

    keys = {_key(unit.code), _key(unit.label)}

    def matches(values):
        return sum(count for value, count in values if _key(value) in keys)

    def grouped(queryset, field):
        from django.db.models import Count

        return queryset.values_list(field).annotate(n=Count("id")).values_list(field, "n")

    items = matches(grouped(
        Item.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(uom=""), "uom"
    ))
    lines = 0
    for model in django_apps.get_models():
        if issubclass(model, DocumentLine) and not model._meta.abstract:
            lines += matches(grouped(
                model.objects.filter(client_id=client_id).exclude(uom__isnull=True), "uom"
            ))
    return {"items": items, "documentLines": lines}
