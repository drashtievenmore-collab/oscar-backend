"""Serializers for parties, items, categories, units, locations and BOM (api.md §4)."""
import re

from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    CategoryCustomField,
    CategoryPart,
    Fabric,
    Item,
    ItemCategory,
    ItemPart,
    ItemSerial,
    Location,
    Party,
    PartyContact,
    Unit,
)


def slug_code(name, fallback):
    """A short, stable code derived from a name -- ``Mild Steel`` -> ``MILD-STEEL``.

    Category and location codes are labels the user reads, not numbers from the
    §1.7 series, so a missing one is filled in here rather than allocated.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
    return cleaned[:24] or fallback


# ---------------------------------------------------------------------------
# Parties (api.md §4.1)
# ---------------------------------------------------------------------------
class PartyContactSerializer(BaseModelSerializer):
    class Meta:
        model = PartyContact
        fields = ["id", "name", "role", "phone", "email", "is_primary"]


class PartySerializer(BaseModelSerializer):
    """The exact shape api.md §4.1 documents.

    ``balance`` is read-only: it is derived from the ledger and never written
    directly (db.md §4.1).
    """

    contacts = PartyContactSerializer(many=True, read_only=True)
    ledgerAccount = serializers.CharField(source="ledger_account.name", read_only=True)
    ledgerAccountId = TenantPrimaryKeyRelatedField(
        source="ledger_account", model="accounting.Account", required=False, allow_null=True
    )

    class Meta:
        model = Party
        fields = [
            "id", "code", "type", "vendor_type",
            "vehicle_number", "vehicle_type", "vehicle_capacity", "vehicle_capacity_unit",
            "name", "phone", "email",
            "gst_treatment", "gstin", "gst_notes", "place_of_supply",
            "tds_applicable", "tds_section", "tds_rate",
            "tcs_applicable", "tcs_rate",
            "ledgerAccount", "ledgerAccountId", "credit_limit", "payment_terms",
            "bank_account_number", "ifsc_code", "bank_name", "account_holder_name",
            "opening_balance", "balance",
            "billing_address", "shipping_address", "contacts", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["balance", "created_at", "updated_at"]
        # api.md 1.7: the server allocates the CUST-/VEND- code in
        # ``PartyViewSet.perform_create``; the client must never invent one, so
        # it is optional on the wire rather than required.
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        existing = Party.objects.filter(
            client_id=client_id, code=value, deleted_at__isnull=True
        )
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A party with this code already exists.")
        return value


class PartySummarySerializer(BaseSerializer):
    """``GET /parties/{id}/summary/`` -- the Customer 360 drawer.

    Replaces ``Customer360Drawer``'s reduce over four context arrays, which
    under-reports the moment those lists are paginated
    (api-integration.md §9.1.3).
    """

    partyId = serializers.CharField()
    name = serializers.CharField()
    balance = MoneyField()
    creditLimit = MoneyField(allow_null=True)
    outstanding = MoneyField()
    lifetimeValue = MoneyField()
    openOrders = serializers.IntegerField()
    openInvoices = serializers.IntegerField()
    lastOrderDate = serializers.DateField(allow_null=True)
    lastInvoiceDate = serializers.DateField(allow_null=True)
    unallocatedAdvance = MoneyField()


# ---------------------------------------------------------------------------
# Categories, units, locations
# ---------------------------------------------------------------------------
class CategoryCustomFieldSerializer(BaseModelSerializer):
    class Meta:
        model = CategoryCustomField
        fields = ["id", "name", "type", "options", "required", "sort_order"]


class ItemCategorySerializer(BaseModelSerializer):
    customFields = CategoryCustomFieldSerializer(
        source="custom_fields", many=True, required=False
    )
    itemCount = serializers.SerializerMethodField()

    class Meta:
        model = ItemCategory
        fields = [
            "id", "name", "code", "kind", "description", "has_sub_parts",
            "lead_time_days", "default_hsn_code", "customFields", "itemCount",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        """Derive the short code from the name when the client omits it."""
        attrs = super().validate(attrs)
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(attrs.get("name"), "CAT")
        return attrs

    def get_itemCount(self, category):
        cached = getattr(category, "item_count", None)
        if cached is not None:
            return cached
        return category.items.filter(deleted_at__isnull=True).count()

    def create(self, validated_data):
        custom_fields = validated_data.pop("custom_fields", [])
        category = super().create(validated_data)
        self._sync_custom_fields(category, custom_fields)
        return category

    def update(self, instance, validated_data):
        custom_fields = validated_data.pop("custom_fields", None)
        category = super().update(instance, validated_data)
        if custom_fields is not None:
            self._sync_custom_fields(category, custom_fields)
        return category

    def _sync_custom_fields(self, category, rows):
        CategoryCustomField.objects.filter(category=category).delete()
        CategoryCustomField.objects.bulk_create(
            [
                CategoryCustomField(
                    client_id=category.client_id, category=category, **row
                )
                for row in rows
            ]
        )


class UnitSerializer(BaseModelSerializer):
    class Meta:
        model = Unit
        fields = ["id", "code", "label", "created_at"]


class LocationSerializer(BaseModelSerializer):
    class Meta:
        model = Location
        fields = [
            "id", "code", "name", "type", "parent", "address", "is_active",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(attrs.get("name"), "LOC")
        return attrs


class FabricSerializer(BaseModelSerializer):
    """Permanent fabric master -- the "Select fabric..." dropdown source."""

    class Meta:
        model = Fabric
        fields = [
            "id", "name", "code", "description", "is_active",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(attrs.get("name"), "FABRIC")
        return attrs

    def validate_name(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("Fabric name is required.")
        client_id = self.context.get("client_id")
        existing = Fabric.objects.filter(
            client_id=client_id, name__iexact=cleaned, deleted_at__isnull=True
        )
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("This fabric already exists.")
        return cleaned


# ---------------------------------------------------------------------------
# Items (api.md §4.2)
# ---------------------------------------------------------------------------
class ItemSerializer(BaseModelSerializer):
    """The ``InventoryItem`` typedef plus the fields ``addInventoryItem`` adds.

    ``availableQty``, ``reservedQty`` and ``status`` are **not columns** -- they
    are joined on from the movement ledger at read time (db.md §4.2), so the
    JSON contract is unchanged while the source of truth moved.
    """

    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", queryset=ItemCategory.objects.all(), required=False, allow_null=True
    )
    vendor = serializers.CharField(source="vendor.name", read_only=True)
    vendorId = TenantPrimaryKeyRelatedField(
        source="vendor", queryset=Party.objects.all(), required=False, allow_null=True
    )
    location = serializers.CharField(source="default_location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="default_location", queryset=Location.objects.all(), required=False,
        allow_null=True,
    )
    unitConversionFactor = serializers.DecimalField(
        source="unit_conversion_factor", max_digits=18, decimal_places=6,
        coerce_to_string=False, required=False,
    )

    # -- derived, read-only -------------------------------------------------
    availableQty = serializers.SerializerMethodField()
    reservedQty = serializers.SerializerMethodField()
    onHandQty = serializers.SerializerMethodField()
    damagedQty = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    serialNumbers = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "sku", "name", "description", "category", "categoryId",
            "item_kind", "vendor", "vendorId",
            "uom", "purchase_unit", "sales_unit", "unitConversionFactor",
            "availableQty", "reservedQty", "onHandQty", "damagedQty",
            "reorder_level", "location", "locationId", "status", "lifecycle_status",
            "cost_price", "selling_price", "hsn_code", "tax_pct",
            "tracking_mode", "serialNumbers",
            "is_weight_item", "theoretical_weight", "weight_unit", "tolerance_pct",
            "has_sheet_spec", "sheet_height", "sheet_height_unit",
            "sheet_width", "sheet_width_unit", "sheet_length", "sheet_length_unit",
            "sheet_weight_kg", "dimension_unit",
            "fabric_quality", "fabric_design", "fabric_color",
            "fabric_width", "fabric_gsm",
            "custom_field_values",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_availableQty(self, item):
        return getattr(item, "available_qty", None)

    def get_reservedQty(self, item):
        return getattr(item, "reserved_qty", None)

    def get_onHandQty(self, item):
        return getattr(item, "on_hand_qty", None)

    def get_damagedQty(self, item):
        return getattr(item, "damaged_qty", None)

    def get_status(self, item):
        return getattr(item, "stock_status", None)

    def get_serialNumbers(self, item):
        """Kept for contract compatibility; the source of truth is
        ``item_serials``, which an array could never carry (db.md §4.3)."""
        if item.tracking_mode != "Serial":
            return []
        cached = getattr(item, "_serial_numbers", None)
        if cached is not None:
            return cached
        return list(
            item.serials.filter(
                status__in=["available", "reserved"], deleted_at__isnull=True
            ).values_list("serial_no", flat=True)
        )

    def validate_sku(self, value):
        client_id = self.context.get("client_id")
        existing = Item.objects.filter(client_id=client_id, sku=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("An item with this SKU already exists.")
        return value

    def validate(self, attrs):
        """api.md §4.2 -- weight items need a theoretical weight to be received
        against a weighbridge at all."""
        is_weight_item = attrs.get(
            "is_weight_item",
            self.instance.is_weight_item if self.instance else False,
        )
        theoretical = attrs.get(
            "theoretical_weight",
            self.instance.theoretical_weight if self.instance else None,
        )
        if is_weight_item and not theoretical:
            raise serializers.ValidationError(
                {"theoreticalWeight": ["Required for a weight-tracked item."]}
            )
        return attrs


class ItemStockSerializer(BaseSerializer):
    onHand = QuantityField()
    reserved = QuantityField()
    damaged = QuantityField()
    available = QuantityField()
    status = serializers.CharField()
    byLocation = serializers.ListField(child=serializers.DictField(), required=False)


class ItemSerialSerializer(BaseModelSerializer):
    class Meta:
        model = ItemSerial
        fields = [
            "id", "serial_no", "status", "batch_number", "location",
            "warranty_card", "created_at",
        ]


class ItemPartSerializer(BaseModelSerializer):
    """Machine BOM line."""

    partItemId = TenantPrimaryKeyRelatedField(
        source="part_item", queryset=Item.objects.all()
    )
    sku = serializers.CharField(source="part_item.sku", read_only=True)
    name = serializers.CharField(source="part_item.name", read_only=True)
    uom = serializers.CharField(source="part_item.uom", read_only=True)

    class Meta:
        model = ItemPart
        fields = ["id", "partItemId", "sku", "name", "uom", "required_qty"]


class CategoryPartSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = CategoryPart
        fields = ["id", "itemId", "sku", "name", "default_qty"]


class ImportRowsSerializer(BaseSerializer):
    """``POST /{module}/{entity}/import/`` (api.md §12.2).

    ``ImportModal`` parses the CSV in the browser and hands the page an array of
    row objects, so import is a JSON endpoint, not a multipart upload.
    """

    rows = serializers.ListField(child=serializers.DictField(), allow_empty=False)
    dryRun = serializers.BooleanField(required=False, default=False)
