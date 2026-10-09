"""
Document numbering (api.md §1.7, db.md §2.7).

The server owns every human-readable number; the client must never invent one.
Numbers are gapless per series, per financial year, per tenant, and are
allocated *inside* the creating transaction so a rollback returns the number.

Why not a Postgres sequence: sequences are explicitly non-transactional and
will leave gaps, which a GST audit will flag (db.md §2.7).
"""
from django.db import connection, transaction
from django.utils import timezone

from .exceptions import Codes, EvenmoreAPIError

#: api.md §1.7 -- series defaults. The tenant may override any of these through
#: ``/settings/numbering/``; these are what a fresh tenant starts with.
#: (prefix, reset_policy, pad_width, separator)
SERIES_DEFAULTS = {
    # Sales
    "QT": ("QT", "fy", 4, "-"),
    "SO": ("SO", "fy", 4, "-"),
    "PI": ("PI", "fy", 4, "-"),
    "INV": ("INV", "fy", 4, "-"),
    "DC": ("DC", "fy", 4, "-"),
    "PAY-IN": ("PAY-IN", "fy", 4, "-"),
    "SR": ("SR", "fy", 4, "-"),
    "WC": ("WC", "fy", 5, "-"),
    # Purchase
    "PO": ("PO", "fy", 4, "-"),
    "BILL": ("BILL", "fy", 4, "-"),
    "GRN": ("GRN", "fy", 4, "-"),
    "PAY-OUT": ("PAY-OUT", "fy", 4, "-"),
    "PR": ("PR", "fy", 4, "-"),
    "EXP": ("EXP", "fy", 4, "-"),
    # Inventory
    "RMA": ("RMA", "fy", 4, "-"),
    "TR": ("TR", "fy", 4, "-"),
    "REQ": ("REQ", "never", 4, "-"),
    "TKT": ("TKT", "never", 4, "-"),
    "AUD": ("AUD", "fy", 4, "-"),
    # Accounts
    "JE": ("JE", "fy", 3, "-"),
    # PMS -- calendar year, three digits: PRJ-2026-001
    "PRJ": ("PRJ", "yearly", 3, "-"),
    "TSK": ("TSK", "never", 4, "-"),
    # CRM -- L00000185, no separator, never resets
    "LEAD": ("L", "never", 8, ""),
    "DEAL": ("DEAL", "fy", 4, "-"),
    "CON": ("CON", "fy", 4, "-"),
    # Masters / HRMS
    "CUST": ("CUST", "never", 4, "-"),
    "VEND": ("VEND", "never", 4, "-"),
    "TRNS": ("TRNS", "never", 4, "-"),
    "EMP": ("EMP", "never", 4, ""),
    "ASSET": ("AST", "never", 4, "-"),
    # Grey-fabric production wiring (HRMS_ERP_CHANGES.md)
    "PROD": ("PROD", "fy", 4, "-"),
    "INC": ("INC", "fy", 4, "-"),
    # Job work
    "PP": ("PP", "fy", 3, "-"),
    "JWO": ("JWO", "fy", 3, "-"),
    "VPI": ("VPI", "fy", 4, "-"),
}


def financial_year_label(client, on_date=None, reset_policy="fy"):
    """The label that goes in the middle of the number.

    ``fy``      -- the year the tenant's financial year started (fy_start_month)
    ``yearly``  -- the calendar year
    ``monthly`` -- YYYYMM
    ``never``   -- empty, and the separator collapses
    """
    on_date = on_date or timezone.localdate()
    if reset_policy == "never":
        return ""
    if reset_policy == "monthly":
        return f"{on_date.year}{on_date.month:02d}"
    if reset_policy == "yearly":
        return str(on_date.year)
    fy_start_month = getattr(client, "fy_start_month", 4) or 4
    year = on_date.year if on_date.month >= fy_start_month else on_date.year - 1
    return str(year)


def format_number(prefix, separator, fy_label, value, pad_width):
    """``{prefix}{sep}{fy_label}{sep}{lpad(value)}`` -> ``INV-2026-0044``."""
    body = str(value).rjust(pad_width, "0")
    parts = [prefix]
    if fy_label:
        parts.append(fy_label)
    parts.append(body)
    return separator.join(parts) if separator else "".join(parts)


@transaction.atomic
def allocate_number(client, series_key, on_date=None):
    """Allocate the next number in a series. Call inside the creating transaction.

    Takes ``pg_advisory_xact_lock`` on (client, series, fy) so concurrent
    allocations serialise on just this tenant's series rather than the table,
    and the lock is released at commit or rollback.
    """
    from .models import NumberSeries

    client_id = getattr(client, "id", client)
    prefix, reset_policy, pad_width, separator = SERIES_DEFAULTS.get(
        series_key, (series_key, "fy", 4, "-")
    )
    fy_label = financial_year_label(client, on_date=on_date, reset_policy=reset_policy)

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"{client_id}{series_key}{fy_label}"],
            )

    series, created = NumberSeries.objects.get_or_create(
        client_id=client_id,
        series_key=series_key,
        fy_label=fy_label,
        defaults={
            "prefix": prefix,
            "reset_policy": reset_policy,
            "pad_width": pad_width,
            "separator": separator,
            "next_value": 1,
        },
    )
    if not created:
        # Re-read under the advisory lock so two allocators cannot both read 41.
        series = NumberSeries.objects.select_for_update().get(pk=series.pk)

    allocated = series.next_value
    series.next_value = allocated + 1
    series.save(update_fields=["next_value"])

    try:
        return format_number(
            series.prefix, series.separator, series.fy_label, allocated, series.pad_width
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise EvenmoreAPIError(
            "Could not allocate a document number.",
            code=Codes.NUMBER_ALLOCATION_FAILED,
            detail=str(exc),
        ) from exc


def peek_next(client, series_key, on_date=None):
    """The number that *would* be allocated. For previews only -- never store it."""
    from .models import NumberSeries

    prefix, reset_policy, pad_width, separator = SERIES_DEFAULTS.get(
        series_key, (series_key, "fy", 4, "-")
    )
    fy_label = financial_year_label(client, on_date=on_date, reset_policy=reset_policy)
    series = NumberSeries.objects.filter(
        client_id=getattr(client, "id", client), series_key=series_key, fy_label=fy_label
    ).first()
    value = series.next_value if series else 1
    if series:
        prefix, separator, pad_width = series.prefix, series.separator, series.pad_width
    return format_number(prefix, separator, fy_label, value, pad_width)


def seed_series_for_client(client):
    """Create every default series row for a new tenant (db.md §14.1 step 4)."""
    from .models import NumberSeries

    created = []
    for series_key, (prefix, reset_policy, pad_width, separator) in SERIES_DEFAULTS.items():
        fy_label = financial_year_label(client, reset_policy=reset_policy)
        obj, was_created = NumberSeries.objects.get_or_create(
            client=client,
            series_key=series_key,
            fy_label=fy_label,
            defaults={
                "prefix": prefix,
                "reset_policy": reset_policy,
                "pad_width": pad_width,
                "separator": separator,
            },
        )
        if was_created:
            created.append(obj)
    return created
