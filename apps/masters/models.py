"""
Shared masters (db.md §4, api.md §4).

Parties, items, categories, units, locations, serials and the machine BOM.
One ``parties`` table serves customers and vendors; the UI shows it three ways
(``/parties``, ``/crm/customers``, vendor pickers) but there is one record.
"""
from django.db import models

from apps.core.models import LegacyIdMixin, TenantModel

# api.md §4.2 -- the typedef says Machine | Part | Standalone, but the code also
# branches on Service, Component and Consumable. Accept all six. `Service` items
# hold no stock and are excluded from movements and valuation.
ITEM_KINDS = [
    ("Machine", "Machine"),
    ("Part", "Part"),
    ("Standalone", "Standalone"),
    ("Service", "Service"),
    ("Component", "Component"),
    ("Consumable", "Consumable"),
    ("Fabric", "Fabric"),
]

#: Kinds that never hold stock (api.md §4.2).
STOCKLESS_ITEM_KINDS = {"Service"}

#: Permanent fabric catalogue. These are master rows stored in the database
#: (``masters.Fabric``), never frontend mock data. Seeded by migration 0007
#: and ``seed_fabrics`` for every tenant.
DEFAULT_FABRICS = [
    "Cotton",
    "Linen",
    "Silk",
    "Wool",
    "Polyester",
    "Nylon",
    "Spandex (Elastane)",
    "Rayon (Viscose)",
    "Denim",
    "Velvet",
    "Chiffon",
    "Georgette",
]

DIMENSION_UNITS = [("mm", "mm"), ("cm", "cm"), ("m", "m"), ("in", "in")]


# ---------------------------------------------------------------------------
# db.md §4.1 -- Parties
# ---------------------------------------------------------------------------
class Party(TenantModel, LegacyIdMixin):
    TYPES = [
        ("Customer", "Customer"),
        ("Vendor", "Vendor"),
        ("Both", "Both"),
        ("Transporter", "Transporter"),
    ]
    #: Supplier classification (db.md §4.1 addendum) -- only meaningful when
    #: ``type == "Vendor"``; left null for every other role.
    VENDOR_TYPES = [
        ("Manufacturing", "Manufacturing"),
        ("Dyeing - Other Process", "Dyeing - Other Process"),
    ]
    #: Fleet classification -- only meaningful when ``type == "Transporter"``.
    VEHICLE_TYPES = [
        ("Truck", "Truck"),
        ("Mini Truck / LCV", "Mini Truck / LCV"),
        ("Trailer", "Trailer"),
        ("Container", "Container"),
        ("Tanker", "Tanker"),
        ("Tempo", "Tempo"),
        ("Other", "Other"),
    ]
    CAPACITY_UNITS = [
        ("Tons", "Tons"),
        ("Kg", "Kg"),
        ("Litres", "Litres"),
        ("Taka", "Taka"),
    ]
    GST_TREATMENTS = [
        ("Registered Business", "Registered Business"),
        ("Unregistered Business", "Unregistered Business"),
        ("Consumer", "Consumer"),
        ("Overseas", "Overseas"),
        ("SEZ", "SEZ"),
        ("Deemed Export", "Deemed Export"),
    ]
    STATUSES = [("Active", "Active"), ("On Hold", "On Hold"), ("Inactive", "Inactive")]

    code = models.TextField()
    type = models.TextField(choices=TYPES, default="Customer")
    vendor_type = models.TextField(choices=VENDOR_TYPES, null=True, blank=True)
    #: Transporter fleet details (``type == "Transporter"`` only).
    vehicle_number = models.TextField(null=True, blank=True)
    vehicle_type = models.TextField(choices=VEHICLE_TYPES, null=True, blank=True)
    vehicle_capacity = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    vehicle_capacity_unit = models.TextField(
        choices=CAPACITY_UNITS, null=True, blank=True
    )
    name = models.TextField()
    phone = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    gst_treatment = models.TextField(choices=GST_TREATMENTS, default="Unregistered Business")
    gstin = models.TextField(null=True, blank=True)
    gst_notes = models.TextField(null=True, blank=True)
    #: Compared with company_profile.state to decide CGST+SGST vs IGST.
    place_of_supply = models.TextField(null=True, blank=True)

    tds_applicable = models.BooleanField(default=False)
    tds_section = models.TextField(null=True, blank=True)
    tds_rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    tcs_applicable = models.BooleanField(default=False)
    tcs_rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)

    ledger_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    credit_limit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    payment_terms = models.TextField(null=True, blank=True)

    bank_account_number = models.TextField(null=True, blank=True)
    ifsc_code = models.TextField(null=True, blank=True)
    bank_name = models.TextField(null=True, blank=True)
    account_holder_name = models.TextField(null=True, blank=True)

    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    #: DERIVED from journal_lines (db.md §12). Never accept it as a write --
    #: it is recomputed on every posting that touches the party and stored
    #: only for list-view performance.
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)
    status = models.TextField(choices=STATUSES, default="Active")

    class Meta:
        db_table = "parties"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_parties_code",
            ),
            models.UniqueConstraint(
                fields=["client", "gstin"],
                condition=models.Q(gstin__isnull=False, deleted_at__isnull=True),
                name="uq_parties_gstin",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "type"],
                name="ix_parties_type",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(fields=["client", "name"], name="ix_parties_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_customer(self):
        return self.type in ("Customer", "Both")

    @property
    def is_vendor(self):
        return self.type in ("Vendor", "Both")


class PartyContact(TenantModel):
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="contacts")
    name = models.TextField()
    role = models.TextField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "party_contacts"
        ordering = ["-is_primary", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["party"],
                condition=models.Q(is_primary=True, deleted_at__isnull=True),
                name="uq_party_primary_contact",
            )
        ]


# ---------------------------------------------------------------------------
# db.md §4.2 -- Items and inventory master
# ---------------------------------------------------------------------------
class ItemCategory(TenantModel, LegacyIdMixin):
    KINDS = [("machine", "machine"), ("stock", "stock")]

    name = models.TextField()
    code = models.TextField()
    kind = models.TextField(choices=KINDS, default="stock")
    description = models.TextField(null=True, blank=True)
    has_sub_parts = models.BooleanField(default=False)
    lead_time_days = models.IntegerField(default=0)
    #: api.md §4.2 -- the HSN default for this family, resolved on item create.
    default_hsn_code = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "item_categories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_item_categories_code",
            )
        ]

    def __str__(self):
        return self.name


class CategoryCustomField(TenantModel):
    """Defines the keys allowed in ``items.custom_field_values`` (db.md §1.7)."""

    TYPES = [
        ("text", "text"),
        ("number", "number"),
        ("dropdown", "dropdown"),
        ("boolean", "boolean"),
    ]

    category = models.ForeignKey(
        ItemCategory, on_delete=models.CASCADE, related_name="custom_fields"
    )
    name = models.TextField()
    type = models.TextField(choices=TYPES, default="text")
    options = models.JSONField(null=True, blank=True)
    required = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "category_custom_fields"
        ordering = ["sort_order", "name"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(type="dropdown") | models.Q(options__isnull=False),
                name="ck_ccf_options",
            )
        ]


class Unit(TenantModel):
    code = models.TextField()
    label = models.TextField()

    class Meta:
        db_table = "units"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_units_code",
            )
        ]

    def __str__(self):
        return self.code


class Location(TenantModel, LegacyIdMixin):
    """Warehouses and zones. ``Transit`` exists because a transfer parks stock
    between the ``TRANSFER_OUT`` and ``TRANSFER_IN`` movements (db.md §7.4)."""

    TYPES = [
        ("Warehouse", "Warehouse"),
        ("Zone", "Zone"),
        ("Transit", "Transit"),
        ("Scrap", "Scrap"),
    ]

    code = models.TextField()
    name = models.TextField()
    type = models.TextField(choices=TYPES, default="Warehouse")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    address = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "locations"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_locations_code",
            )
        ]

    def __str__(self):
        return self.name


class Fabric(TenantModel):
    """Permanent fabric catalogue (Cotton, Silk, Denim, ...).

    Backs the "Select fabric..." dropdown. Rows live in the database per
    tenant -- the frontend must fetch ``GET /inventory/fabrics/``, never a
    hardcoded mock list. ``Item.fabric_quality`` stores the selected fabric
    name as free text for history; this table is the selectable master.
    """

    name = models.TextField()
    code = models.TextField()
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "fabrics"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_fabrics_name",
            ),
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_fabrics_code",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "name"], name="ix_fabrics_name"),
        ]

    def __str__(self):
        return self.name


class Item(TenantModel, LegacyIdMixin):
    """db.md §4.2.

    ``availableQty``, ``reservedQty`` and ``status`` are deliberately **not**
    columns. The mock object carries them because the frontend recomputes them
    on every render; here they are derived from movements and open orders
    (db.md §7.3) and joined on at read, so the JSON contract is unchanged.
    """

    TRACKING_MODES = [("Quantity", "Quantity"), ("Serial", "Serial")]
    LIFECYCLE = [("Active", "Active"), ("Draft", "Draft"), ("Archived", "Archived")]

    sku = models.TextField()
    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    category = models.ForeignKey(
        ItemCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="items"
    )
    item_kind = models.TextField(choices=ITEM_KINDS, default="Standalone")
    hsn_code = models.TextField(null=True, blank=True)

    uom = models.TextField(default="Nos")
    purchase_unit = models.TextField(null=True, blank=True)
    sales_unit = models.TextField(null=True, blank=True)
    unit_conversion_factor = models.DecimalField(
        max_digits=18, decimal_places=6, default=1
    )

    # Weight-based receiving -- the core fabrication rule (api.md §6.3).
    is_weight_item = models.BooleanField(default=False)
    theoretical_weight = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    weight_unit = models.TextField(default="kg")
    tolerance_pct = models.DecimalField(max_digits=7, decimal_places=4, default=2)

    tracking_mode = models.TextField(choices=TRACKING_MODES, default="Quantity")
    reorder_level = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    cost_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    selling_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    default_location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    vendor = models.ForeignKey(
        Party, null=True, blank=True, on_delete=models.SET_NULL, related_name="supplied_items"
    )
    tax_pct = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    lifecycle_status = models.TextField(choices=LIFECYCLE, default="Active")
    custom_field_values = models.JSONField(default=dict, blank=True)

    # Sheet spec
    has_sheet_spec = models.BooleanField(default=False)
    sheet_height = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    sheet_height_unit = models.TextField(choices=DIMENSION_UNITS, null=True, blank=True)
    sheet_width = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    sheet_width_unit = models.TextField(choices=DIMENSION_UNITS, null=True, blank=True)
    sheet_length = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    sheet_length_unit = models.TextField(choices=DIMENSION_UNITS, null=True, blank=True)
    sheet_weight_kg = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    dimension_unit = models.TextField(null=True, blank=True)  # legacy single-axis unit

    # Fabric spec (``item_kind == "Fabric"``) — the textile catalogue fields:
    # quality (Cotton/Polyester/…), weave design, colour, width and GSM.
    fabric_quality = models.TextField(null=True, blank=True)
    fabric_design = models.TextField(null=True, blank=True)
    fabric_color = models.TextField(null=True, blank=True)
    #: Width in inches (58", 60", …) — the way greige fabric is traded.
    fabric_width = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    fabric_gsm = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "items"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "sku"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_items_sku",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "category"],
                name="ix_items_category",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(fields=["client", "sku"], name="ix_items_sku"),
            models.Index(fields=["client", "item_kind"], name="ix_items_kind"),
        ]

    def __str__(self):
        return f"{self.sku} {self.name}"

    @property
    def holds_stock(self):
        return self.item_kind not in STOCKLESS_ITEM_KINDS


class CategoryPart(TenantModel):
    """Category default parts (``categoryParts`` in the frontend)."""

    category = models.ForeignKey(
        ItemCategory, on_delete=models.CASCADE, related_name="default_parts"
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="+")
    default_qty = models.DecimalField(max_digits=18, decimal_places=4, default=1)

    class Meta:
        db_table = "category_parts"
        constraints = [
            models.UniqueConstraint(fields=["category", "item"], name="uq_category_parts")
        ]


class ItemPart(TenantModel):
    """Machine BOM (``itemParts``).

    A machine's parts may themselves be machines; cycles are rejected on insert
    with a recursive check in the service layer, because the self-reference
    constraint only catches the trivial case (db.md §4.2).
    """

    parent_item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="bom_lines")
    part_item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="used_in_boms")
    required_qty = models.DecimalField(max_digits=18, decimal_places=4, default=1)

    class Meta:
        db_table = "item_parts"
        constraints = [
            models.UniqueConstraint(
                fields=["parent_item", "part_item"], name="uq_item_parts"
            ),
            models.CheckConstraint(
                condition=~models.Q(parent_item=models.F("part_item")),
                name="ck_item_parts_no_self",
            ),
            models.CheckConstraint(
                condition=models.Q(required_qty__gt=0), name="ck_item_parts_qty"
            ),
        ]


# ---------------------------------------------------------------------------
# db.md §4.3 -- Serial numbers
# ---------------------------------------------------------------------------
class ItemSerial(TenantModel):
    """Replaces ``items.serialNumbers[]``.

    An array cannot carry per-serial status, location, warranty link or
    movement history -- api.md §4.2 requires filtering by status and §5.9
    requires knowing which serials were already returned.
    """

    STATUSES = [
        ("available", "available"),
        ("reserved", "reserved"),
        ("sold", "sold"),
        ("returned", "returned"),
        ("faulty", "faulty"),
        ("scrapped", "scrapped"),
        ("in_transit", "in_transit"),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="serials")
    serial_no = models.TextField()
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.TextField(choices=STATUSES, default="available")
    batch_number = models.TextField(null=True, blank=True)
    received_movement = models.ForeignKey(
        "inventory.StockMovement",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    sold_movement = models.ForeignKey(
        "inventory.StockMovement",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    warranty_card = models.ForeignKey(
        "sales.WarrantyCard",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "item_serials"
        ordering = ["serial_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "item", "serial_no"], name="uq_item_serials"
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "item", "status"], name="ix_item_serials_status"
            )
        ]

    def __str__(self):
        return self.serial_no


class DocumentLineSerial(TenantModel):
    """Serial selection per document line (db.md §3.2).

    A ``line_table`` discriminator avoids nine near-empty tables; the cost is
    no FK on ``line_id``, acceptable for a child only ever written alongside
    its parent.
    """

    line_table = models.TextField()
    line_id = models.UUIDField()
    serial = models.ForeignKey(ItemSerial, on_delete=models.PROTECT, related_name="line_links")

    class Meta:
        db_table = "document_line_serials"
        constraints = [
            models.UniqueConstraint(
                fields=["line_table", "line_id", "serial"], name="uq_document_line_serials"
            )
        ]
        indexes = [models.Index(fields=["serial"], name="ix_dls_serial")]
