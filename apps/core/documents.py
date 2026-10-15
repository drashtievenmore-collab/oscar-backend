"""
The shared document pattern (db.md §3).

Sales and purchase are eight near-identical headers plus lines. db.md §3 is
explicit about *not* unifying them into one polymorphic ``documents`` table --
the status machines, the FKs and the required columns genuinely differ, and a
single table becomes ``case`` statements all the way down. Instead they share
the **column vocabulary**, which is what these abstract bases are.

Freezing is the rule that makes the schema honest: a posted invoice must render
identically in three years, after the item has been renamed, the party moved
state and the tax slab changed. The denormalised copies below are not
redundancy -- they are the record.
"""
from django.db import models

from .models import LegacyIdMixin, TenantModel


class DocumentHeader(TenantModel, LegacyIdMixin):
    """db.md §3.1 -- the columns every sales/purchase document carries."""

    party = models.ForeignKey(
        "masters.Party", on_delete=models.PROTECT, related_name="+"
    )
    # -- frozen at post time -------------------------------------------------
    party_name = models.TextField(null=True, blank=True)
    party_gstin = models.TextField(null=True, blank=True)
    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)
    place_of_supply = models.TextField(null=True, blank=True)

    doc_date = models.DateField()

    # -- totals, all DERIVED from the lines (db.md §12) ----------------------
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_discount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    freight_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    #: DERIVED from payment_allocations (db.md §12).
    amount_paid = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    #: A document-level discount overrides the sum of line discounts (api.md §5.7).
    discount_override = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    notes = models.TextField(null=True, blank=True)
    terms = models.TextField(null=True, blank=True)

    salesperson = models.TextField(null=True, blank=True)
    salesperson_fk = models.ForeignKey(
        "hrms.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    broker = models.TextField(null=True, blank=True)
    broker_fk = models.ForeignKey(
        "masters.Broker", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payment_terms = models.TextField(null=True, blank=True)
    credit_days = models.IntegerField(null=True, blank=True, default=0)
    transport_responsibility = models.TextField(
        choices=[("Company", "Company"), ("Party", "Party"), ("Broker", "Broker")],
        default="Company",
    )

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancellation_reason = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def balance_due(self):
        """``max(0, total - amount_paid)`` (api.md §5.7)."""
        from .money import ZERO

        remaining = (self.total or ZERO) - (self.amount_paid or ZERO)
        return remaining if remaining > ZERO else ZERO

    @property
    def is_cancelled(self):
        return getattr(self, "status", None) == "Cancelled" or self.cancelled_at is not None

    @property
    def is_posted(self):
        return self.posted_at is not None

    def freeze_party_snapshot(self, party=None):
        """Copy the party's details onto the document (api.md §5.7).

        Called at creation. Editing the party later must not retroactively
        change a posted document.
        """
        party = party or self.party
        if party is None:
            return
        self.party_name = party.name
        self.party_gstin = party.gstin
        self.place_of_supply = party.place_of_supply
        if not self.billing_address:
            self.billing_address = party.billing_address or {}
        if not self.shipping_address:
            self.shipping_address = party.shipping_address or self.billing_address or {}


class DocumentLine(TenantModel):
    """db.md §3.2 -- the columns every document line carries."""

    line_no = models.IntegerField(default=1)
    item = models.ForeignKey(
        "masters.Item", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # -- frozen display copies ----------------------------------------------
    sku = models.TextField(null=True, blank=True)
    item_name = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    hsn_code = models.TextField(null=True, blank=True)
    uom = models.TextField(null=True, blank=True)
    #: Stock units per one line unit, frozen when the line is written (a line
    #: in CM for an item stocked in Meter carries 0.01). Stock postings use
    #: ``qty * uom_factor`` so a later UOM-master edit never rewrites history.
    uom_factor = models.DecimalField(max_digits=18, decimal_places=6, default=1)

    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rate = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    discount_pct = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tax_pct = models.DecimalField(max_digits=7, decimal_places=4, default=18)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # -- Textile & Commercial tracking (PDF §09, §11) ------------------------
    fabric_quality = models.ForeignKey(
        "masters.FabricQuality", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    design = models.ForeignKey(
        "masters.Design", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    shade = models.ForeignKey(
        "masters.Shade", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    lot_preference = models.TextField(null=True, blank=True)
    taka_count = models.IntegerField(default=0)
    landed_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    margin_pct = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    below_cost = models.BooleanField(default=False)
    below_margin = models.BooleanField(default=False)
    reserved_takas = models.JSONField(default=list, blank=True)

    # -- BOM explosion flags (api.md §4.3) ----------------------------------
    is_bom_generated = models.BooleanField(default=False)
    bom_source_item = models.ForeignKey(
        "masters.Item", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    parent_line = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="bom_children"
    )
    is_user_modified = models.BooleanField(default=False)
    parent_sku = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["line_no"]

    @property
    def is_bom_part(self):
        """JSON ``isBomPart`` is derived as ``parent_line_id is not null``."""
        return self.parent_line_id is not None

    def freeze_item_snapshot(self, item=None):
        item = item or self.item
        if item is None:
            return
        self.sku = item.sku
        self.item_name = item.name
        self.hsn_code = self.hsn_code or item.hsn_code
        self.uom = self.uom or item.uom
        from apps.masters.uom import line_factor

        self.uom_factor = line_factor(self.client_id, item, self.uom)
        if not self.description:
            self.description = item.description


def total_check_constraint(table_name):
    """db.md §3.1 -- the header total check, added to every document.

        total = subtotal - total_discount + total_tax
                + freight_charges + other_charges + round_off

    The server recomputes totals and rejects a client mismatch (api.md §5.7);
    this constraint turns a bug in the recompute into a loud failure instead of
    a wrong ledger.
    """
    return models.CheckConstraint(
        condition=models.Q(
            total=(
                models.F("subtotal")
                - models.F("total_discount")
                + models.F("total_tax")
                + models.F("freight_charges")
                + models.F("other_charges")
                + models.F("round_off")
            )
        ),
        name=f"ck_{table_name}_total",
    )


def number_unique_constraint(table_name, field_name):
    """``unique (client_id, number) where deleted_at is null`` (db.md §3.1).

    Partial because every unique index on a soft-deletable table is partial
    (db.md §1.4), and nullable because drafts have no number until they post.
    """
    return models.UniqueConstraint(
        fields=["client", field_name],
        condition=models.Q(deleted_at__isnull=True, **{f"{field_name}__isnull": False}),
        name=f"uq_{table_name}_number",
    )
