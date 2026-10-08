"""Serializers for the inventory module (api.md §7)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.masters.models import Item, ItemPart, Location

from .models import (
    FaultyPart,
    QualityStandard,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    ZoneRequest,
    ZoneRequestLine,
)


class StockMovementSerializer(BaseModelSerializer):
    """The movement record of api.md §7.2, field for field."""

    itemId = serializers.CharField(source="item_id", read_only=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    locationId = serializers.CharField(source="location_id", read_only=True)
    locationName = serializers.CharField(source="location.name", read_only=True)
    referenceId = serializers.CharField(source="reference_id", read_only=True)
    sourceDocumentId = serializers.CharField(source="source_document_id", read_only=True)
    originalMovementId = serializers.CharField(source="original_movement_id", read_only=True)
    reversalMovementId = serializers.CharField(source="reversal_movement_id", read_only=True)
    date = serializers.DateField(source="movement_date", read_only=True)
    serials = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id", "itemId", "itemSku", "itemName", "locationId", "locationName",
            "type", "quantity", "weighed_qty", "unit_cost",
            "reference_type", "referenceId", "reference_number",
            "source_document_type", "sourceDocumentId",
            "originalMovementId", "reversalMovementId",
            "batch_number", "serials", "date", "notes", "created_at",
        ]

    def get_serials(self, movement):
        from apps.masters.models import ItemSerial

        cached = getattr(movement, "_serials", None)
        if cached is not None:
            return cached
        return list(
            ItemSerial.objects.filter(
                sold_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        ) or list(
            ItemSerial.objects.filter(
                received_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        )


class StockAdjustmentSerializer(BaseSerializer):
    """``POST /inventory/adjustments/`` -- replaces ``adjustItemStock``."""

    itemId = TenantPrimaryKeyRelatedField(queryset=Item.objects.all())
    locationId = TenantPrimaryKeyRelatedField(
        queryset=Location.objects.all(), required=False, allow_null=True
    )
    quantity = QuantityField()
    #: True means "set stock to this number"; False means "add this delta".
    isAbsolute = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False, required=False
    )


class StockPositionSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    uom = serializers.CharField()
    onHand = QuantityField()
    reserved = QuantityField()
    available = QuantityField()
    damaged = QuantityField()
    reorderLevel = QuantityField()
    status = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)


class StockTransferLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = StockTransferLine
        fields = ["id", "itemId", "sku", "name", "qty", "received_qty"]


class StockTransferSerializer(BaseModelSerializer):
    """db.md §7.4 standardises on ``items[]`` and keeps the denormalised names
    as read-only echoes, which is what the mock data's flat form used."""

    items = StockTransferLineSerializer(many=True, required=False)
    sourceLocationId = TenantPrimaryKeyRelatedField(
        source="from_location", queryset=Location.objects.all()
    )
    destLocationId = TenantPrimaryKeyRelatedField(
        source="to_location", queryset=Location.objects.all()
    )
    sourceLocation = serializers.CharField(source="from_location.name", read_only=True)
    destLocation = serializers.CharField(source="to_location.name", read_only=True)
    itemsCount = serializers.SerializerMethodField()
    date = serializers.DateField(source="transfer_date")

    class Meta:
        model = StockTransfer
        fields = [
            "id", "transfer_number", "sourceLocationId", "destLocationId",
            "sourceLocation", "destLocation", "date", "itemsCount", "status",
            "shipped_by", "notes", "items", "created_at", "updated_at",
        ]
        read_only_fields = ["transfer_number", "created_at", "updated_at"]

    def get_itemsCount(self, transfer):
        return transfer.items.filter(deleted_at__isnull=True).count()

    def validate(self, attrs):
        source = attrs.get("from_location") or getattr(self.instance, "from_location", None)
        destination = attrs.get("to_location") or getattr(self.instance, "to_location", None)
        if source and destination and source.id == destination.id:
            raise serializers.ValidationError(
                {"destLocationId": ["Must differ from the source location."]}
            )
        return attrs


class FaultyPartSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    vendor = serializers.CharField(source="vendor.name", read_only=True)
    date = serializers.DateField(source="reported_date")
    qty = QuantityField(source="quantity")
    timeline = serializers.SerializerMethodField()

    class Meta:
        model = FaultyPart
        fields = [
            "id", "rma_number", "date", "itemId", "sku", "itemName", "qty",
            "vendor", "status", "notes", "fault_description", "timeline",
            "created_at", "updated_at",
        ]
        read_only_fields = ["rma_number", "created_at", "updated_at"]

    def get_timeline(self, part):
        """api.md §7.3 -- an ordered progress tracker derived from the status.

        The frontend renders ``{ label, timestamp, status }`` directly, so the
        server produces it rather than storing a parallel list.
        """
        steps = [
            "Reported", "Pending Action", "Sent for Replacement",
            "Replaced", "Credited", "Closed",
        ]
        try:
            current = steps.index(part.status)
        except ValueError:
            current = 0
        return [
            {
                "label": label,
                "timestamp": part.created_at if index == 0 else None,
                "status": (
                    "completed" if index < current
                    else "current" if index == current
                    else "future"
                ),
            }
            for index, label in enumerate(steps)
        ]


class ServiceUsageSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    qty = QuantityField(source="quantity")
    date = serializers.DateField(source="used_on")

    class Meta:
        model = ServiceUsage
        fields = [
            "id", "ticket_number", "technician", "itemId", "sku", "qty", "date",
            "notes", "job_reference", "chargeable", "created_at",
        ]
        read_only_fields = ["ticket_number", "created_at"]


class ZoneRequestLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    product = serializers.CharField(source="item.name", read_only=True)
    qty = QuantityField(source="requested_qty")
    warehouseStock = serializers.SerializerMethodField()

    class Meta:
        model = ZoneRequestLine
        fields = ["id", "itemId", "sku", "product", "qty", "issued_qty", "warehouseStock"]

    def get_warehouseStock(self, line):
        """api.md §7.3 -- ``warehouseStock`` is the available quantity *at the
        time the request is read*, so it is computed on read."""
        from . import services as stock

        client_id = self.context.get("client_id") or line.client_id
        return stock.calculate_item_stock(client_id, line.item_id)["available"]


class ZoneRequestSerializer(BaseModelSerializer):
    lines = ZoneRequestLineSerializer(many=True, required=False)
    zone = serializers.CharField(source="zone_location.name", read_only=True)
    zoneLocationId = TenantPrimaryKeyRelatedField(
        source="zone_location", queryset=Location.objects.all()
    )
    requestedBy = serializers.CharField(source="requested_by_name", required=False, allow_null=True)
    date = serializers.DateField(source="request_date", required=False, allow_null=True)

    class Meta:
        model = ZoneRequest
        fields = [
            "id", "request_number", "requestedBy", "zone", "zoneLocationId",
            "target_sector", "date", "requested_at", "status", "notes",
            "manager_signoff_needed", "reject_reason", "lines",
            "created_at", "updated_at",
        ]
        read_only_fields = ["request_number", "requested_at", "created_at", "updated_at"]


class StockAuditLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)
    # GeneratedField (counted_qty - system_qty) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    variance = QuantityField(read_only=True)

    class Meta:
        model = StockAuditLine
        fields = [
            "id", "itemId", "sku", "name", "system_qty", "counted_qty",
            "variance", "reason", "adjustment_movement",
        ]
        read_only_fields = ["variance", "adjustment_movement"]


class StockAuditSerializer(BaseModelSerializer):
    lines = StockAuditLineSerializer(many=True, required=False)

    class Meta:
        model = StockAudit
        fields = [
            "id", "audit_number", "location", "period_month", "status",
            "conducted_by", "posted_at", "notes", "lines", "created_at", "updated_at",
        ]
        read_only_fields = ["audit_number", "posted_at", "created_at", "updated_at"]


class QualityStandardSerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="masters.ItemCategory", required=False, allow_null=True
    )
    checks = serializers.JSONField(source="checklist", required=False)
    active = serializers.BooleanField(source="is_active", required=False)

    class Meta:
        model = QualityStandard
        fields = [
            "id", "name", "category", "categoryId", "checks", "tolerance_pct",
            "active", "created_at", "updated_at",
        ]



class ValuationRowSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    location = serializers.CharField(allow_null=True)
    quantity = QuantityField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)
    ageingBucket = serializers.CharField(allow_null=True)


class ItemPartSerializer(BaseModelSerializer):
    """Machine BOM lines (``itemParts``)."""

    parentItemId = TenantPrimaryKeyRelatedField(source="parent_item", model="masters.Item")
    partItemId = TenantPrimaryKeyRelatedField(source="part_item", model="masters.Item")
    partName = serializers.CharField(source="part_item.name", read_only=True)
    partSku = serializers.CharField(source="part_item.sku", read_only=True)
    requiredQty = QuantityField(source="required_qty", required=False)

    class Meta:
        model = ItemPart
        fields = [
            "id", "parentItemId", "partItemId", "partName", "partSku",
            "requiredQty", "created_at",
        ]
        read_only_fields = ["created_at"]
