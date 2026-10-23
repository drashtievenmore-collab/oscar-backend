"""Serializers for parties, items, categories, units, locations and BOM (api.md §4)."""
import re

from django.db.models import Q
from rest_framework import serializers

from apps.core.files import public_url
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    Vehicle, Driver,
    Agency,
    Broker,
    CategoryCustomField,
    CategoryPart,
    DefectMaster,
    Design,
    Fabric,
    FabricQuality,
    FabricType,
    FabricVariant,
    Item,
    ItemCategory,
    ItemPart,
    ItemSerial,
    JobWorkRateMaster,
    RateMaster,
    PriceList,
    Location,
    LossReasonMaster,
    Party,
    PartyContact,
    ProcessMaster,
    ReturnReasonMaster,
    Shade,
    Transporter,
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
    creditDays = serializers.IntegerField(source="credit_days", required=False, default=30)
    defaultDiscountPct = serializers.DecimalField(
        source="default_discount_pct", max_digits=7, decimal_places=4, required=False, default=0
    )
    defaultBrokerId = TenantPrimaryKeyRelatedField(
        source="default_broker", model="masters.Broker", required=False, allow_null=True
    )
    defaultSalespersonId = TenantPrimaryKeyRelatedField(
        source="default_salesperson", model="hrms.Employee", required=False, allow_null=True
    )
    defaultBrokerName = serializers.CharField(source="default_broker.name", read_only=True)
    defaultSalespersonName = serializers.CharField(source="default_salesperson.name", read_only=True)
    #: How many CRM leads were converted into this party. Lets the admin
    #: customer list show which accounts came from a lead conversion and which
    #: were entered by hand, without a second round trip. Annotated by
    #: ``PartyViewSet.customers``; absent (0) everywhere else.
    leadCount = serializers.SerializerMethodField()

    class Meta:
        model = Party
        fields = [
            "id", "code", "type", "vendor_type", "is_processor",
            "vehicle_number", "vehicle_type", "vehicle_capacity", "vehicle_capacity_unit",
            "name", "phone", "email",
            "gst_treatment", "gstin", "gst_notes", "place_of_supply",
            "tds_applicable", "tds_section", "tds_rate",
            "tcs_applicable", "tcs_rate",
            "ledgerAccount", "ledgerAccountId", "credit_limit", "credit_days", "creditDays",
            "default_discount_pct", "defaultDiscountPct",
            "default_broker", "defaultBrokerId", "defaultBrokerName",
            "default_salesperson", "defaultSalespersonId", "defaultSalespersonName",
            "payment_terms",
            "bank_account_number", "ifsc_code", "bank_name", "account_holder_name",
            "opening_balance", "balance", "leadCount",
            "billing_address", "shipping_address", "contacts", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["balance", "created_at", "updated_at"]
        # api.md 1.7: the server allocates the CUST-/VEND- code in
        # ``PartyViewSet.perform_create``; the client must never invent one, so
        # it is optional on the wire rather than required.
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def get_leadCount(self, party):
        return getattr(party, "lead_count", 0) or 0

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
    parent = TenantPrimaryKeyRelatedField(
        queryset=ItemCategory.objects.all(), required=False, allow_null=True
    )
    parentId = TenantPrimaryKeyRelatedField(
        source="parent", queryset=ItemCategory.objects.all(), required=False, allow_null=True
    )
    parentName = serializers.CharField(source="parent.name", read_only=True, default=None)
    parentCategory = serializers.CharField(source="parent.name", read_only=True, default=None)
    parentCode = serializers.CharField(source="parent.code", read_only=True, default=None)
    isActive = serializers.BooleanField(source="is_active", required=False, default=True)
    status = serializers.SerializerMethodField()
    imageUrl = serializers.CharField(source="image_url", required=False, allow_blank=True, allow_null=True)
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    createdBy = serializers.SerializerMethodField()
    updatedBy = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    usage = serializers.SerializerMethodField()

    class Meta:
        model = ItemCategory
        fields = [
            "id", "name", "code", "kind", "description", "has_sub_parts",
            "lead_time_days", "default_hsn_code", "customFields", "itemCount",
            "parent", "parentId", "parentName", "parentCategory", "parentCode",
            "isActive", "status", "imageUrl", "imageFileId",
            "createdBy", "updatedBy", "createdAt", "updatedAt", "usage",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def get_status(self, obj):
        return "Active" if obj.is_active else "Inactive"

    def get_createdBy(self, obj):
        if obj.created_by:
            return obj.created_by.name or obj.created_by.email or "Admin"
        return "Admin"

    def get_updatedBy(self, obj):
        if obj.updated_by:
            return obj.updated_by.name or obj.updated_by.email or "Admin"
        return "Admin"

    def get_usage(self, obj):
        client_id = obj.client_id
        items_count = obj.items.filter(deleted_at__isnull=True).count()
        return {
            "fabricItems": items_count,
            "purchaseOrders": 0,
            "grn": 0,
            "stockQty": 0,
            "salesOrders": 0,
            "samples": 0,
        }

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if not ret.get("imageUrl") and instance.image_file:
            ret["imageUrl"] = public_url(instance.image_file)
        return ret

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
    """UOM master. ``label`` is the UOM name; ``name`` mirrors it for the UOM page."""

    name = serializers.CharField(source="label", read_only=True)
    status = serializers.SerializerMethodField()
    conversionFactor = serializers.DecimalField(
        source="conversion_factor", max_digits=18, decimal_places=6,
        coerce_to_string=False, required=False,
    )
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    imageUrl = serializers.SerializerMethodField()
    baseUnitCode = serializers.SerializerMethodField()
    usageCount = serializers.SerializerMethodField()

    class Meta:
        model = Unit
        fields = [
            "id", "code", "label", "name", "uom_type", "is_base_unit", "decimal_places",
            "conversionFactor", "description", "is_active", "status",
            "imageFileId", "imageUrl", "image_url", "baseUnitCode", "usageCount",
            "created_at", "updated_at",
        ]
        extra_kwargs = {
            "image_url": {"write_only": True, "required": False, "allow_null": True, "allow_blank": True},
        }

    def _live_peers(self):
        qs = Unit.objects.filter(client_id=self.context.get("client_id"), deleted_at__isnull=True)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        return qs

    def get_status(self, obj):
        return "Active" if obj.is_active else "Inactive"

    def get_imageUrl(self, obj):
        if getattr(obj, "image_file", None):
            return public_url(obj.image_file)
        return obj.image_url or None

    def get_baseUnitCode(self, obj):
        if obj.is_base_unit:
            return obj.code
        base = Unit.objects.filter(
            client_id=obj.client_id, uom_type=obj.uom_type, is_base_unit=True,
            deleted_at__isnull=True,
        ).values_list("code", flat=True).first()
        return base

    def get_usageCount(self, obj):
        from .uom import item_unit_usage

        usage = item_unit_usage(obj.client_id, obj)
        return (
            obj.fabric_qualities.filter(deleted_at__isnull=True).count()
            + obj.fabric_variants.filter(deleted_at__isnull=True).count()
            + usage["items"]
            + usage["documentLines"]
        )

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        # The UOM page sends ``name``; older callers send ``label``.
        if data.get("name") and not data.get("label"):
            data["label"] = data["name"]
        if "imageUrl" in data and not data.get("imageFileId"):
            data["image_url"] = data.get("imageUrl")
        return super().to_internal_value(data)

    def validate_code(self, value):
        # Not upper-cased: items, purchase and CRM lines store the unit code as
        # text, so an existing "Meter" must stay "Meter".
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("UOM Code is required.")
        if self.instance is not None and cleaned != self.instance.code:
            raise serializers.ValidationError(
                "UOM Code can't be changed once created -- items store it by code."
            )
        if self._live_peers().filter(code__iexact=cleaned).exists():
            raise serializers.ValidationError(f"UOM Code {cleaned} is already used.")
        return cleaned

    def validate_label(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("UOM Name is required.")
        if self._live_peers().filter(label__iexact=cleaned).exists():
            raise serializers.ValidationError("This UOM name already exists.")
        return cleaned

    def validate_decimal_places(self, value):
        if value is None or value > 6:
            raise serializers.ValidationError("Decimal places must be between 0 and 6.")
        return value

    def validate_conversionFactor(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Conversion factor must be greater than zero.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        current = self.instance
        is_base = attrs.get("is_base_unit", current.is_base_unit if current else False)
        uom_type = attrs.get("uom_type", current.uom_type if current else "Length")
        if current is not None and uom_type != current.uom_type and self.get_usageCount(current):
            # Existing lines were converted under the old type; switching it
            # would make those conversions inconsistent with new ones.
            raise serializers.ValidationError({
                "uom_type": f"{current.code} is already in use, so its type can't change.",
            })
        if is_base:
            # A base unit is worth exactly one of itself.
            attrs["conversion_factor"] = 1
            clash = self._live_peers().filter(uom_type=uom_type, is_base_unit=True).first()
            if clash:
                raise serializers.ValidationError({
                    "is_base_unit": f"{clash.code} is already the base unit for {uom_type}. "
                    f"Set it to No first.",
                })
        return attrs


def get_location_descendant_ids(location):
    if not location or not location.id:
        return set()
    descendant_ids = set()
    queue = [location.id]
    while queue:
        curr_id = queue.pop(0)
        children = list(Location.objects.filter(
            client_id=location.client_id, parent_id=curr_id, deleted_at__isnull=True
        ).values_list("id", flat=True))
        for cid in children:
            if cid not in descendant_ids:
                descendant_ids.add(cid)
                queue.append(cid)
    return descendant_ids


def get_location_ancestor_info(location):
    warehouse = None
    zone = None
    curr = location.parent
    visited = set()
    while curr and curr.id not in visited:
        visited.add(curr.id)
        if curr.type == "Zone" and not zone:
            zone = curr
        elif curr.type == "Warehouse" and not warehouse:
            warehouse = curr
        curr = curr.parent

    wh_code = warehouse.code if warehouse else (location.code if location.type == "Warehouse" else "-")
    wh_name = warehouse.name if warehouse else (location.name if location.type == "Warehouse" else "-")
    zn_code = zone.code if zone else (location.code if location.type == "Zone" else "-")
    zn_name = zone.name if zone else (location.name if location.type == "Zone" else "-")
    return wh_code, wh_name, zn_code, zn_name


class LocationSerializer(BaseModelSerializer):
    parent_code = serializers.SerializerMethodField()
    parent_name = serializers.SerializerMethodField()
    warehouse_code = serializers.SerializerMethodField()
    warehouse_name = serializers.SerializerMethodField()
    zone_code = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()
    used_quantity = serializers.SerializerMethodField()
    available_capacity = serializers.SerializerMethodField()
    utilization_pct = serializers.SerializerMethodField()
    stock_item_count = serializers.SerializerMethodField()
    lot_taka_count = serializers.SerializerMethodField()

    class Meta:
        model = Location
        fields = [
            "id", "code", "name", "type", "parent", "parent_code", "parent_name",
            "warehouse_code", "warehouse_name", "zone_code", "zone_name",
            "address", "description", "capacity", "capacity_uom", "manager",
            "is_active", "used_quantity", "available_capacity", "utilization_pct",
            "stock_item_count", "lot_taka_count", "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def get_parent_code(self, obj):
        return obj.parent.code if obj.parent else "-"

    def get_parent_name(self, obj):
        return obj.parent.name if obj.parent else "-"

    def get_warehouse_code(self, obj):
        wh_code, _, _, _ = get_location_ancestor_info(obj)
        return wh_code

    def get_warehouse_name(self, obj):
        _, wh_name, _, _ = get_location_ancestor_info(obj)
        return wh_name

    def get_zone_code(self, obj):
        _, _, zn_code, _ = get_location_ancestor_info(obj)
        return zn_code

    def get_zone_name(self, obj):
        _, _, _, zn_name = get_location_ancestor_info(obj)
        return zn_name

    def get_used_quantity(self, obj):
        from apps.inventory.models import StockBalance
        from django.db.models import Sum
        all_ids = [obj.id] + list(get_location_descendant_ids(obj))
        val = StockBalance.objects.filter(
            client_id=obj.client_id, location_id__in=all_ids, on_hand__gt=0
        ).aggregate(total=Sum("on_hand"))["total"] or 0
        return float(val)

    def get_available_capacity(self, obj):
        used = self.get_used_quantity(obj)
        cap = float(obj.capacity or 0)
        return max(0.0, round(cap - used, 2)) if cap > 0 else 0.0

    def get_utilization_pct(self, obj):
        cap = float(obj.capacity or 0)
        if cap <= 0:
            return 0.0
        used = self.get_used_quantity(obj)
        return round((used / cap) * 100, 1)

    def get_stock_item_count(self, obj):
        from apps.inventory.models import StockBalance
        all_ids = [obj.id] + list(get_location_descendant_ids(obj))
        return StockBalance.objects.filter(
            client_id=obj.client_id, location_id__in=all_ids, on_hand__gt=0
        ).values("item_id").distinct().count()

    def get_lot_taka_count(self, obj):
        from apps.inventory.models import LotTaka
        all_ids = [obj.id] + list(get_location_descendant_ids(obj))
        return LotTaka.objects.filter(
            client_id=obj.client_id, location_id__in=all_ids, deleted_at__isnull=True, status="Active"
        ).count()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        name = attrs.get("name")
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(name, "LOC")

        parent = attrs.get("parent")
        loc_type = attrs.get("type") or (self.instance.type if self.instance else "Warehouse")

        if self.instance and parent:
            if parent.id == self.instance.id:
                raise serializers.ValidationError({"parent": "A location cannot be its own parent."})
            curr = parent
            visited = {self.instance.id}
            while curr:
                if curr.id in visited:
                    raise serializers.ValidationError({"parent": "Circular reference detected in location hierarchy."})
                visited.add(curr.id)
                curr = curr.parent

        if parent and not parent.is_active:
            raise serializers.ValidationError({"parent": "Cannot assign an inactive location as parent."})

        # Validate hierarchical relationship if parent specified
        if loc_type == "Zone" and parent and parent.type not in ("Warehouse", "Zone"):
            raise serializers.ValidationError({"parent": f"A Zone can only be placed under a Warehouse or another Zone, not a {parent.type}."})
        elif loc_type == "Godown" and parent and parent.type not in ("Warehouse", "Zone", "Godown"):
            raise serializers.ValidationError({"parent": f"A Godown can only be placed under a Warehouse, Zone, or Godown, not a {parent.type}."})
        elif loc_type == "Rack" and parent and parent.type not in ("Warehouse", "Zone", "Godown", "Rack"):
            raise serializers.ValidationError({"parent": f"A Rack can only be placed under a Godown, Zone, or Warehouse, not a {parent.type}."})
        elif loc_type == "Bin" and parent and parent.type not in ("Warehouse", "Zone", "Godown", "Rack", "Bin"):
            raise serializers.ValidationError({"parent": f"A Bin can only be placed under a Rack, Godown, Zone, or Warehouse, not a {parent.type}."})

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
# Textile masters -- quality, design, shade, fabric type
# ---------------------------------------------------------------------------
class TextileMasterSerializer(BaseModelSerializer):
    """Shared rules: name required, code derived from the name when omitted,
    both unique per tenant among live rows."""

    code_fallback = "CODE"
    entity_label = "record"

    class Meta:
        fields = [
            "id", "code", "name", "description", "is_active",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def _live_peers(self):
        qs = self.Meta.model.objects.filter(
            client_id=self.context.get("client_id"), deleted_at__isnull=True
        )
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        return qs

    def validate_name(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("Name is required.")
        if self._live_peers().filter(name__iexact=cleaned).exists():
            raise serializers.ValidationError(f"This {self.entity_label} already exists.")
        return cleaned

    def validate_code(self, value):
        cleaned = (value or "").strip().upper()
        if cleaned and self._live_peers().filter(code__iexact=cleaned).exists():
            raise serializers.ValidationError(f"Code {cleaned} is already used.")
        return cleaned

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("code") and self.instance is not None:
            attrs.pop("code", None)  # a blank code on edit keeps the current one
        elif not attrs.get("code"):
            code = slug_code(attrs.get("name"), self.code_fallback)
            if self._live_peers().filter(code__iexact=code).exists():
                raise serializers.ValidationError(
                    {"code": f"Code {code} is already used. Enter a different code."}
                )
            attrs["code"] = code
        return attrs


class FabricTypeSerializer(TextileMasterSerializer):
    code_fallback = "TYPE"
    entity_label = "fabric type"

    status = serializers.SerializerMethodField()
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    imageUrl = serializers.SerializerMethodField()
    qualityCount = serializers.SerializerMethodField()

    class Meta(TextileMasterSerializer.Meta):
        model = FabricType
        fields = TextileMasterSerializer.Meta.fields + [
            "process_stage", "status", "imageFileId", "imageUrl", "qualityCount", "image_url",
        ]
        extra_kwargs = {
            **TextileMasterSerializer.Meta.extra_kwargs,
            "image_url": {"write_only": True, "required": False, "allow_null": True, "allow_blank": True},
        }

    def get_status(self, obj):
        return "Active" if obj.is_active else "Inactive"

    def get_imageUrl(self, obj):
        if getattr(obj, "image_file", None):
            return public_url(obj.image_file)
        return obj.image_url or None

    def get_qualityCount(self, obj):
        return obj.qualities.filter(deleted_at__isnull=True).count()

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "imageUrl" in data and not data.get("imageFileId"):
            data["image_url"] = data.get("imageUrl")
        return super().to_internal_value(data)


class FabricQualitySerializer(TextileMasterSerializer):
    code_fallback = "QUALITY"
    entity_label = "quality"

    fabricId = TenantPrimaryKeyRelatedField(
        source="fabric", queryset=Fabric.objects.all(), required=False, allow_null=True
    )
    fabricName = serializers.CharField(source="fabric.name", read_only=True, default=None)
    fabricTypeId = TenantPrimaryKeyRelatedField(
        source="fabric_type", queryset=FabricType.objects.all(), required=False, allow_null=True
    )
    fabricTypeName = serializers.CharField(
        source="fabric_type.name", read_only=True, default=None
    )
    uomId = TenantPrimaryKeyRelatedField(
        source="uom", queryset=Unit.objects.all(), required=False, allow_null=True
    )

    def validate_uomId(self, unit):
        """New choices must be active; a record keeps an already-linked unit
        even after that unit is inactivated."""
        current = getattr(self.instance, "uom", None)
        if unit is not None and not unit.is_active and unit != current:
            raise serializers.ValidationError(f"Unit {unit.code} is inactive.")
        return unit
    uom = serializers.SerializerMethodField()
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    imageUrl = serializers.SerializerMethodField()

    def get_uom(self, obj):
        if obj.uom:
            return obj.uom.label or obj.uom.code
        return None

    def get_imageUrl(self, obj):
        if getattr(obj, "image_file", None):
            return public_url(obj.image_file)
        return obj.image_url or None

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "uom" in data and not data.get("uomId") and not data.get("uom_id"):
            val = str(data.get("uom") or "").strip()
            if val:
                from .uom import UnitBook

                unit = UnitBook(self.context.get("client_id")).resolve(val)
                if unit is None:
                    raise serializers.ValidationError(
                        {"uom": [f"Unit {val} is not in the UOM master."]}
                    )
                data["uomId"] = str(unit.id)
        if "imageUrl" in data and not data.get("imageFileId"):
            data["image_url"] = data.get("imageUrl")
        return super().to_internal_value(data)

    class Meta(TextileMasterSerializer.Meta):
        model = FabricQuality
        fields = TextileMasterSerializer.Meta.fields + [
            "fabricId", "fabricName", "fabricTypeId", "fabricTypeName",
            "composition", "uomId", "uom", "imageFileId", "imageUrl",
            "width", "gsm", "reed", "pick", "warp_count", "weft_count",
        ]


class DesignSerializer(TextileMasterSerializer):
    code_fallback = "DESIGN"
    entity_label = "design"

    patternType = serializers.ChoiceField(
        source="pattern_type", choices=Design.PATTERN_TYPES, required=True
    )
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    imageUrl = serializers.SerializerMethodField()

    class Meta(TextileMasterSerializer.Meta):
        model = Design
        fields = TextileMasterSerializer.Meta.fields + [
            "patternType", "season", "reference", "imageFileId", "imageUrl",
        ]

    def get_imageUrl(self, design):
        # Relative, so it resolves through whatever host serves the app.
        return public_url(design.image_file)


class ShadeSerializer(TextileMasterSerializer):
    code_fallback = "SHADE"
    entity_label = "shade"

    colourType = serializers.CharField(source="colour_type", required=False, allow_blank=True, default="Solid")
    pantoneReference = serializers.CharField(
        source="pantone_reference", required=False, allow_blank=True, allow_null=True
    )
    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    imageUrl = serializers.SerializerMethodField()

    def get_imageUrl(self, obj):
        if getattr(obj, "image_file", None):
            return public_url(obj.image_file)
        return obj.image_url or None

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "imageUrl" in data and not data.get("imageFileId"):
            data["image_url"] = data.get("imageUrl")
        return super().to_internal_value(data)

    class Meta(TextileMasterSerializer.Meta):
        model = Shade
        fields = TextileMasterSerializer.Meta.fields + [
            "colourType", "pantoneReference", "imageFileId", "imageUrl", "hex_color",
        ]

    def validate_hex_color(self, value):
        if value and not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise serializers.ValidationError("Use a #RRGGBB colour.")
        return value or None


class FabricVariantSerializer(TextileMasterSerializer):
    """Fabric item / variant: quality + design + shade. Name and code are
    derived from the parts when left blank; each combination exists once."""

    code_fallback = "ITEM"
    entity_label = "fabric item"

    name = serializers.CharField(required=False, allow_blank=True)
    qualityId = TenantPrimaryKeyRelatedField(source="quality", queryset=FabricQuality.objects.all())
    designId = TenantPrimaryKeyRelatedField(
        source="design", queryset=Design.objects.all(), required=False, allow_null=True
    )
    shadeId = TenantPrimaryKeyRelatedField(
        source="shade", queryset=Shade.objects.all(), required=False, allow_null=True
    )
    uomId = TenantPrimaryKeyRelatedField(
        source="uom", queryset=Unit.objects.all(), required=False, allow_null=True
    )
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", queryset=ItemCategory.objects.all(), required=False, allow_null=True
    )
    fabricTypeId = TenantPrimaryKeyRelatedField(
        source="fabric_type_ref", queryset=FabricType.objects.all(), required=False, allow_null=True
    )
    itemId = TenantPrimaryKeyRelatedField(
        source="item", queryset=Item.objects.all(), required=False, allow_null=True
    )

    def validate_uomId(self, unit):
        """New choices must be active; a record keeps an already-linked unit
        even after that unit is inactivated."""
        current = getattr(self.instance, "uom", None)
        if unit is not None and not unit.is_active and unit != current:
            raise serializers.ValidationError(f"Unit {unit.code} is inactive.")
        return unit

    def validate_categoryId(self, cat):
        current = getattr(self.instance, "category", None)
        if cat is not None and not cat.is_active and cat != current:
            raise serializers.ValidationError(f"Category '{cat.name}' is inactive.")
        return cat

    def validate_fabricTypeId(self, ft):
        current = getattr(self.instance, "fabric_type_ref", None)
        if ft is not None and not ft.is_active and ft != current:
            raise serializers.ValidationError(f"Fabric Type '{ft.name}' is inactive.")
        return ft

    imageFileId = TenantPrimaryKeyRelatedField(
        source="image_file", model="core.File", required=False, allow_null=True
    )
    hsnCode = serializers.CharField(source="hsn_code", required=False, allow_blank=True, allow_null=True)
    fabricType = serializers.CharField(source="fabric_type", required=False, allow_blank=True, allow_null=True)
    construction = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    imageUrl = serializers.CharField(source="image_url", required=False, allow_blank=True, allow_null=True)
    rate = MoneyField(required=False, allow_null=True)

    qualityName = serializers.CharField(source="quality.name", read_only=True)
    qualityCode = serializers.CharField(source="quality.code", read_only=True)
    designName = serializers.CharField(source="design.name", read_only=True, default=None)
    designCode = serializers.CharField(source="design.code", read_only=True, default=None)
    shadeName = serializers.CharField(source="shade.name", read_only=True, default=None)
    shadeCode = serializers.CharField(source="shade.code", read_only=True, default=None)
    shadeHex = serializers.CharField(source="shade.hex_color", read_only=True, default=None)
    categoryName = serializers.CharField(source="category.name", read_only=True, default=None)
    categoryCode = serializers.CharField(source="category.code", read_only=True, default=None)
    fabricTypeName = serializers.CharField(source="fabric_type_ref.name", read_only=True, default=None)
    fabricTypeCode = serializers.CharField(source="fabric_type_ref.code", read_only=True, default=None)
    itemSku = serializers.CharField(source="item.sku", read_only=True, default=None)
    uom = serializers.SerializerMethodField()
    width = serializers.DecimalField(source="quality.width", max_digits=7, decimal_places=2, read_only=True)
    gsm = serializers.DecimalField(source="quality.gsm", max_digits=7, decimal_places=2, read_only=True)

    class Meta(TextileMasterSerializer.Meta):
        model = FabricVariant
        fields = TextileMasterSerializer.Meta.fields + [
            "qualityId", "qualityName", "qualityCode",
            "designId", "designName", "designCode",
            "shadeId", "shadeName", "shadeCode", "shadeHex",
            "categoryId", "categoryName", "categoryCode",
            "fabricTypeId", "fabricTypeName", "fabricTypeCode",
            "itemId", "itemSku",
            "uomId", "uom", "width", "gsm", "hsnCode", "rate",
            "fabricType", "construction",
            "imageFileId", "imageUrl",
        ]

    def get_uom(self, variant):
        unit = variant.uom or (variant.quality.uom if variant.quality else None)
        return unit.code if unit else None

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if not ret.get("imageUrl") and instance.image_file:
            ret["imageUrl"] = public_url(instance.image_file)
        if not ret.get("fabricTypeName") and instance.fabric_type:
            ret["fabricTypeName"] = instance.fabric_type
        return ret

    def validate_name(self, value):
        # Blank is allowed: ``validate`` builds the name from the parts.
        return super().validate_name(value) if (value or "").strip() else ""

    def validate_rate(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Rate cannot be negative.")
        return value

    def validate(self, attrs):
        quality = attrs.get("quality", getattr(self.instance, "quality", None))
        design = attrs.get("design", getattr(self.instance, "design", None))
        shade = attrs.get("shade", getattr(self.instance, "shade", None))
        parts = [p for p in (quality, design, shade) if p is not None]

        # -- Block inactive Quality / Design / Shade on *new* variants ------
        is_create = self.instance is None
        if is_create:
            if quality and not quality.is_active:
                raise serializers.ValidationError(
                    {"qualityId": f"Quality '{quality.name}' is inactive."}
                )
            if design and not design.is_active:
                raise serializers.ValidationError(
                    {"designId": f"Design '{design.name}' is inactive."}
                )
            if shade and not shade.is_active:
                raise serializers.ValidationError(
                    {"shadeId": f"Shade '{shade.name}' is inactive."}
                )

        uom_val = self.initial_data.get("uom")
        if uom_val and not attrs.get("uom"):
            from .uom import UnitBook

            unit = UnitBook(self.context.get("client_id")).resolve(uom_val)
            if unit is None:
                raise serializers.ValidationError(
                    {"uom": [f"Unit {uom_val} is not in the UOM master."]}
                )
            attrs["uom"] = self.validate_uomId(unit)

        # -- Auto-populate UOM from quality when not explicitly set ----------
        if not attrs.get("uom") and is_create and quality and getattr(quality, "uom", None):
            attrs["uom"] = quality.uom

        # -- Auto-populate fabricType and fabric_type_ref from quality ----------
        if not attrs.get("fabric_type_ref") and quality and getattr(quality, "fabric_type", None):
            attrs["fabric_type_ref"] = quality.fabric_type
        if not attrs.get("fabric_type"):
            ft_ref = attrs.get("fabric_type_ref") or getattr(quality, "fabric_type", None)
            if ft_ref:
                attrs["fabric_type"] = ft_ref.name if hasattr(ft_ref, "name") else str(ft_ref)

        # -- Auto-populate category if omitted ----------
        if not attrs.get("category") and is_create:
            client_id = self.context.get("client_id")
            default_cat = ItemCategory.objects.filter(
                client_id=client_id, code="CAT-FABRIC", deleted_at__isnull=True
            ).first() or ItemCategory.objects.filter(
                client_id=client_id, is_active=True, deleted_at__isnull=True
            ).first()
            if default_cat:
                attrs["category"] = default_cat

        if self._live_peers().filter(quality=quality, design=design, shade=shade).exists():
            raise serializers.ValidationError(
                {"qualityId": "A fabric item for this quality, design and shade already exists."}
            )
        if not attrs.get("name") and self.instance is None:
            name = " — ".join(p.name for p in parts)
            if self._live_peers().filter(name__iexact=name).exists():
                raise serializers.ValidationError({"name": f"This {self.entity_label} already exists."})
            attrs["name"] = name
        elif "name" in attrs and not attrs["name"]:
            attrs.pop("name")  # a blank name on edit keeps the current one
        if not attrs.get("code") and self.instance is None:
            code = slug_code("-".join(p.code for p in parts), self.code_fallback)
            if self._live_peers().filter(code__iexact=code).exists():
                raise serializers.ValidationError(
                    {"code": f"Code {code} is already used. Enter a different code."}
                )
            attrs["code"] = code
        return super().validate(attrs)

    def _sync_to_item(self, variant):
        from decimal import Decimal
        client = variant.client
        item = variant.item
        if not item:
            item = Item.objects.filter(
                client=client, sku=variant.code, deleted_at__isnull=True
            ).first()

        cat = variant.category
        if not cat:
            cat = ItemCategory.objects.filter(
                client=client, code="CAT-FABRIC", deleted_at__isnull=True
            ).first() or ItemCategory.objects.filter(
                client=client, is_active=True, deleted_at__isnull=True
            ).first()

        unit_str = variant.uom.code if variant.uom else (variant.quality.uom.code if variant.quality and variant.quality.uom else "Mtr")
        quality_name = variant.quality.name if variant.quality else ""
        design_name = variant.design.name if variant.design else ""
        shade_name = variant.shade.name if variant.shade else ""
        width_val = variant.quality.width if variant.quality else None
        gsm_val = variant.quality.gsm if variant.quality else None
        rate_val = variant.rate or Decimal("0")

        if not item:
            item = Item.objects.create(
                client=client,
                sku=variant.code,
                name=variant.name,
                category=cat,
                item_kind="Fabric",
                uom=unit_str,
                fabric_quality=quality_name,
                fabric_design=design_name,
                fabric_color=shade_name,
                fabric_width=width_val,
                fabric_gsm=gsm_val,
                cost_price=rate_val,
                selling_price=rate_val,
                lifecycle_status="Active" if variant.is_active else "Inactive",
            )
        else:
            item.name = variant.name
            item.sku = variant.code
            item.item_kind = "Fabric"
            if cat:
                item.category = cat
            item.uom = unit_str
            item.fabric_quality = quality_name
            item.fabric_design = design_name
            item.fabric_color = shade_name
            item.fabric_width = width_val
            item.fabric_gsm = gsm_val
            if variant.rate is not None:
                item.cost_price = rate_val
                item.selling_price = rate_val
            item.lifecycle_status = "Active" if variant.is_active else "Inactive"
            item.save()

        if variant.item_id != item.id:
            FabricVariant.objects.filter(pk=variant.pk).update(item=item)
            variant.item = item
        return item

    def create(self, validated_data):
        if "client_id" not in validated_data and "client" not in validated_data:
            client = self.context.get("client")
            client_id = getattr(client, "id", None) or self.context.get("client_id")
            if client_id:
                validated_data["client_id"] = client_id
        variant = super().create(validated_data)
        self._sync_to_item(variant)
        return variant

    def update(self, instance, validated_data):
        variant = super().update(instance, validated_data)
        self._sync_to_item(variant)
        return variant




# ---------------------------------------------------------------------------
# Items (api.md §4.2)
# ---------------------------------------------------------------------------
class ItemSerializer(BaseModelSerializer):
    """The ``InventoryItem`` typedef plus the fields ``addInventoryItem`` adds.

    ``availableQty``, ``reservedQty`` and ``status`` are **not columns** -- they
    are joined on from the movement ledger at read time (db.md §4.2), so the
    JSON contract is unchanged while the source of truth moved.
    """

    sku = serializers.CharField(required=False, allow_blank=True)
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
            "tracking_mode", "tracking_type", "serialNumbers",
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

    def create(self, validated_data):
        # The server owns document numbers: a blank SKU gets the next ITM number.
        if not validated_data.get("sku"):
            from apps.core.numbering import allocate_number

            validated_data["sku"] = allocate_number(self.context.get("client_id"), "ITM")
        return super().create(validated_data)

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
        value = (value or "").strip()
        if not value:
            if self.instance is not None:
                raise serializers.ValidationError("SKU cannot be blank.")
            return value  # allocated by the server in create()
        client_id = self.context.get("client_id")
        existing = Item.objects.filter(client_id=client_id, sku=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("An item with this SKU already exists.")
        return value

    def validate_uom(self, value):
        """The stock unit must be an active UOM-master unit, and it is fixed once
        stock has moved -- balances are counted in it."""
        from apps.inventory.models import StockMovement

        from .uom import UnitBook, same_unit_text

        value = (value or "").strip()
        current = self.instance.uom if self.instance is not None else None
        if current is not None and same_unit_text(value, current):
            return value  # unchanged (or a different spelling of it)
        book = UnitBook(self.context.get("client_id"))
        if not book.empty:
            unit = book.resolve(value)
            if unit is None:
                raise serializers.ValidationError(
                    f"Unit {value} is not in the UOM master. Add it under Units of Measurement first."
                )
            if not unit.is_active:
                raise serializers.ValidationError(f"Unit {unit.code} is inactive.")
            if current is not None and book.resolve(current) == unit:
                return value
        if self.instance is not None and StockMovement.objects.filter(
            item=self.instance, deleted_at__isnull=True
        ).exists():
            raise serializers.ValidationError(
                f"{self.instance.name} already has stock movements in {current}; "
                "its stock unit can't change."
            )
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


# ---------------------------------------------------------------------------
# Textile & Job Work Masters (Phase 2.1)
# ---------------------------------------------------------------------------
class AgencySerializer(BaseModelSerializer):
    class Meta:
        model = Agency
        fields = [
            "id", "code", "name", "contact_person", "phone", "email",
            "gstin", "address", "default_rate", "rate_uom", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        if not value:
            return value
        existing = Agency.objects.filter(
            client_id=client_id, code__iexact=value, deleted_at__isnull=True
        )
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("An agency with this code already exists.")
        return value


class BrokerSerializer(BaseModelSerializer):
    class Meta:
        model = Broker
        fields = [
            "id", "code", "name", "contact_person", "phone", "email",
            "pan", "gstin", "commission_type", "default_value", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        if not value:
            return value
        existing = Broker.objects.filter(
            client_id=client_id, code__iexact=value, deleted_at__isnull=True
        )
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A broker with this code already exists.")
        return value


class ProcessMasterSerializer(BaseModelSerializer):
    class Meta:
        model = ProcessMaster
        fields = [
            "id", "code", "name", "category", "expected_loss_pct",
            "tolerance_pct", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}


class JobWorkRateMasterSerializer(BaseModelSerializer):
    processorName = serializers.CharField(source="processor.name", read_only=True)
    processName = serializers.CharField(source="process.name", read_only=True)
    fabricQualityName = serializers.CharField(source="fabric_quality.name", read_only=True, allow_null=True)

    processorId = TenantPrimaryKeyRelatedField(
        source="processor", queryset=Party.objects.all()
    )
    processId = TenantPrimaryKeyRelatedField(
        source="process", queryset=ProcessMaster.objects.all()
    )
    fabricQualityId = TenantPrimaryKeyRelatedField(
        source="fabric_quality", queryset=FabricQuality.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = JobWorkRateMaster
        fields = [
            "id", "processorId", "processorName", "processId", "processName",
            "fabricQualityId", "fabricQualityName", "rate", "rate_uom",
            "effective_from", "effective_to", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class RateMasterSerializer(BaseModelSerializer):
    rateType = serializers.CharField(source="rate_type", default="Sales")
    partyName = serializers.CharField(source="party.name", read_only=True, allow_null=True)
    partyCode = serializers.CharField(source="party.code", read_only=True, allow_null=True)
    itemName = serializers.CharField(source="item.name", read_only=True, allow_null=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True, allow_null=True)
    fabricQualityName = serializers.CharField(source="fabric_quality.name", read_only=True, allow_null=True)
    designName = serializers.CharField(source="design.name", read_only=True, allow_null=True)
    shadeName = serializers.CharField(source="shade.name", read_only=True, allow_null=True)

    partyId = TenantPrimaryKeyRelatedField(
        source="party", queryset=Party.objects.all(), required=False, allow_null=True
    )
    itemId = TenantPrimaryKeyRelatedField(
        source="item", queryset=Item.objects.all(), required=False, allow_null=True
    )
    fabricQualityId = TenantPrimaryKeyRelatedField(
        source="fabric_quality", queryset=FabricQuality.objects.all(), required=False, allow_null=True
    )
    designId = TenantPrimaryKeyRelatedField(
        source="design", queryset=Design.objects.all(), required=False, allow_null=True
    )
    shadeId = TenantPrimaryKeyRelatedField(
        source="shade", queryset=Shade.objects.all(), required=False, allow_null=True
    )

    minSellingRate = serializers.DecimalField(
        source="min_selling_rate", max_digits=18, decimal_places=4, required=False, allow_null=True
    )
    validFrom = serializers.DateField(source="valid_from", required=False)
    validTo = serializers.DateField(source="valid_to", required=False, allow_null=True)
    isActive = serializers.BooleanField(source="is_active", required=False, default=True)

    class Meta:
        model = RateMaster
        fields = [
            "id", "rateType", "rate_type",
            "partyId", "partyName", "partyCode",
            "itemId", "itemName", "itemSku",
            "fabricQualityId", "fabricQualityName",
            "designId", "designName",
            "shadeId", "shadeName",
            "rate", "minSellingRate", "min_selling_rate",
            "uom", "validFrom", "validTo", "valid_from", "valid_to",
            "isActive", "is_active", "notes",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class DefectMasterSerializer(BaseModelSerializer):
    class Meta:
        model = DefectMaster
        fields = [
            "id", "code", "name", "category", "severity", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}


class LossReasonMasterSerializer(BaseModelSerializer):
    class Meta:
        model = LossReasonMaster
        fields = [
            "id", "code", "name", "description", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}


class ReturnReasonMasterSerializer(BaseModelSerializer):
    class Meta:
        model = ReturnReasonMaster
        fields = [
            "id", "code", "name", "description", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}


class TransporterSerializer(BaseModelSerializer):
    class Meta:
        model = Transporter
        fields = [
            "id", "code", "name", "contact_person", "phone", "email",
            "gstin", "pan", "address", "vehicles", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}




class VehicleSerializer(BaseModelSerializer):
    vehicleNumber = serializers.CharField(source="vehicle_number", required=False)
    vehicleType = serializers.CharField(source="vehicle_type", required=False, default="Truck")
    transporterId = TenantPrimaryKeyRelatedField(
        source="transporter", model="masters.Transporter", required=False, allow_null=True
    )
    transporterName = serializers.CharField(source="transporter.name", read_only=True)
    isActive = serializers.BooleanField(source="is_active", required=False, default=True)

    class Meta:
        model = Vehicle
        fields = [
            "id", "vehicleNumber", "vehicle_number", "vehicleType", "vehicle_type",
            "capacity", "transporterId", "transporter", "transporterName",
            "isActive", "is_active", "remarks", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {
            "vehicle_number": {"required": False},
            "vehicle_type": {"required": False},
            "is_active": {"required": False},
        }

    def to_internal_value(self, data):
        data = dict(data)
        if "vehicleNo" in data and "vehicleNumber" not in data and "vehicle_number" not in data:
            data["vehicleNumber"] = data["vehicleNo"]
        if "vehicle_no" in data and "vehicleNumber" not in data and "vehicle_number" not in data:
            data["vehicleNumber"] = data["vehicle_no"]
        if "type" in data and "vehicleType" not in data and "vehicle_type" not in data:
            data["vehicleType"] = data["type"]
        if "vehicle_number" not in data and "vehicleNumber" in data:
            data["vehicle_number"] = data["vehicleNumber"]
        if "vehicle_type" not in data and "vehicleType" in data:
            data["vehicle_type"] = data["vehicleType"]
        return super().to_internal_value(data)


class DriverSerializer(BaseModelSerializer):
    transporterId = TenantPrimaryKeyRelatedField(
        source="transporter", model="masters.Transporter", required=False, allow_null=True
    )
    transporterName = serializers.CharField(source="transporter.name", read_only=True)
    isActive = serializers.BooleanField(source="is_active", required=False, default=True)

    class Meta:
        model = Driver
        fields = [
            "id", "name", "mobile", "licence", "transporterId", "transporter",
            "transporterName", "isActive", "is_active", "remarks",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {
            "is_active": {"required": False},
        }

    def to_internal_value(self, data):
        data = dict(data)
        if "phone" in data and "mobile" not in data:
            data["mobile"] = data["phone"]
        if "license" in data and "licence" not in data:
            data["licence"] = data["license"]
        return super().to_internal_value(data)
