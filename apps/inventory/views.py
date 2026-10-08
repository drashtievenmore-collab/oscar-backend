"""Inventory endpoints (api.md §7)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.masters.models import Item, ItemPart, Location

from . import services as stock
from .models import (
    FaultyPart,
    QualityStandard,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    ZoneRequest,
    ZoneRequestLine,
)
from .serializers import (
    FaultyPartSerializer,
    ItemPartSerializer,
    QualityStandardSerializer,
    ServiceUsageSerializer,
    StockAdjustmentSerializer,
    StockAuditSerializer,
    StockMovementSerializer,
    StockTransferSerializer,
    ZoneRequestSerializer,
)

QTY = DecimalField(max_digits=18, decimal_places=4)
MONEY = DecimalField(max_digits=18, decimal_places=2)


# ---------------------------------------------------------------------------
# Stock position and movements
# ---------------------------------------------------------------------------
class StockPositionView(APIView):
    """``GET /inventory/stock/`` -- every figure derived from the ledger."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        queryset = Item.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).exclude(item_kind="Service").select_related("category")

        category_id = request.query_params.get("categoryId")
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        location_id = request.query_params.get("locationId")
        items = list(queryset)
        stock.annotate_items_with_stock(request.client_id, items)

        if location_id:
            balances = stock.balances_for(
                request.client_id, [item.id for item in items], location_id
            )
            for item in items:
                row = balances.get(item.id, {})
                item.on_hand_qty = row.get("on_hand", ZERO) or ZERO
                item.damaged_qty = row.get("damaged", ZERO) or ZERO
                item.available_qty = item.on_hand_qty - (item.reserved_qty or ZERO)
                item.stock_status = stock.stock_status(item.available_qty, item.reorder_level)

        if request.query_params.get("belowReorder") == "true":
            items = [item for item in items if item.stock_status in ("Low Stock", "Critical")]
        status_filter = request.query_params.getlist("status")
        if status_filter:
            items = [item for item in items if item.stock_status in status_filter]

        rows = []
        total_value = ZERO
        for item in items:
            unit_cost = round4(item.cost_price)
            value = round2(D(item.on_hand_qty) * unit_cost)
            total_value += value
            rows.append(
                {
                    "itemId": str(item.id),
                    "sku": item.sku,
                    "name": item.name,
                    "category": item.category.name if item.category_id else None,
                    "uom": item.uom,
                    "onHand": item.on_hand_qty,
                    "reserved": item.reserved_qty,
                    "available": item.available_qty,
                    "damaged": item.damaged_qty,
                    "reorderLevel": round4(item.reorder_level),
                    "status": item.stock_status,
                    "unitCost": unit_cost,
                    "value": value,
                }
            )

        return Response(
            envelope(
                rows,
                aggregates={
                    "totalItems": len(rows),
                    "totalValue": round2(total_value),
                    "lowStock": sum(1 for row in rows if row["status"] == "Low Stock"),
                    "critical": sum(1 for row in rows if row["status"] == "Critical"),
                    "outOfStock": sum(1 for row in rows if D(row["available"]) <= ZERO),
                },
            )
        )


class StockSummaryView(APIView):
    """``GET /inventory/stock/summary/`` -- the KPI tiles only."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        items = list(
            Item.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
            .exclude(item_kind="Service")
            .only("id", "reorder_level", "cost_price")
        )
        stock.annotate_items_with_stock(request.client_id, items)

        total_value = sum(
            (round2(D(item.on_hand_qty) * D(item.cost_price)) for item in items), ZERO
        )
        return Response(
            {
                "totalItems": len(items),
                "totalValue": round2(total_value),
                "lowStock": sum(1 for item in items if item.stock_status == "Low Stock"),
                "critical": sum(1 for item in items if item.stock_status == "Critical"),
                "outOfStock": sum(1 for item in items if D(item.available_qty) <= ZERO),
            }
        )


class StockMovementViewSet(ReadOnlyTenantViewSet):
    """``GET /inventory/movements/`` -- the movement ledger.

    Read-only by design: movements are append-only and corrections are new
    reversing movements (api.md §7.2).
    """

    queryset = StockMovement.objects.select_related("item", "location")
    serializer_class = StockMovementSerializer
    required_permissions = ["view_inventory"]
    status_field = None
    filter_map = {
        "itemId": "item_id",
        "item_id": "item_id",
        "type": "type",
        "referenceType": "reference_type",
        "reference_type": "reference_type",
        "referenceId": "reference_id",
        "locationId": "location_id",
    }
    default_date_field = "movement_date"
    allowed_date_fields = ("movement_date", "created_at")
    search_fields = ["reference_number", "item__sku", "item__name", "notes"]
    ordering = ["-movement_date", "-created_at"]

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            inward=Coalesce(
                Sum("quantity", filter=Q(quantity__gt=0)), Value(Decimal("0.0000")),
                output_field=QTY,
            ),
            outward=Coalesce(
                Sum("quantity", filter=Q(quantity__lt=0)), Value(Decimal("0.0000")),
                output_field=QTY,
            ),
            count=Count("id"),
        )
        rows["outward"] = abs(rows["outward"])
        return rows


class StockAdjustmentView(APIView):
    """``POST /inventory/adjustments/`` -- replaces ``adjustItemStock``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["adjust_stock"]

    @transaction.atomic
    def post(self, request):
        serializer = StockAdjustmentSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        item = data["itemId"]
        location = data.get("locationId") or item.default_location
        if location is None:
            raise ValidationFailed(
                "Choose the location being adjusted.",
                field_errors={"locationId": ["Required."]},
            )

        if data.get("isAbsolute"):
            current = stock.calculate_item_stock(request.client_id, item, location.id)
            delta = round4(D(data["quantity"]) - D(current["onHand"]))
        else:
            delta = round4(data["quantity"])

        if delta == ZERO:
            raise BusinessRuleViolation(
                "That adjustment would not change the stock figure.",
                code="NO_CHANGE",
            )

        movement = stock.post_movement(
            client_id=request.client_id,
            item=item,
            location=location,
            type="ADJUSTMENT",
            quantity=delta,
            unit_cost=data.get("unitCost") or item.cost_price,
            notes=data["reason"],
            user=request.user,
        )

        from apps.accounting.services import post_stock_adjustment

        post_stock_adjustment(movement, user=request.user)

        return Response(
            {
                "movement": StockMovementSerializer(movement).data,
                "stock": stock.calculate_item_stock(request.client_id, item),
            },
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Transfers (api.md §7)
# ---------------------------------------------------------------------------
class StockTransferViewSet(TenantModelViewSet):
    queryset = StockTransfer.objects.select_related(
        "from_location", "to_location"
    ).prefetch_related("items__item")
    serializer_class = StockTransferSerializer
    audit_entity_type = "StockTransfer"
    audit_label_field = "transfer_number"
    required_permissions = ["view_inventory"]
    permission_map = {"write": ["create_transfer"]}
    status_field = "status"
    default_date_field = "transfer_date"
    search_fields = ["transfer_number", "notes"]
    ordering = ["-transfer_date", "-created_at"]

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("items", [])
        if not lines:
            raise ValidationFailed(
                "A transfer needs at least one item.",
                field_errors={"items": ["Add at least one line."]},
            )
        serializer.validated_data["transfer_number"] = allocate_number(
            self.request.user.client, "TR", serializer.validated_data.get("transfer_date")
        )
        transfer = super().perform_create(serializer)

        StockTransferLine.objects.bulk_create(
            [
                StockTransferLine(client_id=self.get_client_id(), stock_transfer=transfer, **line)
                for line in lines
            ]
        )
        return transfer

    # ``dispatch`` is APIView's own entry point: a method of that name here
    # shadows it and breaks every request to this viewset. Keep the URL,
    # rename the method.
    @action(detail=True, methods=["post"], url_path="dispatch", url_name="dispatch")
    def dispatch_action(self, request, pk=None):
        """Posts the ``TRANSFER_OUT`` / ``TRANSFER_IN`` pair (api.md §7.2).

        Both movements share a ``reference_id``, and the stock between them
        sits at the destination -- which is why ``locations.type`` carries
        ``Transit`` (db.md §7.4).
        """
        transfer = self.get_object()
        if transfer.status not in ("Requested",):
            raise Conflict(
                f"This transfer is already {transfer.status}.", code=Codes.BAD_TARGET
            )

        with transaction.atomic():
            for line in transfer.items.filter(deleted_at__isnull=True).select_related("item"):
                stock.assert_sufficient_stock(
                    request.client_id, line.item, line.qty,
                    transfer.from_location_id, line.item.name,
                )
                stock.post_movement(
                    client_id=request.client_id,
                    item=line.item_id,
                    location=transfer.from_location_id,
                    type="TRANSFER_OUT",
                    quantity=-D(line.qty),
                    unit_cost=line.item.cost_price,
                    reference_type="StockTransfer",
                    reference_id=transfer.id,
                    reference_number=transfer.transfer_number,
                    movement_date=transfer.transfer_date,
                    user=request.user,
                )
                stock.post_movement(
                    client_id=request.client_id,
                    item=line.item_id,
                    location=transfer.to_location_id,
                    type="TRANSFER_IN",
                    quantity=D(line.qty),
                    unit_cost=line.item.cost_price,
                    reference_type="StockTransfer",
                    reference_id=transfer.id,
                    reference_number=transfer.transfer_number,
                    movement_date=transfer.transfer_date,
                    user=request.user,
                )
            transfer.status = "In Transit"
            transfer.save(update_fields=["status", "updated_at"])
            self.write_audit("dispatch", transfer, description="Transfer dispatched")

        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        transfer = self.get_object()
        if transfer.status != "In Transit":
            raise Conflict(
                "Only a transfer in transit can be received.", code=Codes.BAD_TARGET
            )
        with transaction.atomic():
            transfer.items.filter(deleted_at__isnull=True).update(received_qty=F("qty"))
            transfer.status = "Received"
            transfer.save(update_fields=["status", "updated_at"])
            self.write_audit("receive", transfer, description="Transfer received")
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        transfer = self.get_object()
        if transfer.status == "Cancelled":
            raise Conflict("This transfer is already cancelled.", code=Codes.ALREADY_CANCELLED)

        with transaction.atomic():
            if transfer.status == "In Transit":
                stock.reverse_movements(
                    reference_type="StockTransfer",
                    reference_id=transfer.id,
                    client_id=request.client_id,
                    user=request.user,
                    notes="Transfer cancelled",
                )
            transfer.status = "Cancelled"
            transfer.save(update_fields=["status", "updated_at"])
            self.write_audit("cancel", transfer, description=request.data.get("reason"))
        return Response(self.get_serializer(transfer).data)


# ---------------------------------------------------------------------------
# Faulty parts, service usage, zone requests
# ---------------------------------------------------------------------------
class FaultyPartViewSet(TenantModelViewSet):
    queryset = FaultyPart.objects.select_related("item", "vendor", "serial")
    serializer_class = FaultyPartSerializer
    audit_entity_type = "FaultyPart"
    audit_label_field = "rma_number"
    required_permissions = ["view_inventory"]
    status_field = "status"
    default_date_field = "reported_date"
    search_fields = ["rma_number", "item__sku", "item__name", "notes"]
    ordering = ["-reported_date"]

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["rma_number"] = allocate_number(
            self.request.user.client, "RMA"
        )
        serializer.validated_data.setdefault("reported_by", self.request.user)
        part = super().perform_create(serializer)

        # api.md §7.2 -- reporting a faulty part posts a FAULTY movement,
        # which is what makes `damaged` a derived figure rather than a guess.
        location = part.location_id or part.item.default_location_id
        if location and part.item.holds_stock:
            stock.post_movement(
                client_id=self.get_client_id(),
                item=part.item_id,
                location=location,
                type="FAULTY",
                quantity=-D(part.quantity),
                unit_cost=part.item.cost_price,
                reference_type="FaultyPart",
                reference_id=part.id,
                reference_number=part.rma_number,
                movement_date=part.reported_date,
                user=self.request.user,
            )
        if part.serial_id:
            stock.set_serial_status([part.serial_id], "faulty")
        return part

    @action(detail=True, methods=["patch"])
    def notes(self, request, pk=None):
        part = self.get_object()
        part.notes = request.data.get("notes")
        part.save(update_fields=["notes", "updated_at"])
        return Response(self.get_serializer(part).data)


class ServiceUsageViewSet(TenantModelViewSet):
    queryset = ServiceUsage.objects.select_related("item", "party")
    serializer_class = ServiceUsageSerializer
    audit_entity_type = "ServiceUsage"
    audit_label_field = "ticket_number"
    required_permissions = ["view_inventory"]
    status_field = None
    default_date_field = "used_on"
    search_fields = ["ticket_number", "technician", "item__sku"]
    ordering = ["-used_on"]

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["ticket_number"] = allocate_number(
            self.request.user.client, "TKT"
        )
        serializer.validated_data.setdefault("used_by", self.request.user)
        usage = super().perform_create(serializer)

        location = usage.item.default_location_id
        if location and usage.item.holds_stock:
            stock.assert_sufficient_stock(
                self.get_client_id(), usage.item, usage.quantity, location, usage.item.name
            )
            stock.post_movement(
                client_id=self.get_client_id(),
                item=usage.item_id,
                location=location,
                type="SERVICE_USAGE",
                quantity=-D(usage.quantity),
                unit_cost=usage.item.cost_price,
                reference_type="ServiceUsage",
                reference_id=usage.id,
                reference_number=usage.ticket_number,
                movement_date=usage.used_on,
                user=self.request.user,
            )
        return usage


class ZoneRequestViewSet(TenantModelViewSet):
    queryset = ZoneRequest.objects.select_related("zone_location").prefetch_related("lines__item")
    serializer_class = ZoneRequestSerializer
    audit_entity_type = "ZoneRequest"
    audit_label_field = "request_number"
    required_permissions = ["view_inventory"]
    status_field = "status"
    default_date_field = "request_date"
    search_fields = ["request_number", "requested_by_name", "notes"]
    ordering = ["-requested_at"]

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("lines", [])
        serializer.validated_data["request_number"] = allocate_number(
            self.request.user.client, "REQ"
        )
        serializer.validated_data.setdefault("requested_by", self.request.user)
        serializer.validated_data.setdefault("requested_by_name", self.request.user.name)
        request_row = super().perform_create(serializer)

        ZoneRequestLine.objects.bulk_create(
            [
                ZoneRequestLine(client_id=self.get_client_id(), zone_request=request_row, **line)
                for line in lines
            ]
        )
        return request_row

    @transaction.atomic
    def perform_update(self, serializer):
        """api.md §7 -- only the ``Fulfilled`` transition issues stock."""
        previous = serializer.instance.status
        request_row = super().perform_update(serializer)

        if previous != "Fulfilled" and request_row.status == "Fulfilled":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_zone_request")
            for line in request_row.lines.filter(deleted_at__isnull=True).select_related("item"):
                quantity = D(line.requested_qty) - D(line.issued_qty)
                if quantity <= ZERO or not line.item.holds_stock:
                    continue
                stock.assert_sufficient_stock(
                    self.get_client_id(), line.item, quantity,
                    request_row.zone_location_id, line.item.name,
                )
                stock.post_movement(
                    client_id=self.get_client_id(),
                    item=line.item_id,
                    location=request_row.zone_location_id,
                    type="ZONE_ISSUE",
                    quantity=-quantity,
                    unit_cost=line.item.cost_price,
                    reference_type="ZoneRequest",
                    reference_id=request_row.id,
                    reference_number=request_row.request_number,
                    user=self.request.user,
                )
                line.issued_qty = line.requested_qty
                line.save(update_fields=["issued_qty", "updated_at"])
            request_row.issued_by = self.request.user
            request_row.save(update_fields=["issued_by", "updated_at"])
        return request_row


# ---------------------------------------------------------------------------
# Machine BOM (masters.ItemPart lives here because items do)
# ---------------------------------------------------------------------------
class ItemPartViewSet(TenantModelViewSet):
    queryset = ItemPart.objects.select_related("parent_item", "part_item")
    serializer_class = ItemPartSerializer
    audit_entity_type = "ItemPart"
    status_field = None
    filter_map = {"parentItemId": "parent_item_id"}
    search_fields = ["part_item__name", "part_item__sku"]
    ordering = ["part_item__name"]
    required_permissions = ["view_inventory"]


# ---------------------------------------------------------------------------
# Audits and valuation
# ---------------------------------------------------------------------------
class StockAuditViewSet(TenantModelViewSet):
    queryset = StockAudit.objects.select_related("location").prefetch_related("lines__item")
    serializer_class = StockAuditSerializer
    audit_entity_type = "StockAudit"
    audit_label_field = "audit_number"
    required_permissions = ["view_inventory"]
    permission_map = {"write": ["perform_audit"]}
    status_field = "status"
    default_date_field = "period_month"
    ordering = ["-period_month"]

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("lines", [])
        serializer.validated_data["audit_number"] = allocate_number(
            self.request.user.client, "AUD"
        )
        serializer.validated_data.setdefault("conducted_by", self.request.user)
        audit = super().perform_create(serializer)

        if not lines:
            # An audit with no explicit lines counts everything with stock, so
            # the user starts from the system's numbers rather than a blank grid.
            items = list(
                Item.objects.filter(
                    client_id=self.get_client_id(), deleted_at__isnull=True
                ).exclude(item_kind="Service")
            )
            stock.annotate_items_with_stock(self.get_client_id(), items)
            lines = [
                {"item": item, "system_qty": item.on_hand_qty}
                for item in items
                if D(item.on_hand_qty) != ZERO
            ]

        StockAuditLine.objects.bulk_create(
            [
                StockAuditLine(client_id=self.get_client_id(), stock_audit=audit, **line)
                for line in lines
            ]
        )
        return audit

    @action(detail=True, methods=["post"])
    def post_variances(self, request, pk=None):
        """Turns every non-zero variance into an ``ADJUSTMENT`` movement and
        stamps the back-link (db.md §7.4)."""
        audit = self.get_object()
        if audit.status == "Posted":
            raise Conflict("This audit is already posted.", code=Codes.ALREADY_DONE)

        posted = 0
        with transaction.atomic():
            for line in audit.lines.filter(deleted_at__isnull=True).select_related("item"):
                if line.counted_qty is None:
                    continue
                variance = round4(D(line.counted_qty) - D(line.system_qty))
                if variance == ZERO:
                    continue

                location = audit.location_id or line.item.default_location_id
                if location is None:
                    continue

                movement = stock.post_movement(
                    client_id=request.client_id,
                    item=line.item_id,
                    location=location,
                    type="ADJUSTMENT",
                    quantity=variance,
                    unit_cost=line.item.cost_price,
                    notes=line.reason or f"Stock audit {audit.audit_number}",
                    user=request.user,
                )
                line.adjustment_movement = movement
                line.save(update_fields=["adjustment_movement", "updated_at"])
                posted += 1

            audit.status = "Posted"
            audit.posted_at = timezone.now()
            audit.save(update_fields=["status", "posted_at", "updated_at"])
            self.write_audit(
                "post", audit, description=f"{posted} variance adjustment(s) posted"
            )

        return Response({"postedAdjustments": posted, "status": audit.status})


class ValuationView(APIView):
    """``GET /inventory/valuation/`` -- ``?asOf=``, ``?method=FIFO|WAC``.

    db.md Appendix B decision 5 assumes WAC from the movement ``unit_cost``;
    FIFO would need a ``stock_layers`` table, so it is reported as unsupported
    rather than silently returning WAC under a FIFO label.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        method = (request.query_params.get("method") or "WAC").upper()
        if method not in ("WAC", "FIFO"):
            raise ValidationFailed(
                "Unknown valuation method.",
                field_errors={"method": ["Expected WAC or FIFO."]},
            )
        if method == "FIFO":
            raise ValidationFailed(
                "FIFO valuation is not available on this workspace.",
                code="VALUATION_METHOD_UNSUPPORTED",
                detail="FIFO requires per-receipt cost layers; WAC is available.",
            )

        balances = (
            StockBalance.objects.filter(
                client_id=request.client_id, deleted_at__isnull=True
            )
            .select_related("item", "item__category", "location")
            .exclude(item__item_kind="Service")
        )

        rows = []
        total = ZERO
        for balance in balances:
            if D(balance.on_hand) == ZERO:
                continue
            unit_cost = balance.weighted_average_cost or D(balance.item.cost_price)
            value = round2(D(balance.on_hand) * D(unit_cost))
            total += value
            rows.append(
                {
                    "itemId": str(balance.item_id),
                    "sku": balance.item.sku,
                    "name": balance.item.name,
                    "category": balance.item.category.name if balance.item.category_id else None,
                    "location": balance.location.name,
                    "quantity": balance.on_hand,
                    "unitCost": round4(unit_cost),
                    "value": value,
                }
            )

        rows.sort(key=lambda row: row["value"], reverse=True)
        return Response(
            envelope(rows, aggregates={"method": "WAC", "totalValuation": round2(total)})
        )


class QualityStandardViewSet(TenantModelViewSet):
    queryset = QualityStandard.objects.select_related("category")
    serializer_class = QualityStandardSerializer
    audit_entity_type = "QualityStandard"
    audit_label_field = "name"
    required_permissions = ["view_purchase"]
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]
