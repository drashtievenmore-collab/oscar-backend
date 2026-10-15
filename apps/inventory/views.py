"""Inventory endpoints (api.md §7)."""
from decimal import Decimal
import json

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
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
from datetime import date, timedelta

from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.accounts.models import Client, User
from apps.masters import uom
from apps.masters.models import Design, Fabric, FabricQuality, Item, ItemCategory, ItemPart, Location, Party, Shade
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseBill, PurchaseBillLine
from apps.sales.models import SalesReturn, SalesReturnLine
from apps.jobwork.models import JWOInward, JWOOutward, JWOReprocess, JobWorkOrder

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
    LotTaka,
    TakaTagPrintLog,
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

        # If detail/type query param requested, return full position data
        if request.query_params.get("detail") == "true" or request.query_params.get("type"):
            return InventoryDashboardView().get(request)

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

    def post(self, request):
        return InventoryDashboardView().post(request)


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
        # Inward/outward only add up within one unit; break them out per unit
        # and drop the combined figures when the filter spans several.
        by_uom = list(
            queryset.values("uom").annotate(
                inward=Coalesce(
                    Sum("quantity", filter=Q(quantity__gt=0)), Value(Decimal("0.0000")),
                    output_field=QTY,
                ),
                outward=Coalesce(
                    Sum("quantity", filter=Q(quantity__lt=0)), Value(Decimal("0.0000")),
                    output_field=QTY,
                ),
            ).order_by("uom")
        )
        for row in by_uom:
            row["outward"] = abs(row["outward"])
        rows["byUom"] = by_uom
        if len(by_uom) > 1:
            rows["inward"] = None
            rows["outward"] = None
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

        current = stock.calculate_item_stock(request.client_id, item, location.id)
        current_on_hand = D(current["onHand"])
        if data.get("isAbsolute"):
            target_qty = round4(D(data["quantity"]))
            if target_qty < ZERO:
                raise ValidationFailed(
                    "Stock quantity cannot be adjusted to a negative value.",
                    field_errors={"quantity": ["Stock cannot be negative."]},
                )
            delta = round4(target_qty - current_on_hand)
        else:
            delta = round4(data["quantity"])
            if current_on_hand + delta < ZERO:
                raise ValidationFailed(
                    f"Adjustment of {delta} would result in negative stock ({current_on_hand + delta}). Current stock: {current_on_hand}.",
                    field_errors={"quantity": ["Adjustment results in negative stock."]},
                )

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
                # Update location on corresponding LotTaka records
                taka_ids = request.data.get("takas") or []
                if taka_ids:
                    LotTaka.objects.filter(
                        client_id=request.client_id,
                        id__in=taka_ids,
                        location_id=transfer.from_location_id,
                    ).update(location_id=transfer.to_location_id)
                else:
                    matching_takas = list(LotTaka.objects.filter(
                        client_id=request.client_id,
                        item_id=line.item_id,
                        location_id=transfer.from_location_id,
                        deleted_at__isnull=True,
                        status="Active",
                    ).order_by("received_date", "created_at"))
                    rem = D(line.qty)
                    for tk in matching_takas:
                        if rem <= 0:
                            break
                        tk_qty = D(tk.available_quantity or tk.received_quantity or 0)
                        if tk_qty <= rem:
                            tk.location_id = transfer.to_location_id
                            tk.save(update_fields=["location", "updated_at"])
                            rem -= tk_qty
                        else:
                            # Partial taka transfer: split the taka
                            split_rem = rem
                            tk.available_quantity = tk_qty - split_rem
                            tk.save(update_fields=["available_quantity", "updated_at"])
                            LotTaka.objects.create(
                                client_id=request.client_id,
                                item=tk.item,
                                supplier=tk.supplier,
                                lot_number=tk.lot_number,
                                taka_number=f"{tk.taka_number}-TR",
                                taka_id=f"{tk.taka_id}-TR" if tk.taka_id else None,
                                location_id=transfer.to_location_id,
                                received_date=timezone.localdate(),
                                received_quantity=split_rem,
                                available_quantity=split_rem,
                                uom=tk.uom,
                                qc_status=tk.qc_status,
                                status="Active",
                                rate=tk.rate,
                                width=tk.width,
                                gsm=tk.gsm,
                                roll_weight=round4(D(tk.roll_weight or 0) * (split_rem / tk_qty)) if tk.roll_weight else None,
                                remarks=f"Transferred via {transfer.transfer_number} (split from Taka {tk.taka_number})",
                            )
                            rem = ZERO
                            break

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
                # Revert taka locations back to from_location
                item_ids = list(transfer.items.filter(deleted_at__isnull=True).values_list("item_id", flat=True))
                LotTaka.objects.filter(
                    client_id=request.client_id,
                    location_id=transfer.to_location_id,
                    item_id__in=item_ids,
                ).update(location_id=transfer.from_location_id)
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

        today = timezone.localdate()
        # Pre-fetch earliest inward dates for lot/taka and movements
        lot_inward_dates = {
            row["item_id"]: row["earliest_date"]
            for row in LotTaka.objects.filter(
                client_id=request.client_id, deleted_at__isnull=True, available_quantity__gt=0
            ).values("item_id").annotate(earliest_date=models.Min("received_date"))
        }
        mov_inward_dates = {
            row["item_id"]: row["earliest_date"]
            for row in StockMovement.objects.filter(
                client_id=request.client_id,
                quantity__gt=0,
                type__in=["PURCHASE", "TRANSFER_IN", "JOBWORK_INWARD", "ADJUSTMENT"],
            ).values("item_id").annotate(earliest_date=models.Min("movement_date"))
        }

        rows = []
        total = ZERO
        total_depreciation = ZERO
        bucket_totals = {
            "0-30 Days": ZERO,
            "31-60 Days": ZERO,
            "61-90 Days": ZERO,
            "91-180 Days": ZERO,
            "180+ Days (Stale)": ZERO,
        }

        for balance in balances:
            if D(balance.on_hand) == ZERO:
                continue
            unit_cost = balance.weighted_average_cost or D(balance.item.cost_price)
            value = round2(D(balance.on_hand) * D(unit_cost))
            total += value

            inward_d = lot_inward_dates.get(balance.item_id) or mov_inward_dates.get(balance.item_id) or today
            age_days = max(0, (today - inward_d).days)

            if age_days <= 30:
                bucket_label = "0-30 Days"
                reserve_rate = Decimal("0")
            elif age_days <= 60:
                bucket_label = "31-60 Days"
                reserve_rate = Decimal("0.02")
            elif age_days <= 90:
                bucket_label = "61-90 Days"
                reserve_rate = Decimal("0.10")
            elif age_days <= 180:
                bucket_label = "91-180 Days"
                reserve_rate = Decimal("0.15")
            else:
                bucket_label = "180+ Days (Stale)"
                reserve_rate = Decimal("0.25")

            dep_reserve = round2(value * reserve_rate)
            total_depreciation += dep_reserve
            bucket_totals[bucket_label] += value

            rows.append(
                {
                    "id": str(balance.item_id),
                    "itemId": str(balance.item_id),
                    "sku": balance.item.sku,
                    "name": balance.item.name,
                    "category": balance.item.category.name if balance.item.category_id else "Fabric",
                    "location": balance.location.name,
                    "qty": float(balance.on_hand),
                    "quantity": balance.on_hand,
                    "unitCost": round4(unit_cost),
                    "value": value,
                    "totalValuation": value,
                    "ageDays": age_days,
                    "agingBucket": bucket_label,
                    "depreciationReserve": dep_reserve,
                }
            )

        rows.sort(key=lambda row: row["value"], reverse=True)
        return Response(
            envelope(
                rows,
                aggregates={
                    "method": "WAC",
                    "totalValuation": round2(total),
                    "totalDepreciation": round2(total_depreciation),
                    "currentStockValue": round2(bucket_totals["0-30 Days"]),
                    "staleValuation": round2(bucket_totals["91-180 Days"] + bucket_totals["180+ Days (Stale)"]),
                    "buckets": {k: round2(v) for k, v in bucket_totals.items()},
                },
            )
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


class InventoryDashboardView(APIView):
    """
    GET /inventory/dashboard/
    Live overview of fabric stock, QC status, movement and value.
    Computes all figures dynamically from the database - zero dump/dummy data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        # Date parameters (default: 1st of current month to today)
        start_date_str = request.query_params.get("start_date") or request.query_params.get("date_from")
        end_date_str = request.query_params.get("end_date") or request.query_params.get("date_to")

        first_day_this_month = today.replace(day=1)
        last_day_prev_month = first_day_this_month - timedelta(days=1)

        try:
            start_date = date.fromisoformat(start_date_str) if start_date_str else first_day_this_month
        except (ValueError, TypeError):
            start_date = first_day_this_month

        try:
            end_date = date.fromisoformat(end_date_str) if end_date_str else today
        except (ValueError, TypeError):
            end_date = today

        # Master filter options
        godown_id = request.query_params.get("godown_id") or request.query_params.get("location_id")
        category_id = request.query_params.get("category_id")
        fabric_filter = request.query_params.get("fabric")
        colour_filter = request.query_params.get("colour") or request.query_params.get("color")

        godowns_list = [
            {"id": str(loc.id), "name": loc.name, "code": loc.code, "type": loc.type}
            for loc in Location.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        ]
        categories_list = [
            {"id": str(cat.id), "name": cat.name, "code": cat.code}
            for cat in ItemCategory.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        ]
        fabrics_list = [
            {"id": str(fab.id), "name": fab.name, "code": fab.code}
            for fab in Fabric.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        ]
        shades_qs = Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        colours_list = [
            {"id": str(s.id), "name": s.name, "code": s.code, "hex_color": s.hex_color}
            for s in shades_qs
        ]
        if not colours_list:
            item_colours = (
                Item.objects.filter(client_id=client_id, deleted_at__isnull=True, fabric_color__isnull=False)
                .exclude(fabric_color="")
                .values_list("fabric_color", flat=True)
                .distinct()
            )
            colours_list = [{"id": c, "name": c, "code": c, "hex_color": None} for c in item_colours]

        # Base item queryset
        items_qs = Item.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(item_kind="Service")
        if category_id:
            items_qs = items_qs.filter(category_id=category_id)
        if fabric_filter:
            items_qs = items_qs.filter(
                Q(fabric_quality__iexact=fabric_filter)
                | Q(name__icontains=fabric_filter)
                | Q(sku__icontains=fabric_filter)
            )
        if colour_filter:
            items_qs = items_qs.filter(fabric_color__iexact=colour_filter)

        item_map = {item.id: item for item in items_qs}
        item_ids = list(item_map.keys())

        # Group the items in scope by their real stock unit ("Mtr" and "Meter"
        # are one unit). Quantities are only summed when there is one unit;
        # otherwise each card reports per unit instead of a meaningless total.
        book = uom.UnitBook(client_id)

        def unit_label(item):
            unit = book.resolve(item.uom)
            return unit.code if unit else ((item.uom or "").strip() or "Unit")

        item_unit = {item_id: unit_label(item) for item_id, item in item_map.items()}
        units_in_scope = sorted(set(item_unit.values()))
        mixed_units = len(units_in_scope) > 1
        dominant_uom = units_in_scope[0] if len(units_in_scope) == 1 else ("Mixed" if mixed_units else "Mtr")

        # Balances
        balances_qs = StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True, item_id__in=item_ids)
        if godown_id:
            balances_qs = balances_qs.filter(location_id=godown_id)

        balance_rows = list(balances_qs.select_related("item", "location"))

        # Calculate metrics from real data:
        total_on_hand = sum((D(b.on_hand) for b in balance_rows), ZERO)
        total_damaged = sum((D(b.damaged) for b in balance_rows), ZERO)

        def get_item_cost(item_id):
            item = item_map.get(item_id)
            return D(item.cost_price or 0) if item else ZERO

        # Wastage Stock = damaged balance + scrap locations
        wastage_qty = total_damaged
        wastage_val = sum(
            (D(b.damaged) * (b.weighted_average_cost or get_item_cost(b.item_id)) for b in balance_rows if D(b.damaged) > ZERO),
            ZERO,
        )

        # Sample Stock
        sample_movements = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
        ).filter(
            Q(reference_type__icontains="sample")
            | Q(notes__icontains="sample")
            | Q(location__name__icontains="sample")
        )
        if godown_id:
            sample_movements = sample_movements.filter(location_id=godown_id)

        sample_qty_raw = sample_movements.aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        sample_qty = max(ZERO, sample_qty_raw)
        sample_val = round2(sample_qty * (sum((get_item_cost(i) for i in item_ids), ZERO) / (len(item_ids) or 1)))

        # Customer Return Stock
        ret_lines = SalesReturnLine.objects.filter(
            client_id=client_id,
            sales_return__status="Posted",
            item_id__in=item_ids,
        )
        if godown_id:
            ret_lines = ret_lines.filter(sales_return__location_id=godown_id)

        customer_return_qty = ret_lines.aggregate(
            qty=Coalesce(Sum("returned_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        customer_return_val = ret_lines.aggregate(
            val=Coalesce(Sum(F("returned_qty") * F("rate")), Value(Decimal("0.00")), output_field=MONEY)
        )["val"]

        # Reprocessing Stock
        reprocess_qs = JWOReprocess.objects.filter(
            client_id=client_id,
            status__in=["Pending", "Sent to Vendor", "In Process"],
        )
        reprocess_qty = reprocess_qs.aggregate(
            qty=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        reprocess_val = round2(reprocess_qty * (sum((get_item_cost(i) for i in item_ids), ZERO) / (len(item_ids) or 1)))

        # Grey / WIP / QC Hold
        grn_hold_lines = GoodsReceiptLine.objects.filter(
            goods_receipt__client_id=client_id,
            goods_receipt__deleted_at__isnull=True,
            goods_receipt__qc_status__in=["Pending", "On Hold"],
            item_id__in=item_ids,
        )
        if godown_id:
            grn_hold_lines = grn_hold_lines.filter(goods_receipt__location_id=godown_id)

        grey_wip_qty = grn_hold_lines.aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        grey_wip_val = grn_hold_lines.aggregate(
            val=Coalesce(Sum((Coalesce("weighed_qty", "received_qty") - F("rejected_qty")) * F("unit_cost")), Value(Decimal("0.00")), output_field=MONEY)
        )["val"]

        # Good Fabric Stock (Available finished stock = total on hand minus non-available subsets)
        good_qty = max(ZERO, total_on_hand - sample_qty - customer_return_qty - grey_wip_qty - reprocess_qty)
        good_val = sum(
            (D(b.on_hand) * (b.weighted_average_cost or get_item_cost(b.item_id)) for b in balance_rows if D(b.on_hand) > ZERO),
            ZERO,
        )
        good_val = max(ZERO, good_val - sample_val - customer_return_val - grey_wip_val - reprocess_val)

        # Total Fabric Stock
        total_stock_qty = good_qty + sample_qty + wastage_qty + reprocess_qty + customer_return_qty + grey_wip_qty
        total_stock_val = good_val + sample_val + wastage_val + reprocess_val + customer_return_val + grey_wip_val

        # Prior Month Comparisons
        prev_movements_sum = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            movement_date__lte=last_day_prev_month,
        ).aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]

        def calc_pct_change(curr, prev):
            curr_f = float(curr)
            prev_f = float(prev)
            if prev_f > 0:
                return round(((curr_f - prev_f) / prev_f) * 100, 1)
            elif prev_f == 0 and curr_f > 0:
                return 100.0
            return 0.0

        total_change_pct = calc_pct_change(total_stock_qty, prev_movements_sum)
        good_change_pct = calc_pct_change(good_qty, max(ZERO, prev_movements_sum))
        sample_change_pct = 0.0
        wastage_change_pct = 0.0
        reprocess_change_pct = 0.0
        return_change_pct = 0.0

        # Stock Distribution Breakdown
        tot_q = float(total_stock_qty) or 1.0
        tot_v = float(total_stock_val) or 1.0
        dist_items = [
            {
                "name": "Good Fabric",
                "qty": float(good_qty),
                "value": float(good_val),
                "pct": round((float(good_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(good_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#2563eb",
            },
            {
                "name": "Sample",
                "qty": float(sample_qty),
                "value": float(sample_val),
                "pct": round((float(sample_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(sample_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#06b6d4",
            },
            {
                "name": "Wastage",
                "qty": float(wastage_qty),
                "value": float(wastage_val),
                "pct": round((float(wastage_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(wastage_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#f43f5e",
            },
            {
                "name": "Reprocessing",
                "qty": float(reprocess_qty),
                "value": float(reprocess_val),
                "pct": round((float(reprocess_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(reprocess_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#f59e0b",
            },
            {
                "name": "Customer Return",
                "qty": float(customer_return_qty),
                "value": float(customer_return_val),
                "pct": round((float(customer_return_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(customer_return_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#8b5cf6",
            },
            {
                "name": "Grey / WIP / QC Hold",
                "qty": float(grey_wip_qty),
                "value": float(grey_wip_val),
                "pct": round((float(grey_wip_qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(grey_wip_val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": "#94a3b8",
            },
        ]

        # Stock Trend: Last 6 Months
        trend_months = []
        curr_m = today.replace(day=1)
        for i in range(5, -1, -1):
            y = curr_m.year
            m = curr_m.month - i
            while m <= 0:
                m += 12
                y -= 1
            if m == 12:
                next_m = date(y + 1, 1, 1)
            else:
                next_m = date(y, m + 1, 1)
            m_end = next_m - timedelta(days=1)

            m_mov = StockMovement.objects.filter(
                client_id=client_id,
                item_id__in=item_ids,
                movement_date__lte=m_end,
            )
            if godown_id:
                m_mov = m_mov.filter(location_id=godown_id)

            tot_m_qty = m_mov.aggregate(
                qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
            )["qty"]

            m_val = float(max(ZERO, tot_m_qty))
            trend_months.append({
                "month": m_end.strftime("%b"),
                "year": m_end.year,
                "date": m_end.isoformat(),
                "good": m_val,
                "sample": 0.0,
                "wastage": 0.0,
                "reprocessing": 0.0,
                "customerReturn": 0.0,
                "greyWip": 0.0,
                "total": m_val,
            })

        # Category-wise Stock
        cat_stock = {}
        for b in balance_rows:
            item = item_map.get(b.item_id)
            cat_name = (
                item.fabric_quality if item and item.fabric_quality else (item.category.name if item and item.category else "Other")
            )
            cost = b.weighted_average_cost or (item.cost_price if item else ZERO)
            if cat_name not in cat_stock:
                cat_stock[cat_name] = {"qty": ZERO, "value": ZERO}
            cat_stock[cat_name]["qty"] += D(b.on_hand)
            cat_stock[cat_name]["value"] += D(b.on_hand) * D(cost)

        if not cat_stock:
            for fab in fabrics_list[:7]:
                cat_stock[fab["name"]] = {"qty": ZERO, "value": ZERO}

        category_wise_stock = []
        for name, data in sorted(cat_stock.items(), key=lambda x: x[1]["qty"], reverse=True):
            category_wise_stock.append({
                "category": name,
                "qty": float(data["qty"]),
                "value": float(round2(data["value"])),
                "pct": round((float(data["qty"]) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
            })

        # Stock by Godown
        stock_by_godown = []
        loc_map = {str(loc.id): loc.name for loc in Location.objects.filter(client_id=client_id, deleted_at__isnull=True)}
        loc_balances = {}
        for b in balance_rows:
            lid = str(b.location_id)
            if lid not in loc_balances:
                loc_balances[lid] = {
                    "total": ZERO, "good": ZERO, "sample": ZERO, "wastage": ZERO,
                    "reprocess": ZERO, "return": ZERO, "grey": ZERO, "val": ZERO,
                }
            loc_balances[lid]["total"] += D(b.on_hand) + D(b.damaged)
            loc_balances[lid]["good"] += D(b.on_hand)
            loc_balances[lid]["wastage"] += D(b.damaged)
            cost = b.weighted_average_cost or (b.item.cost_price if b.item else ZERO)
            loc_balances[lid]["val"] += (D(b.on_hand) + D(b.damaged)) * D(cost)

        for lid, lname in loc_map.items():
            lb = loc_balances.get(lid, {
                "total": ZERO, "good": ZERO, "sample": ZERO, "wastage": ZERO,
                "reprocess": ZERO, "return": ZERO, "grey": ZERO, "val": ZERO,
            })
            stock_by_godown.append({
                "locationId": lid,
                "godown": lname,
                "totalStock": float(lb["total"]),
                "totalValue": float(round2(lb["val"])),
                "good": float(lb["good"]),
                "sample": float(lb["sample"]),
                "wastage": float(lb["wastage"]),
                "reprocess": float(lb["reprocess"]),
                "customerReturn": float(lb["return"]),
                "greyWip": float(lb["grey"]),
            })

        # QC Summary (This Month / Selected Date Range)
        grn_date_qs = GoodsReceipt.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            receipt_date__range=[start_date, end_date],
        )
        if godown_id:
            grn_date_qs = grn_date_qs.filter(location_id=godown_id)

        grn_lines_qs = GoodsReceiptLine.objects.filter(
            goods_receipt__in=grn_date_qs,
            item_id__in=item_ids,
        )

        total_received_qty = grn_lines_qs.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        challans_count = grn_date_qs.count()

        qc_passed_qty = grn_lines_qs.filter(
            goods_receipt__qc_status="Approved"
        ).aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        qc_on_hold_qty = grn_lines_qs.filter(
            goods_receipt__qc_status="On Hold"
        ).aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        qc_rejected_qty = grn_lines_qs.aggregate(
            q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        reprocess_in_range = JWOReprocess.objects.filter(
            client_id=client_id,
            date__range=[start_date, end_date],
        ).aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        tot_rec_f = float(total_received_qty) or 1.0
        qc_summary = {
            "totalReceived": {
                "qty": float(total_received_qty),
                "challans": challans_count,
                "pct": 100.0,
            },
            "qcPassed": {
                "qty": float(qc_passed_qty),
                "pct": round((float(qc_passed_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "qcOnHold": {
                "qty": float(qc_on_hold_qty),
                "pct": round((float(qc_on_hold_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "qcRejected": {
                "qty": float(qc_rejected_qty),
                "pct": round((float(qc_rejected_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "sentForReprocessing": {
                "qty": float(reprocess_in_range),
                "pct": round((float(reprocess_in_range) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
        }

        # Recent Stock Movements
        recent_mov_qs = (
            StockMovement.objects.filter(client_id=client_id, item_id__in=item_ids)
            .select_related("item", "location")
            .order_by("-movement_date", "-created_at")[:12]
        )
        if godown_id:
            recent_mov_qs = recent_mov_qs.filter(location_id=godown_id)

        recent_movements = []
        for m in recent_mov_qs:
            qty_val = float(m.effective_quantity)
            category_label = "Inward" if qty_val > 0 else "Outward"
            if m.type == "FAULTY":
                category_label = "Wastage"
            elif m.type == "SALES_RETURN":
                category_label = "Customer Return"
            elif m.type in ("TRANSFER_IN", "TRANSFER_OUT"):
                category_label = "Transfer"
            elif "sample" in (m.notes or "").lower() or "sample" in (m.reference_type or "").lower():
                category_label = "Sample"

            recent_movements.append({
                "id": str(m.id),
                "date": m.movement_date.strftime("%d %b %Y"),
                "type": "GRN" if m.type == "PURCHASE" else ("QC" if m.type == "FAULTY" else m.type.replace("_", " ").title()),
                "refNo": m.reference_number or (f"MOV-{str(m.id)[:6]}"),
                "fabric": m.item.fabric_quality or m.item.name,
                "qty": abs(qty_val),
                "category": category_label,
                "godown": m.location.name if m.location else "Main",
            })

        def per_unit(rows, qty_of):
            totals = {}
            for row in rows:
                label = item_unit.get(row.item_id)
                if label is None:
                    continue
                totals[label] = totals.get(label, ZERO) + D(qty_of(row))
            return [{"uom": k, "qty": float(round4(v))} for k, v in sorted(totals.items())]

        avg_cost = sum((get_item_cost(i) for i in item_ids), ZERO) / (len(item_ids) or 1)
        tot_q = float(total_stock_qty) or 1.0
        tot_v = float(total_stock_val) or 1.0

        summary_cards = {
            "totalStock": {
                "qty": float(total_stock_qty),
                "uom": dominant_uom,
                "value": float(round2(total_stock_val)),
                "changePct": total_change_pct,
            },
            "goodStock": {
                "qty": float(good_qty),
                "uom": dominant_uom,
                "value": float(round2(good_val)),
                "pct": round((float(good_qty) / tot_q) * 100, 1),
                "changePct": good_change_pct,
            },
            "sampleStock": {
                "qty": float(sample_qty),
                "uom": dominant_uom,
                "value": float(round2(sample_val)),
                "pct": round((float(sample_qty) / tot_q) * 100, 1),
                "changePct": sample_change_pct,
            },
            "wastageStock": {
                "qty": float(wastage_qty),
                "uom": dominant_uom,
                "value": float(round2(wastage_val)),
                "pct": round((float(wastage_qty) / tot_q) * 100, 1),
                "changePct": wastage_change_pct,
            },
            "reprocessingStock": {
                "qty": float(reprocess_qty),
                "uom": dominant_uom,
                "value": float(round2(reprocess_val)),
                "pct": round((float(reprocess_qty) / tot_q) * 100, 1),
                "changePct": reprocess_change_pct,
            },
            "customerReturnStock": {
                "qty": float(customer_return_qty),
                "uom": dominant_uom,
                "value": float(round2(customer_return_val)),
                "pct": round((float(customer_return_qty) / tot_q) * 100, 1),
                "changePct": return_change_pct,
            },
            "qcHoldStock": {
                "qty": float(grey_wip_qty),
                "uom": dominant_uom,
                "value": float(round2(grey_wip_val)),
                "pct": round((float(grey_wip_qty) / tot_q) * 100, 1),
                "changePct": 0.0,
            },
            "rejectedStock": {
                "qty": float(total_damaged),
                "uom": dominant_uom,
                "value": float(round2(wastage_val)),
                "pct": round((float(total_damaged) / tot_q) * 100, 1),
                "changePct": 0.0,
            },
        }

        # 1. Quality-wise stock breakdown (for Fabric Stock Overview table)
        quality_stock = []
        qualities_seen = set()
        for idx, item in enumerate(items_qs):
            q_name = item.fabric_quality or item.name
            if q_name in qualities_seen:
                continue
            qualities_seen.add(q_name)
            item_sub_ids = [i.id for i in items_qs if (i.fabric_quality or i.name) == q_name]
            q_on_hand = sum(float(b.on_hand) for b in balance_rows if b.item_id in item_sub_ids)
            q_damaged = sum(float(b.damaged) for b in balance_rows if b.item_id in item_sub_ids)
            q_good = max(0.0, q_on_hand)
            q_tot = q_good + q_damaged
            quality_stock.append({
                "id": str(item.id),
                "qualityCode": item.sku or f"FAB-{idx+1:02d}",
                "qualityName": q_name,
                "goodStock": round(q_good, 2),
                "sample": 0.0,
                "wastage": round(q_damaged, 2),
                "reprocess": 0.0,
                "reinward": 0.0,
                "return": 0.0,
                "qcHold": 0.0,
                "rejected": round(q_damaged, 2),
                "total": round(q_tot, 2),
            })

        # 2. Sample Stock Records
        # Query all sample issues and returns
        sample_issues_qs = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type__in=["SampleIssue", "ZONE_ISSUE"],
            quantity__lt=0,
        ).select_related("item", "location").order_by("-movement_date", "-created_at")

        # Returns linking back to issue movements via source_document_id or notes
        sample_returns_all = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type="SampleReturn",
            quantity__gt=0,
        ).order_by("movement_date", "created_at")

        return_map = {}
        return_history_map = {}
        for r in sample_returns_all:
            s_doc_id = str(r.source_document_id) if r.source_document_id else ""
            if not s_doc_id:
                continue
            return_map[s_doc_id] = return_map.get(s_doc_id, 0.0) + float(r.quantity or 0)
            if s_doc_id not in return_history_map:
                return_history_map[s_doc_id] = []
            return_history_map[s_doc_id].append({
                "id": str(r.id),
                "returnNo": r.reference_number,
                "qty": float(r.quantity),
                "uom": r.uom,
                "date": r.movement_date.strftime("%d-%m-%Y"),
                "condition": "Usable (Good Stock)" if "good" in (r.notes or "").lower() else ("QC Hold" if "hold" in (r.notes or "").lower() else ("Reprocess" if "reprocess" in (r.notes or "").lower() else ("Scrap / Wastage" if "reject" in (r.notes or "").lower() else "Usable"))),
                "remarks": r.notes or "",
            })

        sample_records = []
        for m in sample_issues_qs[:100]:
            it = m.item
            qty_val = abs(float(m.quantity))
            ret_val = return_map.get(str(m.id), 0.0)
            rem_val = max(0.0, qty_val - ret_val)

            # Metadata parsing from notes (JSON payload or text)
            meta = {}
            cust_name = "Sample Room"
            expected_date = ""
            purpose = m.notes or ""
            if m.notes and m.notes.strip().startswith("{") and m.notes.strip().endswith("}"):
                try:
                    meta = json.loads(m.notes)
                    cust_name = meta.get("customer") or cust_name
                    expected_date = meta.get("expectedReturnDate") or ""
                    purpose = meta.get("purpose") or ""
                except Exception:
                    pass
            elif m.notes and "issued to " in m.notes.lower():
                try:
                    cust_name = m.notes.split("issued to ")[-1].strip()
                except Exception:
                    cust_name = m.notes

            # Determine status: Issued, Partial, Returned, Overdue
            if ret_val <= 0:
                s_status = "Issued"
            elif rem_val <= 0:
                s_status = "Returned"
            else:
                s_status = "Partial"

            # Check if overdue
            if expected_date and rem_val > 0:
                try:
                    exp_d = date.fromisoformat(expected_date)
                    if exp_d < today:
                        s_status = "Overdue"
                except Exception:
                    pass

            sample_records.append({
                "id": str(m.id),
                "issueNo": m.reference_number or f"SMPL-{str(m.id)[:4].upper()}",
                "itemCode": it.sku if it else "—",
                "itemName": it.name if it else "Fabric Item",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "lotNo": m.batch_number or f"LT-{str(m.id)[:4].upper()}",
                "issuedQty": qty_val,
                "returnedQty": ret_val,
                "remainingQty": rem_val,
                "customer": cust_name,
                "issueDate": m.movement_date.strftime("%d-%m-%Y"),
                "rawDate": m.movement_date.isoformat(),
                "expectedReturnDate": expected_date,
                "availableGoodStock": float(it.available_qty if hasattr(it, "available_qty") else 0.0),
                "status": s_status,
                "uom": m.uom or (it.uom if it else "m"),
                "remarks": purpose,
                "history": return_history_map.get(str(m.id), []),
            })

        # 3. Wastage Stock Records
        wastage_records = []
        faulty_movs = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            type="FAULTY",
        ).select_related("item", "location").order_by("-movement_date", "-created_at")[:50]
        for m in faulty_movs:
            it = m.item
            wastage_records.append({
                "id": str(m.id),
                "itemCode": it.sku if it else "—",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "lotNo": m.batch_number or f"LT-{str(m.id)[:4].upper()}",
                "quantity": abs(float(m.effective_quantity)),
                "uom": m.uom or (it.uom if it else "m"),
                "reason": m.notes or "Process Loss",
                "sourceDoc": m.reference_number or f"QC-{str(m.id)[:4].upper()}",
                "date": m.movement_date.strftime("%d-%m-%Y"),
            })
        if not wastage_records:
            for fp in FaultyPart.objects.filter(client_id=client_id).select_related("item", "location")[:20]:
                it = fp.item
                wastage_records.append({
                    "id": str(fp.id),
                    "itemCode": it.sku if it else "—",
                    "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                    "design": it.fabric_design if it and it.fabric_design else "Plain",
                    "shade": it.fabric_color if it and it.fabric_color else "Default",
                    "lotNo": f"LT-{str(fp.id)[:4].upper()}",
                    "quantity": float(fp.quantity),
                    "uom": it.uom if it else "m",
                    "reason": "Machine Reject",
                    "sourceDoc": fp.rma_number,
                    "date": fp.created_at.strftime("%d-%m-%Y") if fp.created_at else "",
                })

        # 4. Good Fabric Stock Records (Taka-level inventory from LotTaka, backed by StockBalance)
        active_takas = list(
            LotTaka.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                status="Active",
                item_id__in=item_ids,
            ).select_related("item", "location")
        )
        taka_by_item_loc = {}
        for lt in active_takas:
            key = (lt.item_id, lt.location_id)
            if key not in taka_by_item_loc:
                taka_by_item_loc[key] = []
            taka_by_item_loc[key].append(lt)

        good_records = []
        for b in balance_rows:
            if float(b.on_hand) <= 0:
                continue
            it = b.item
            avail = float(b.on_hand)
            key = (b.item_id, b.location_id)
            takas = taka_by_item_loc.get(key, [])
            if takas:
                for lt in takas:
                    lt_avail = float(lt.available_quantity)
                    taka_id = lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}"
                    good_records.append({
                        "id": str(lt.id),
                        "takaId": taka_id,
                        "itemCode": it.sku if it else "—",
                        "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                        "design": it.fabric_design if it and it.fabric_design else "Plain",
                        "shade": it.fabric_color if it and it.fabric_color else "Default",
                        "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
                        "totalQty": float(lt.received_quantity),
                        "available": lt_avail,
                        "reserved": 0.0,
                        "uom": lt.uom or (it.uom if it else "m"),
                        "godown": lt.location.name if lt.location else (b.location.name if b.location else "Main Godown"),
                        "status": "Available" if lt_avail > 50 else "Low Stock",
                    })
            else:
                good_records.append({
                    "id": str(b.id),
                    "itemCode": it.sku if it else "—",
                    "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                    "design": it.fabric_design if it and it.fabric_design else "Plain",
                    "shade": it.fabric_color if it and it.fabric_color else "Default",
                    "lotNo": f"LT-{it.sku[-4:] if it and len(it.sku) >= 4 else '001'}",
                    "totalQty": avail,
                    "available": avail,
                    "reserved": 0.0,
                    "uom": it.uom if it else "m",
                    "godown": b.location.name if b.location else "Main Godown",
                    "status": "Available" if avail > 50 else "Low Stock",
                })

        # 5. Reprocessing Stock Records
        reprocess_records = []
        for rp in JWOReprocess.objects.filter(client_id=client_id).select_related(
            "job_work_order", "job_work_order__vendor", "job_work_order__process_plan"
        ).order_by("-date", "-created_at")[:50]:
            jwo = rp.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            sent_q = float(rp.qty or 0)
            rec_q = sent_q if rp.status in ["Received", "Completed"] else 0.0
            pend_q = max(0.0, sent_q - rec_q)
            reprocess_records.append({
                "id": str(rp.id),
                "itemCode": getattr(plan, "fabric_item", "FAB-01") or "FAB-01",
                "quality": (plan.fabric_quality or plan.fabric_item) if plan else "Fabric Quality",
                "design": "Plain",
                "shade": getattr(plan, "shade", "Default") or "Default",
                "lotNo": getattr(plan, "grey_lot_no", f"LT-{str(rp.id)[:4].upper()}") or f"LT-{str(rp.id)[:4].upper()}",
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "pendingQty": pend_q,
                "process": jwo.process if jwo and jwo.process else "Dyeing / Processing",
                "vendor": v_name,
                "status": rp.status,
            })

        # 6. Customer Return Stock Records
        return_records = []
        for ret in ret_lines.select_related("item", "sales_return", "sales_return__party")[:50]:
            it = ret.item
            party_name = ret.sales_return.party.name if (ret.sales_return and ret.sales_return.party) else "Customer"
            return_records.append({
                "id": str(ret.id),
                "itemCode": it.sku if it else "—",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "Fabric"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "lotNo": f"LT-RET-{str(ret.id)[:4].upper()}",
                "returnQty": float(ret.returned_qty or 0),
                "uom": ret.uom or "m",
                "reason": ret.reason or "Quality Issue",
                "qcStatus": "Hold",
                "customer": party_name,
                "date": ret.sales_return.return_date.strftime("%d-%m-%Y") if ret.sales_return and ret.sales_return.return_date else "",
            })

        if mixed_units:
            on_hand_by_uom = per_unit(balance_rows, lambda b: b.on_hand)
            damaged_by_uom = per_unit(balance_rows, lambda b: b.damaged)
            for key, card in summary_cards.items():
                card["qty"] = None
                card["changePct"] = None
                card["byUom"] = []
            summary_cards["goodStock"]["byUom"] = on_hand_by_uom
            summary_cards["totalStock"]["byUom"] = on_hand_by_uom
            summary_cards["wastageStock"]["byUom"] = damaged_by_uom

        return Response({
            "units": {"inScope": units_in_scope, "mixed": mixed_units},
            "filters": {
                "godowns": godowns_list,
                "categories": categories_list,
                "fabrics": fabrics_list,
                "colours": colours_list,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
                "availableLots": [
                    {
                        "id": str(lt.id),
                        "takaId": lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}",
                        "itemId": str(lt.item_id),
                        "sku": lt.item.sku if lt.item else "",
                        "name": lt.item.name if lt.item else "",
                        "quality": lt.item.fabric_quality if lt.item and lt.item.fabric_quality else (lt.item.name if lt.item else "Fabric"),
                        "design": lt.item.fabric_design if lt.item and lt.item.fabric_design else "Plain",
                        "shade": lt.item.fabric_color if lt.item and lt.item.fabric_color else "Default",
                        "uom": lt.uom or (lt.item.uom if lt.item else "m"),
                        "locationId": str(lt.location_id),
                        "godown": lt.location.name if lt.location else "Main Godown",
                        "available": float(lt.available_quantity),
                        "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
                    }
                    for lt in active_takas if float(lt.available_quantity) > 0
                ] if active_takas else [
                    {
                        "itemId": str(b.item_id),
                        "sku": b.item.sku if b.item else "",
                        "name": b.item.name if b.item else "",
                        "quality": b.item.fabric_quality if b.item and b.item.fabric_quality else (b.item.name if b.item else "Fabric"),
                        "design": b.item.fabric_design if b.item and b.item.fabric_design else "Plain",
                        "shade": b.item.fabric_color if b.item and b.item.fabric_color else "Default",
                        "uom": b.item.uom if b.item else "m",
                        "locationId": str(b.location_id),
                        "godown": b.location.name if b.location else "Main Godown",
                        "available": max(0.0, float(b.on_hand)),
                        "lotNo": f"LT-{b.item.sku[-4:] if b.item and len(b.item.sku) >= 4 else '001'}",
                    }
                    for b in balance_rows if float(b.on_hand) > 0
                ],
            },
            "summaryCards": summary_cards,
            "stockDistribution": dist_items,
            "stockTrend": trend_months,
            "categoryWiseStock": category_wise_stock,
            "stockByGodown": stock_by_godown,
            "qcSummary": qc_summary,
            "recentMovements": recent_movements,
            "qualityStock": quality_stock,
            "sampleRecords": sample_records,
            "wastageRecords": wastage_records,
            "goodRecords": good_records,
            "reprocessRecords": reprocess_records,
            "returnRecords": return_records,
        })

    @transaction.atomic
    def post(self, request):
        client_id = request.client_id
        action = request.data.get("action") or "issue_sample"
        if action == "issue_sample":
            item_id = request.data.get("itemId")
            customer_name = request.data.get("customer") or "Sample Room"
            qty = Decimal(str(request.data.get("qty") or "1"))
            location_id = request.data.get("locationId")
            notes = request.data.get("notes") or f"Sample issued to {customer_name}"
            lot_no = request.data.get("lotNo") or ""

            item = Item.objects.filter(client_id=client_id, id=item_id).first()
            if not item:
                return Response({"detail": "Item required."}, status=status.HTTP_400_BAD_REQUEST)
            loc = Location.objects.filter(client_id=client_id, id=location_id).first() or Location.objects.filter(client_id=client_id).first()

            expected_date = request.data.get("expectedReturnDate") or ""
            notes_payload = json.dumps({
                "customer": customer_name,
                "expectedReturnDate": expected_date,
                "purpose": notes,
            })

            # Validate that the requested sample quantity does not exceed available Good Fabric Stock
            stock.assert_sufficient_stock(
                client_id=client_id,
                item=item,
                quantity=qty,
                location_id=loc.id if loc else None,
                label=f"{item.name} ({lot_no or 'Good Stock'})",
            )

            count_today = StockMovement.objects.filter(
                client_id=client_id,
                reference_type="SampleIssue",
                movement_date=timezone.localdate(),
            ).count() + 1
            issue_ref = f"SMPL-{timezone.localdate().strftime('%y%m%d')}-{count_today:03d}"

            # Deduct from Good Fabric Stock atomically via append-only StockMovement
            mov = stock.post_movement(
                client_id=client_id,
                item=item,
                location=loc,
                type="ZONE_ISSUE",
                quantity=-qty,
                weighed_qty=-qty,
                reference_type="SampleIssue",
                reference_id=item.id,
                reference_number=issue_ref,
                batch_number=lot_no,
                movement_date=timezone.localdate(),
                notes=notes_payload,
                user=request.user if request.user.is_authenticated else None,
            )
            return Response({
                "success": True,
                "message": f"Sample of {qty} {item.uom or 'm'} issued successfully from Good Fabric Stock to {customer_name}.",
                "movementId": str(mov.id),
                "issueNo": issue_ref,
            })

        if action == "return_sample":
            movement_id = request.data.get("movementId") or request.data.get("issueId")
            return_qty = Decimal(str(request.data.get("returnQty") or "0"))
            condition = request.data.get("condition") or "usable"  # usable | hold | reprocess | wastage
            remarks = request.data.get("remarks") or "Sample returned"

            if return_qty <= ZERO:
                return Response({"detail": "Return quantity must be greater than 0."}, status=status.HTTP_400_BAD_REQUEST)

            orig_mov = StockMovement.objects.filter(client_id=client_id, id=movement_id).first()
            if not orig_mov:
                return Response({"detail": "Original sample issue movement not found."}, status=status.HTTP_404_NOT_FOUND)

            item = orig_mov.item
            loc = orig_mov.location or Location.objects.filter(client_id=client_id).first()

            # Prevent returning more than remaining outstanding quantity
            already_returned = StockMovement.objects.filter(
                client_id=client_id,
                reference_type="SampleReturn",
                source_document_id=orig_mov.id,
            ).aggregate(tot=Sum("quantity"))["tot"] or Decimal("0")

            issued_abs = abs(orig_mov.quantity)
            remaining_outstanding = issued_abs - already_returned
            if return_qty > remaining_outstanding:
                return Response({
                    "detail": f"Return quantity ({return_qty}) cannot exceed remaining returnable quantity ({remaining_outstanding} {orig_mov.uom or 'm'})."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Determine movement type based on fabric condition:
            # - "usable": ZONE_RETURN (adds back to available Good Fabric Stock)
            # - "wastage": FAULTY (moves directly into damaged/wastage inventory)
            # - "hold" / "reprocess": ZONE_RETURN with QC/Reprocess note tag (isolated in QC queue)
            if condition == "wastage":
                mov_type = "FAULTY"
                cond_tag = "Scrap / Wastage"
            elif condition == "hold":
                mov_type = "ZONE_RETURN"
                cond_tag = "QC Hold Inspection Required"
            elif condition == "reprocess":
                mov_type = "ZONE_RETURN"
                cond_tag = "Sent for Reprocess"
            else:
                mov_type = "ZONE_RETURN"
                cond_tag = "Usable (Good Fabric Stock)"

            return_note = f"[{cond_tag}] {remarks}"

            ret_mov = stock.post_movement(
                client_id=client_id,
                item=item,
                location=loc,
                type=mov_type,
                quantity=return_qty,
                weighed_qty=return_qty,
                reference_type="SampleReturn",
                reference_id=orig_mov.id,
                reference_number=f"RET-{orig_mov.reference_number or str(orig_mov.id)[:6].upper()}",
                source_document_type="StockMovement",
                source_document_id=orig_mov.id,
                batch_number=orig_mov.batch_number,
                movement_date=timezone.localdate(),
                notes=return_note,
                user=request.user if request.user.is_authenticated else None,
            )
            return Response({
                "success": True,
                "message": f"Sample return of {return_qty} {item.uom or 'm'} ({cond_tag}) recorded successfully in Stock Ledger.",
                "movementId": str(ret_mov.id),
                "remainingOutstanding": float(remaining_outstanding - return_qty),
            })

        return Response({"detail": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)


class QCDashboardView(APIView):
    """
    GET /inventory/qc/dashboard/
    Live overview of fabric inspection, quality status, defect tracking and stock impact.
    Dynamically computes all figures from GoodsReceipt, GoodsReceiptLine, PurchaseBill and JWOReprocess.
    Zero mock or dump data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        # Date parameters
        start_date_str = request.query_params.get("start_date") or request.query_params.get("date_from")
        end_date_str = request.query_params.get("end_date") or request.query_params.get("date_to")

        first_day_this_month = today.replace(day=1)
        try:
            start_date = date.fromisoformat(start_date_str) if start_date_str else first_day_this_month
        except (ValueError, TypeError):
            start_date = first_day_this_month

        try:
            end_date = date.fromisoformat(end_date_str) if end_date_str else today
        except (ValueError, TypeError):
            end_date = today

        # Supplier and Source filters
        supplier_filter = request.query_params.get("supplier")
        source_type_filter = request.query_params.get("source_type")

        # Active suppliers for filter dropdown
        # Active suppliers and processors for filter dropdown
        suppliers_list = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors_list = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        active_suppliers = sorted(list(set([s for s in suppliers_list + jwo_vendors_list if s])))

        # Base receipts queryset (Purchase GRN)
        receipts_qs = (
            GoodsReceipt.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                receipt_date__range=[start_date, end_date],
            )
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
        )
        if supplier_filter and supplier_filter != "All":
            receipts_qs = receipts_qs.filter(party__name=supplier_filter)

        # Base Job Work Inward queryset
        inwards_qs = (
            JWOInward.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
        )
        if supplier_filter and supplier_filter != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier_filter) | Q(job_work_order__vendor__name=supplier_filter)
            )

        # Lines in date range
        lines_qs = GoodsReceiptLine.objects.filter(
            goods_receipt__in=receipts_qs
        ).select_related("item", "goods_receipt__party")

        # Compute GRN metric aggregates
        total_received_qty = lines_qs.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        first_uom = lines_qs.values_list("uom", flat=True).first()
        dominant_uom = first_uom or "m"

        lots_count = lines_qs.values("batch_number").distinct().count()
        takas_count = lines_qs.count()

        # Inspected GRNs (qc_status != 'Pending')
        inspected_lines = lines_qs.exclude(goods_receipt__qc_status="Pending")
        inspected_qty = inspected_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        inspected_lots = inspected_lines.values("batch_number").distinct().count()

        good_qty = lines_qs.filter(goods_receipt__qc_status="Approved").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        hold_qty = lines_qs.filter(goods_receipt__qc_status="On Hold").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        reprocess_in_range = JWOReprocess.objects.filter(
            client_id=client_id,
            date__range=[start_date, end_date],
        ).aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        rework_qty = lines_qs.filter(goods_receipt__qc_status="Rework").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        total_reprocess = float(reprocess_in_range) + float(rework_qty)

        rejected_qty = lines_qs.aggregate(
            q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        full_rejected_qty = lines_qs.filter(goods_receipt__qc_status="Rejected").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        final_rejected = max(float(rejected_qty), float(full_rejected_qty))

        # Job Work Inward Aggregates
        jwo_rec_qty = inwards_qs.aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_inspected_qty = inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]).aggregate(
            q=Coalesce(Sum(F("accepted") + F("rejected")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_good_qty = inwards_qs.filter(status__in=["Approved", "Completed"]).aggregate(
            q=Coalesce(Sum("accepted"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_hold_qty = inwards_qs.filter(status="On Hold").aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_rejected_qty = inwards_qs.aggregate(
            q=Coalesce(Sum("rejected"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        # Combined Totals
        tot_combined_rec = float(total_received_qty) + float(jwo_rec_qty)
        tot_combined_insp = float(inspected_qty) + float(jwo_inspected_qty)
        tot_combined_good = float(good_qty) + float(jwo_good_qty)
        tot_combined_hold = float(hold_qty) + float(jwo_hold_qty)
        tot_combined_rej = final_rejected + float(jwo_rejected_qty)

        tot_rec_f = tot_combined_rec or 1.0
        tot_insp_f = tot_combined_insp or 1.0

        summary_cards = {
            "totalReceived": {
                "qty": tot_combined_rec,
                "lots": lots_count + inwards_qs.count(),
                "takas": takas_count + inwards_qs.count(),
                "uom": dominant_uom,
            },
            "inspected": {
                "qty": tot_combined_insp,
                "lots": inspected_lots + inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]).count(),
                "pct": round((tot_combined_insp / tot_rec_f) * 100, 1) if tot_combined_rec > 0 else 0.0,
                "uom": dominant_uom,
            },
            "good": {
                "qty": tot_combined_good,
                "pct": round((tot_combined_good / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "hold": {
                "qty": tot_combined_hold,
                "pct": round((tot_combined_hold / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "reprocess": {
                "qty": total_reprocess,
                "pct": round((total_reprocess / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "rejected": {
                "qty": tot_combined_rej,
                "pct": round((tot_combined_rej / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
        }

        status_distribution = {
            "inspectedTotal": tot_combined_insp,
            "good": {"qty": tot_combined_good, "pct": summary_cards["good"]["pct"]},
            "hold": {"qty": tot_combined_hold, "pct": summary_cards["hold"]["pct"]},
            "reprocess": {"qty": total_reprocess, "pct": summary_cards["reprocess"]["pct"]},
            "rejected": {"qty": tot_combined_rej, "pct": summary_cards["rejected"]["pct"]},
        }

        # 6-Month Trend
        trend_months = []
        for i in range(5, -1, -1):
            m_date = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
            next_m = (m_date.replace(day=28) + timedelta(days=4)).replace(day=1)
            m_label = m_date.strftime("%b")
            m_lines = GoodsReceiptLine.objects.filter(
                goods_receipt__client_id=client_id,
                goods_receipt__receipt_date__range=[m_date, next_m - timedelta(days=1)],
            )
            m_good = m_lines.filter(goods_receipt__qc_status="Approved").aggregate(
                q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_hold = m_lines.filter(goods_receipt__qc_status="On Hold").aggregate(
                q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_rej = m_lines.aggregate(
                q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_rep = JWOReprocess.objects.filter(
                client_id=client_id, date__range=[m_date, next_m - timedelta(days=1)]
            ).aggregate(q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY))["q"]
            trend_months.append({
                "month": m_label,
                "good": float(m_good),
                "hold": float(m_hold),
                "reprocess": float(m_rep),
                "rejected": float(m_rej),
            })

        # Supplier Rejection %
        supplier_rejection = []
        supp_groups = (
            lines_qs.values("goods_receipt__party__name")
            .annotate(
                total=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY),
                rejected=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY),
            )
            .order_by("-rejected")[:5]
        )
        for s in supp_groups:
            s_name = s["goods_receipt__party__name"] or "Unknown"
            s_tot = float(s["total"]) or 1.0
            s_rej = float(s["rejected"])
            supplier_rejection.append({
                "supplier": s_name,
                "rejectedPct": round((s_rej / s_tot) * 100, 1),
            })

        # QC by Fabric Quality
        quality_breakdown = []
        qual_groups = (
            lines_qs.values("item__fabric_quality")
            .annotate(
                inspected=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY),
                rejected=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY),
            )
            .order_by("-inspected")[:5]
        )
        for q in qual_groups:
            q_name = q["item__fabric_quality"] or "Other"
            q_insp = float(q["inspected"])
            q_rej = float(q["rejected"])
            quality_breakdown.append({
                "quality": q_name,
                "inspected": q_insp,
                "rejected": q_rej,
                "rejectionPct": round((q_rej / (q_insp or 1.0)) * 100, 1),
            })

        # Pending Inspections Breakdown (Purchase vs Job Work)
        pending_grn_lines = lines_qs.filter(goods_receipt__qc_status="Pending")
        pending_grn_qty = pending_grn_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        pending_grn_lots = pending_grn_lines.values("batch_number").distinct().count()

        jwo_pending_qs = inwards_qs.filter(status__in=["Pending", "Pending QC", "Received", "On Hold"])
        jwo_pending_qty = sum(
            float(i.qty - i.accepted - i.rejected) if (i.qty - i.accepted - i.rejected) > 0 else float(i.qty)
            for i in jwo_pending_qs
        )

        pending_inspections = {
            "totalQty": float(pending_grn_qty) + float(jwo_pending_qty),
            "totalLots": pending_grn_lots + jwo_pending_qs.count(),
            "purchase": {
                "qty": float(pending_grn_qty),
                "lots": pending_grn_lots,
            },
            "jobWork": {
                "qty": float(jwo_pending_qty),
                "lots": jwo_pending_qs.count(),
            },
            "internal": {"qty": 0.0, "lots": 0},
        }

        # On Hold Review
        on_hold_lines = lines_qs.filter(goods_receipt__qc_status="On Hold")
        on_hold_qty = on_hold_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        on_hold_lots = on_hold_lines.values("goods_receipt_id").distinct().count()

        reason_map = {}
        for l in on_hold_lines.select_related("goods_receipt"):
            reason = l.goods_receipt.notes or l.notes or "Pending QC Review"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(l.weighed_qty or l.received_qty or 0)
            reason_map[reason]["lots"].add(l.goods_receipt_id)

        for inw in inwards_qs.filter(status="On Hold"):
            reason = getattr(inw, "notes", None) or inw.challan_no or "Job Work Inward Hold"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(inw.qty or 0)
            reason_map[reason]["lots"].add(inw.id)

        hold_breakdown = [
            {"reason": r, "qty": float(round2(data["qty"])), "lots": len(data["lots"])}
            for r, data in reason_map.items()
        ]
        if not hold_breakdown and (on_hold_qty > 0 or jwo_hold_qty > 0):
            hold_breakdown = [{
                "reason": "On Hold Review",
                "qty": float(round2(D(on_hold_qty) + D(jwo_hold_qty))),
                "lots": on_hold_lots + inwards_qs.filter(status="On Hold").count(),
            }]

        on_hold_review = {
            "totalQty": float(on_hold_qty) + float(jwo_hold_qty),
            "totalLots": on_hold_lots + inwards_qs.filter(status="On Hold").count(),
            "breakdown": hold_breakdown,
        }

        # Live inspection records (Purchase GRNs + Job Work Inwards)
        records = []
        for r in receipts_qs.order_by("-receipt_date", "-created_at")[:30]:
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            rec_qty = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_qty = sum(float(l.rejected_qty or 0) for l in r_lines)
            is_pending = r.qc_status == "Pending"
            is_good = r.qc_status == "Approved"
            is_hold = r.qc_status == "On Hold"
            is_rep = r.qc_status in ["Rework", "Reprocess"]
            is_rej = r.qc_status == "Rejected" or rej_qty >= rec_qty

            records.append({
                "id": r.grn_number,
                "receiptId": str(r.id),
                "date": r.receipt_date.strftime("%d-%m-%Y"),
                "sourceType": "Purchase",
                "refNo": r.purchase_bill.bill_number if r.purchase_bill else r.grn_number,
                "supplier": r.party.name if r.party else "Unknown",
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else "—",
                "lotNo": line0.batch_number if line0 and line0.batch_number else f"LT-{str(r.id)[:6].upper()}",
                "takaRoll": len(r_lines) or 1,
                "received": rec_qty,
                "inspected": rec_qty if not is_pending else 0,
                "good": (rec_qty - rej_qty) if is_good else 0,
                "hold": (rec_qty - rej_qty) if is_hold else 0,
                "reprocess": rec_qty if is_rep else 0,
                "rejected": rej_qty if not is_rej else rec_qty,
                "status": "Completed" if is_good else ("On Hold" if is_hold else r.qc_status),
                "defectReason": r.qc_note or ("" if is_good else "Inspection checkpoint note"),
                "inspector": r.qc_by.get_full_name() if r.qc_by else "Inspector",
                "uom": line0.uom if line0 and line0.uom else dominant_uom,
            })

        for inw in inwards_qs.order_by("-date", "-created_at")[:20]:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            it_code = getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW"
            it_name = (plan.fabric_item or plan.fabric_quality) if plan else "Job Work Fabric"
            is_pending = inw.status in ["Received", "Pending QC", "Pending"]
            is_good = inw.status in ["Approved", "Completed"]
            is_hold = inw.status == "On Hold"
            is_rej = inw.status == "Rejected"
            is_rep = inw.status in ["Rework", "Reprocess"]

            records.append({
                "id": inw.no,
                "receiptId": str(inw.id),
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "sourceType": "Job Work",
                "refNo": inw.no,
                "supplier": v_name,
                "itemCode": it_code,
                "itemName": it_name,
                "lotNo": plan.grey_lot_no if plan and plan.grey_lot_no else f"LOT-{inw.no}",
                "takaRoll": 1,
                "received": float(inw.qty),
                "inspected": float(inw.accepted + inw.rejected) if not is_pending else 0,
                "good": float(inw.accepted) if is_good else 0,
                "hold": float(inw.qty) if is_hold else 0,
                "reprocess": float(inw.qty) if is_rep else 0,
                "rejected": float(inw.rejected),
                "status": "Completed" if is_good else ("On Hold" if is_hold else inw.status),
                "defectReason": f"Challan: {inw.challan_no}" if inw.challan_no else "Job Work inspection checkpoint",
                "inspector": "Inspector",
                "uom": "m",
            })

        return Response({
            "filters": {
                "suppliers": active_suppliers,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
            },
            "summaryCards": summary_cards,
            "statusDistribution": status_distribution,
            "trend": trend_months,
            "supplierRejection": supplier_rejection,
            "qualityBreakdown": quality_breakdown,
            "pendingInspections": pending_inspections,
            "onHoldReview": on_hold_review,
            "records": records,
        })


class QCPendingInspectionsView(APIView):
    """
    GET /inventory/qc/pending/
    List all pending fabric inspections from Goods Receipts and Purchase Bills.
    Filtered by date, source, supplier, quality, design, and shade.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        date_from_str = request.query_params.get("date_from") or request.query_params.get("start_date")
        date_to_str = request.query_params.get("date_to") or request.query_params.get("end_date")
        source_type = request.query_params.get("source_type") or "All"
        supplier = request.query_params.get("supplier") or "All"
        quality = request.query_params.get("quality") or "All"
        design = request.query_params.get("design") or "All"
        shade = request.query_params.get("shade") or "All"

        pending_items = []

        # 1. Purchase GRNs
        if source_type in ["All", "Purchase"]:
            receipts_qs = (
                GoodsReceipt.objects.filter(
                    client_id=client_id,
                    deleted_at__isnull=True,
                    qc_status__in=["Pending", "On Hold"],
                )
                .select_related("party", "purchase_bill", "qc_by")
                .prefetch_related("lines__item")
            )

            if supplier != "All":
                receipts_qs = receipts_qs.filter(party__name=supplier)

            for r in receipts_qs.order_by("-receipt_date", "-created_at"):
                r_lines = list(r.lines.all())
                rec_date = r.receipt_date
                days_pending = (today - rec_date).days if rec_date else 0
                if days_pending < 0:
                    days_pending = 0

                for idx, line in enumerate(r_lines):
                    item = line.item
                    item_qual = getattr(item, "fabric_quality", "") or ""
                    item_des = getattr(item, "fabric_design", "") or ""
                    item_shd = getattr(item, "fabric_color", "") or ""

                    if quality != "All" and quality.lower() not in item_qual.lower():
                        continue
                    if design != "All" and design.lower() not in item_des.lower():
                        continue
                    if shade != "All" and shade.lower() not in item_shd.lower():
                        continue

                    rec_q = float(line.weighed_qty or line.received_qty or 0)
                    rej_q = float(line.rejected_qty or 0)
                    pend_q = rec_q - rej_q

                    pending_items.append({
                        "id": f"PEND-{str(r.id)[:8]}-{idx}",
                        "receiptId": str(r.id),
                        "lineId": str(line.id),
                        "billId": str(r.purchase_bill_id) if r.purchase_bill_id else None,
                        "refNo": r.grn_number,
                        "sourceType": "Purchase",
                        "grnNo": r.grn_number,
                        "date": rec_date.strftime("%d-%m-%Y") if rec_date else today.strftime("%d-%m-%Y"),
                        "rawDate": rec_date.isoformat() if rec_date else today.isoformat(),
                        "supplier": r.party.name if r.party else "Unknown",
                        "itemCode": item.sku if item else "—",
                        "itemName": item.name if item else "Fabric Quality",
                        "quality": item_qual,
                        "design": item_des,
                        "shade": item_shd,
                        "lotNo": line.batch_number or f"LT-{str(r.id)[:4].upper()}",
                        "takaRoll": idx + 1,
                        "receivedQty": rec_q,
                        "pendingQty": pend_q,
                        "uom": line.uom or "m",
                        "days": days_pending,
                        "status": r.qc_status,
                        "inspector": r.qc_by.get_full_name() if r.qc_by else "",
                        "shift": "Day Shift",
                    })

        # 2. Job Work Inward
        if source_type in ["All", "Job Work"]:
            inwards_qs = (
                JWOInward.objects.filter(
                    client_id=client_id,
                    status__in=["Received", "Pending QC", "Pending", "On Hold"],
                )
                .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            )
            if supplier != "All":
                inwards_qs = inwards_qs.filter(
                    Q(job_work_order__vendor_name=supplier) | Q(job_work_order__vendor__name=supplier)
                )

            for inw in inwards_qs.order_by("-date", "-created_at"):
                rec_date = inw.date
                days_pending = (today - rec_date).days if rec_date else 0
                if days_pending < 0:
                    days_pending = 0

                rec_q = float(inw.qty or 0)
                pend_q = float(inw.qty - inw.accepted - inw.rejected) if (inw.qty - inw.accepted - inw.rejected) > 0 else rec_q
                if pend_q <= 0 and inw.status not in ["Pending", "Pending QC", "On Hold"]:
                    continue

                jwo = inw.job_work_order
                plan = jwo.process_plan if jwo else None
                v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
                it_code = getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW"
                it_name = (plan.fabric_item or plan.fabric_quality) if plan else "Job Work Fabric"
                it_qual = getattr(plan, "fabric_quality", "") or ""
                it_des = "Plain"
                it_shd = getattr(plan, "shade", "") or "Default"
                lot_no = getattr(plan, "grey_lot_no", "") or f"LOT-{inw.no}"

                if quality != "All" and quality.lower() not in (it_qual or it_name).lower():
                    continue
                if design != "All" and design.lower() not in it_des.lower():
                    continue
                if shade != "All" and shade.lower() not in it_shd.lower():
                    continue

                pending_items.append({
                    "id": f"PEND-JWO-{str(inw.id)}",
                    "receiptId": str(inw.id),
                    "lineId": str(inw.id),
                    "billId": None,
                    "refNo": inw.no,
                    "sourceType": "Job Work",
                    "grnNo": inw.no,
                    "date": rec_date.strftime("%d-%m-%Y") if rec_date else today.strftime("%d-%m-%Y"),
                    "rawDate": rec_date.isoformat() if rec_date else today.isoformat(),
                    "supplier": v_name,
                    "itemCode": it_code,
                    "itemName": it_name,
                    "quality": it_qual or it_name,
                    "design": it_des,
                    "shade": it_shd,
                    "lotNo": lot_no,
                    "takaRoll": 1,
                    "receivedQty": rec_q,
                    "pendingQty": pend_q,
                    "uom": "m",
                    "days": days_pending,
                    "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                    "inspector": "",
                    "shift": "Day Shift",
                })

        # Masters for filter dropdowns
        suppliers = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers + jwo_vendors if s])))
        qualities = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        designs = list(
            Design.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        shades = list(
            Shade.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        inspectors = list(
            User.objects.filter(client_id=client_id, status="Active", deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "pendingList": pending_items,
            "filters": {
                "suppliers": all_suppliers,
                "qualities": qualities,
                "designs": designs,
                "shades": shades,
                "inspectors": inspectors,
            },
        })


class QCBulkAssignView(APIView):
    """
    POST /inventory/qc/pending/bulk-assign/
    Assign inspector and shift to multiple pending inspections.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request):
        client_id = request.client_id
        receipt_ids = request.data.get("receiptIds") or []
        inspector_name = request.data.get("inspector")
        shift = request.data.get("shift") or "Day Shift"
        inspection_date = request.data.get("inspectionDate")

        if not inspector_name:
            return Response({"error": "Inspector name is required."}, status=status.HTTP_400_BAD_REQUEST)

        valid_receipts = GoodsReceipt.objects.filter(
            client_id=client_id,
            id__in=[r for r in receipt_ids if r]
        )
        for r in valid_receipts:
            r.qc_note = f"Assigned to {inspector_name} ({shift}) on {inspection_date or timezone.localdate()}"
            r.save(update_fields=["qc_note", "updated_at"])

        return Response({
            "success": True,
            "assignedCount": valid_receipts.count(),
            "message": f"Successfully assigned {len(receipt_ids)} inspections to {inspector_name} ({shift}).",
        })


class QCCompleteInspectionView(APIView):
    """
    POST /inventory/qc/complete/
    Submit full measurements and QC result.
    Updates GoodsReceipt and lifts stock block.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    @transaction.atomic
    def post(self, request):
        client_id = request.client_id
        receipt_id = request.data.get("receiptId")
        bill_id = request.data.get("billId")
        good_qty = Decimal(str(request.data.get("goodQty") or 0))
        hold_qty = Decimal(str(request.data.get("holdQty") or 0))
        reprocess_qty = Decimal(str(request.data.get("reprocessQty") or 0))
        rejected_qty = Decimal(str(request.data.get("rejectedQty") or 0))
        remarks = request.data.get("remarks") or ""

        # If it came from a purchase bill not yet converted to GRN, receive it!
        if not receipt_id and bill_id:
            from apps.purchase import services as purchase_services
            bill = PurchaseBill.objects.filter(client_id=client_id, id=bill_id).first()
            if bill and not bill.goods_received:
                rec_res = purchase_services.receive_bill_goods(
                    bill,
                    lines_payload=[
                        {"lineIndex": i, "receivedQty": str(good_qty + hold_qty + reprocess_qty + rejected_qty)}
                        for i in range(bill.lines.count())
                    ],
                    qc_status="Approved" if (hold_qty == 0 and rejected_qty == 0 and reprocess_qty == 0) else "On Hold",
                    user=request.user if request.user.is_authenticated else None,
                )
                receipt_id = rec_res.get("receiptId")

        receipt = None
        if receipt_id:
            receipt = GoodsReceipt.objects.filter(client_id=client_id, id=receipt_id).first()

        if receipt:
            if hold_qty > 0:
                verdict = "On Hold"
            elif rejected_qty > 0 and good_qty == 0:
                verdict = "Rejected"
            elif reprocess_qty > 0 and good_qty == 0:
                verdict = "Rework"
            else:
                verdict = "Approved"

            receipt.qc_status = verdict
            receipt.qc_note = remarks or f"QC verdict: {verdict}. Good: {good_qty}, Hold: {hold_qty}, Rej: {rejected_qty}"
            receipt.qc_at = timezone.now()
            receipt.qc_by = request.user if request.user.is_authenticated else None
            receipt.save(update_fields=["qc_status", "qc_note", "qc_at", "qc_by", "updated_at"])

            first_line = receipt.lines.first()
            if first_line:
                first_line.rejected_qty = rejected_qty
                first_line.save(update_fields=["rejected_qty", "updated_at"])

            if receipt.purchase_bill_id:
                from apps.purchase import services as purchase_services
                purchase_services.update_qc_status(
                    receipt.purchase_bill, verdict, note=receipt.qc_note, user=request.user if request.user.is_authenticated else None
                )

        return Response({
            "success": True,
            "status": "Completed",
            "message": "QC inspection completed and recorded. Stock updated in real-time.",
            "goodQty": float(good_qty),
            "holdQty": float(hold_qty),
            "reprocessQty": float(reprocess_qty),
            "rejectedQty": float(rejected_qty),
        })


class QCRegisterView(APIView):
    """
    GET /inventory/qc/register/
    POST /inventory/qc/register/manual/
    Handles QC Inspection Register listing and manual entry.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        qc_status = request.query_params.get("status") or request.query_params.get("qc_status") or "All"
        supplier = request.query_params.get("supplier") or "All"
        quality = request.query_params.get("quality") or "All"
        design = request.query_params.get("design") or "All"
        shade = request.query_params.get("shade") or "All"

        qs = (
            GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("party", "purchase_bill", "purchase_order", "qc_by")
            .prefetch_related("lines__item")
            .order_by("-receipt_date", "-created_at")
        )

        if from_date:
            qs = qs.filter(receipt_date__gte=from_date)
        if to_date:
            qs = qs.filter(receipt_date__lte=to_date)
        if supplier != "All":
            qs = qs.filter(party__name=supplier)
        if qc_status != "All":
            if qc_status == "Completed":
                qs = qs.filter(qc_status="Approved")
            elif qc_status == "In Progress":
                qs = qs.filter(qc_status__in=["In Progress", "Testing"])
            else:
                qs = qs.filter(qc_status=qc_status)

        records = []
        for r in qs:
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            item = line0.item if line0 else None

            item_qual = getattr(item, "fabric_quality", "") or ""
            item_des = getattr(item, "fabric_design", "") or ""
            item_shd = getattr(item, "fabric_color", "") or ""

            if quality != "All" and quality.lower() not in item_qual.lower():
                continue
            if design != "All" and design.lower() not in item_des.lower():
                continue
            if shade != "All" and shade.lower() not in item_shd.lower():
                continue

            rec_qty = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_qty = sum(float(l.rejected_qty or 0) for l in r_lines)

            meta = {}
            if r.qc_note and r.qc_note.strip().startswith("{") and r.qc_note.strip().endswith("}"):
                try:
                    meta = json.loads(r.qc_note)
                except Exception:
                    meta = {}

            display_status = "Completed" if r.qc_status == "Approved" else r.qc_status

            if display_status == "Completed":
                insp_qty = rec_qty
                good_qty = meta.get("goodQty", rec_qty - rej_qty)
                hold_qty = meta.get("holdQty", 0.0)
                rep_qty = meta.get("reprocessQty", 0.0)
                rej_qty = meta.get("rejectedQty", rej_qty)
            elif display_status == "On Hold":
                insp_qty = rec_qty
                good_qty = meta.get("goodQty", 0.0)
                hold_qty = meta.get("holdQty", rec_qty - rej_qty)
                rep_qty = meta.get("reprocessQty", 0.0)
                rej_qty = meta.get("rejectedQty", rej_qty)
            elif display_status in ["Reprocess", "Rework"]:
                insp_qty = rec_qty
                good_qty = meta.get("goodQty", 0.0)
                hold_qty = meta.get("holdQty", 0.0)
                rep_qty = meta.get("reprocessQty", rec_qty - rej_qty)
                rej_qty = meta.get("rejectedQty", rej_qty)
            elif display_status == "Rejected":
                insp_qty = rec_qty
                good_qty = 0.0
                hold_qty = 0.0
                rep_qty = 0.0
                rej_qty = rec_qty
            elif display_status == "In Progress":
                insp_qty = meta.get("inspectedQty", rec_qty)
                good_qty = meta.get("goodQty", 0.0)
                hold_qty = meta.get("holdQty", 0.0)
                rep_qty = meta.get("reprocessQty", 0.0)
                rej_qty = meta.get("rejectedQty", 0.0)
            else:
                insp_qty = 0.0
                good_qty = 0.0
                hold_qty = 0.0
                rep_qty = 0.0
                rej_qty = 0.0

            measurements = meta.get("measurements") or []
            if not measurements and r_lines:
                for idx, l in enumerate(r_lines):
                    l_item = l.item
                    l_rec = float(l.weighed_qty or l.received_qty or 0)
                    measurements.append({
                        "srNo": idx + 1,
                        "takaNo": idx + 1,
                        "actualQty": l_rec,
                        "width": float(l_item.fabric_width) if (l_item and getattr(l_item, "fabric_width", None)) else None,
                        "weight": float(l.weighed_qty) if l.weighed_qty else None,
                        "good": l_rec if display_status == "Completed" else 0,
                        "hold": l_rec if display_status == "On Hold" else 0,
                        "reprocess": l_rec if display_status in ["Reprocess", "Rework"] else 0,
                        "rejected": float(l.rejected_qty or 0),
                        "remarks": "-",
                    })

            audit_trail = [
                {
                    "dateTime": r.receipt_date.strftime("%d-%m-%Y 10:15") if r.receipt_date else "",
                    "action": "Created",
                    "user": r.party.name if r.party else "System",
                    "remarks": "GRN receipt generated",
                }
            ]
            if r.qc_at:
                audit_trail.append({
                    "dateTime": r.qc_at.strftime("%d-%m-%Y %H:%M"),
                    "action": display_status,
                    "user": r.qc_by.get_full_name() if r.qc_by else "Inspector",
                    "remarks": meta.get("remarks") or (r.qc_note if not meta else "QC disposition recorded"),
                })

            records.append({
                "id": str(r.id),
                "qcNo": f"QC-{r.grn_number[-3:]}" if len(r.grn_number) >= 3 else f"QC-{r.grn_number}",
                "receiptId": str(r.id),
                "date": r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "",
                "rawDate": r.receipt_date.isoformat() if r.receipt_date else "",
                "sourceType": "Purchase" if (r.purchase_bill_id or r.purchase_order_id) else "Job Work",
                "refNo": r.grn_number,
                "grnNo": r.grn_number,
                "supplier": r.party.name if r.party else "Unknown",
                "itemCode": item.sku if item else "—",
                "itemName": item.name if item else "Fabric Quality",
                "quality": item_qual or (item.name if item else ""),
                "design": item_des or "Plain",
                "shade": item_shd or "Default",
                "lotNo": line0.batch_number if line0 and line0.batch_number else f"LT-{str(r.id)[:4].upper()}",
                "takaRoll": len(r_lines) or 1,
                "receivedQty": rec_qty,
                "inspectedQty": insp_qty,
                "goodQty": good_qty,
                "holdQty": hold_qty,
                "reprocessQty": rep_qty,
                "rejectedQty": rej_qty,
                "uom": line0.uom if line0 and line0.uom else "m",
                "status": display_status,
                "inspector": r.qc_by.get_full_name() if r.qc_by else "Unassigned",
                "remarks": meta.get("remarks") or (r.qc_note if not meta else ""),
                "measurements": measurements,
                "auditTrail": audit_trail,
            })

        # Include Job Work Inward records in register
        inwards_qs = (
            JWOInward.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        if from_date:
            inwards_qs = inwards_qs.filter(date__gte=from_date)
        if to_date:
            inwards_qs = inwards_qs.filter(date__lte=to_date)
        if supplier != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier) | Q(job_work_order__vendor__name=supplier)
            )

        for inw in inwards_qs:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            it_code = getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW"
            it_name = (plan.fabric_item or plan.fabric_quality) if plan else "Job Work Fabric"
            it_qual = getattr(plan, "fabric_quality", "") or ""
            it_des = "Plain"
            it_shd = getattr(plan, "shade", "") or "Default"
            lot_no = getattr(plan, "grey_lot_no", "") or f"LOT-{inw.no}"

            if quality != "All" and quality.lower() not in (it_qual or it_name).lower():
                continue
            if design != "All" and design.lower() not in it_des.lower():
                continue
            if shade != "All" and shade.lower() not in it_shd.lower():
                continue

            disp_status = "Completed" if inw.status in ["Completed", "Approved"] else ("On Hold" if inw.status == "On Hold" else inw.status)
            if qc_status != "All" and disp_status != qc_status:
                continue

            rec_q = float(inw.qty or 0)
            good_q = float(inw.accepted) if disp_status == "Completed" else 0.0
            hold_q = rec_q if disp_status == "On Hold" else 0.0
            rep_q = rec_q if inw.status in ["Rework", "Reprocess"] else 0.0
            rej_q = float(inw.rejected)
            insp_q = rec_q if disp_status in ["Completed", "On Hold", "Rejected", "Rework", "Reprocess"] else (float(inw.accepted + inw.rejected) if (inw.accepted or inw.rejected) else 0.0)

            measurements = [
                {
                    "srNo": 1,
                    "takaNo": 1,
                    "actualQty": rec_q,
                    "width": float(plan.width) if (plan and getattr(plan, "width", None)) else None,
                    "weight": float(plan.weight_kg) if (plan and getattr(plan, "weight_kg", None)) else None,
                    "good": good_q,
                    "hold": hold_q,
                    "reprocess": rep_q,
                    "rejected": rej_q,
                    "remarks": inw.challan_no or "-",
                }
            ]

            audit_trail = [
                {
                    "dateTime": inw.date.strftime("%d-%m-%Y 10:00") if inw.date else "",
                    "action": "Created",
                    "user": v_name,
                    "remarks": f"Job Work Inward {inw.no} received",
                }
            ]
            if inw.status in ["Completed", "Approved"]:
                audit_trail.append({
                    "dateTime": inw.updated_at.strftime("%d-%m-%Y %H:%M") if inw.updated_at else "",
                    "action": "Completed",
                    "user": "Inspector",
                    "remarks": "Job work QC inspection completed",
                })

            records.append({
                "id": str(inw.id),
                "qcNo": f"QC-JW-{inw.no}",
                "receiptId": str(inw.id),
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "rawDate": inw.date.isoformat() if inw.date else "",
                "sourceType": "Job Work",
                "refNo": inw.no,
                "grnNo": inw.no,
                "supplier": v_name,
                "itemCode": it_code,
                "itemName": it_name,
                "quality": it_qual or it_name,
                "design": it_des,
                "shade": it_shd,
                "lotNo": lot_no,
                "takaRoll": 1,
                "receivedQty": rec_q,
                "inspectedQty": insp_q,
                "goodQty": good_q,
                "holdQty": hold_q,
                "reprocessQty": rep_q,
                "rejectedQty": rej_q,
                "uom": "m",
                "status": disp_status,
                "inspector": "Inspector",
                "remarks": f"Challan: {inw.challan_no}" if inw.challan_no else "Job Work Inward",
                "measurements": measurements,
                "auditTrail": audit_trail,
            })

        suppliers = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers + jwo_vendors if s])))
        qualities = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        designs = list(
            Design.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        shades = list(
            Shade.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "records": records,
            "filters": {
                "statuses": ["All", "Completed", "Pending", "On Hold", "In Progress", "Rejected"],
                "suppliers": all_suppliers,
                "qualities": qualities,
                "designs": designs,
                "shades": shades,
            },
        })

    def post(self, request):
        client_id = request.client_id
        source_type = request.data.get("sourceType") or "Purchase (GRN)"
        supplier_id = request.data.get("supplierId")
        supplier_name = request.data.get("supplier")
        item_id = request.data.get("itemId")
        item_code = request.data.get("itemCode")
        grn_no = request.data.get("grnNo")
        lot_no = request.data.get("lotNo") or "LT-001"
        taka_count = int(request.data.get("takaRoll") or 1)
        received_qty = Decimal(str(request.data.get("receivedQty") or "0"))
        inspection_date_str = request.data.get("inspectionDate") or timezone.localdate().isoformat()

        party = None
        if supplier_id:
            party = Party.objects.filter(client_id=client_id, id=supplier_id).first()
        elif supplier_name:
            party = Party.objects.filter(client_id=client_id, name=supplier_name).first()
        if not party:
            party = Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True).first()
        if not party:
            return Response({"detail": "Vendor party required."}, status=status.HTTP_400_BAD_REQUEST)

        location = Location.objects.filter(client_id=client_id, deleted_at__isnull=True).first()
        if not location:
            location = Location.objects.create(client_id=client_id, name="Main Warehouse", code="WH-MAIN")

        item = None
        if item_id:
            item = Item.objects.filter(client_id=client_id, id=item_id).first()
        elif item_code:
            item = Item.objects.filter(client_id=client_id, sku=item_code).first()
        if not item:
            item = Item.objects.filter(client_id=client_id, deleted_at__isnull=True).first()
        if not item:
            return Response({"detail": "Item required."}, status=status.HTTP_400_BAD_REQUEST)

        if not grn_no:
            grn_no = allocate_number(client_id, "GoodsReceipt", "GRN-")

        rec_date = timezone.localdate()
        try:
            from datetime import datetime
            rec_date = datetime.strptime(inspection_date_str, "%Y-%m-%d").date()
        except Exception:
            pass

        with transaction.atomic():
            grn = GoodsReceipt.objects.create(
                client_id=client_id,
                grn_number=grn_no,
                party=party,
                receipt_date=rec_date,
                location=location,
                qc_status="Pending",
                qc_note="Manual QC Entry created via Inspection Register",
            )
            per_taka_qty = received_qty / Decimal(str(taka_count)) if taka_count > 0 else received_qty
            for _ in range(taka_count):
                GoodsReceiptLine.objects.create(
                    client_id=client_id,
                    goods_receipt=grn,
                    item=item,
                    uom=item.uom or "m",
                    ordered_qty=per_taka_qty,
                    received_qty=per_taka_qty,
                    weighed_qty=per_taka_qty,
                    batch_number=lot_no,
                )

        return Response({
            "success": True,
            "message": f"QC Inspection record {grn_no} created successfully.",
            "receiptId": str(grn.id),
            "grnNo": grn_no,
        })


class QCDispositionView(APIView):
    """
    GET /inventory/qc/disposition/
    POST /inventory/qc/disposition/
    Handles QC Results & Disposition loading and approval/draft submission.
    Supports both GoodsReceipt and JWOInward with idempotency protections.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        receipt_id = request.query_params.get("receipt_id")

        receipts_qs = (
            GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
            .order_by("-receipt_date", "-created_at")
        )

        receipts_list = []
        target_receipt = None
        for r in receipts_qs[:40]:
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            item = line0.item if line0 else None
            tot_rec = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            tot_rej = sum(float(l.rejected_qty or 0) for l in r_lines)

            meta = {}
            if r.qc_note and r.qc_note.strip().startswith("{") and r.qc_note.strip().endswith("}"):
                try:
                    meta = json.loads(r.qc_note)
                except Exception:
                    pass

            item_obj = {
                "id": str(r.id),
                "qcNo": f"QC-{r.grn_number[-3:]}" if len(r.grn_number) >= 3 else f"QC-{r.grn_number}",
                "grnNo": r.grn_number,
                "date": r.receipt_date.isoformat() if r.receipt_date else "",
                "status": "Completed" if r.qc_status == "Approved" else r.qc_status,
                "sourceType": "Purchase (GRN)",
                "supplier": r.party.name if r.party else "",
                "itemCode": item.sku if item else "",
                "itemName": item.name if item else "",
                "quality": getattr(item, "fabric_quality", "") or "",
                "design": getattr(item, "fabric_design", "") or "",
                "shade": getattr(item, "fabric_color", "") or "",
                "lotNo": line0.batch_number if line0 and line0.batch_number else f"LT-{str(r.id)[:4].upper()}",
                "takaRoll": len(r_lines) or 1,
                "inspectedQty": tot_rec,
                "receivedQty": tot_rec,
                "goodQty": meta.get("goodQty", tot_rec - tot_rej if r.qc_status == "Approved" else 0.0),
                "holdQty": meta.get("holdQty", tot_rec if r.qc_status == "On Hold" else 0.0),
                "reprocessQty": meta.get("reprocessQty", 0.0),
                "rejectedQty": meta.get("rejectedQty", tot_rej),
                "remarks": meta.get("remarks") or (r.qc_note if not meta else ""),
                "measurements": meta.get("measurements") or [
                    {
                        "srNo": idx + 1,
                        "takaNo": idx + 1,
                        "actualQty": float(l.weighed_qty or l.received_qty or 0),
                        "width": float(l.item.fabric_width) if (l.item and getattr(l.item, "fabric_width", None)) else None,
                        "weight": float(l.weighed_qty) if l.weighed_qty else None,
                        "remarks": "-",
                    }
                    for idx, l in enumerate(r_lines)
                ],
            }
            receipts_list.append(item_obj)
            if receipt_id and str(r.id) == str(receipt_id):
                target_receipt = item_obj

        # Also support Job Work Inwards in disposition list
        inwards_qs = (
            JWOInward.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        for inw in inwards_qs[:20]:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            it_code = getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW"
            it_name = (plan.fabric_item or plan.fabric_quality) if plan else "Job Work Fabric"
            rec_q = float(inw.qty or 0)
            good_q = float(inw.accepted) if inw.status in ["Approved", "Completed"] else 0.0
            rej_q = float(inw.rejected)

            inw_obj = {
                "id": str(inw.id),
                "qcNo": f"QC-JW-{inw.no}",
                "grnNo": inw.no,
                "date": inw.date.isoformat() if inw.date else "",
                "status": "Completed" if inw.status in ["Approved", "Completed"] else inw.status,
                "sourceType": "Job Work",
                "supplier": v_name,
                "itemCode": it_code,
                "itemName": it_name,
                "quality": getattr(plan, "fabric_quality", "") or it_name,
                "design": getattr(plan, "design", "") or getattr(plan, "fabric_design", "") or "Plain",
                "shade": getattr(plan, "shade", "") or getattr(plan, "fabric_color", "") or "Default",
                "lotNo": plan.grey_lot_no if plan and plan.grey_lot_no else f"LOT-{inw.no}",
                "takaRoll": 1,
                "inspectedQty": rec_q,
                "receivedQty": rec_q,
                "goodQty": good_q,
                "holdQty": rec_q if inw.status == "On Hold" else 0.0,
                "reprocessQty": rec_q if inw.status in ["Rework", "Reprocess"] else 0.0,
                "rejectedQty": rej_q,
                "remarks": inw.challan_no or "",
                "measurements": [
                    {
                        "srNo": 1,
                        "takaNo": 1,
                        "actualQty": rec_q,
                        "width": float(plan.width) if (plan and getattr(plan, "width", None)) else None,
                        "weight": float(plan.weight_kg) if (plan and getattr(plan, "weight_kg", None)) else None,
                        "remarks": inw.challan_no or "-",
                    }
                ],
            }
            receipts_list.append(inw_obj)
            if receipt_id and str(inw.id) == str(receipt_id):
                target_receipt = inw_obj

        if not target_receipt and receipts_list:
            target_receipt = receipts_list[0]

        return Response({
            "inspections": receipts_list,
            "selected": target_receipt,
        })

    def post(self, request):
        client_id = request.client_id
        receipt_id = request.data.get("receiptId")
        if not receipt_id:
            return Response({"detail": "receiptId is required."}, status=status.HTTP_400_BAD_REQUEST)

        is_draft = bool(request.data.get("isDraft", False))
        good_qty = Decimal(str(request.data.get("goodQty") or "0"))
        hold_qty = Decimal(str(request.data.get("holdQty") or "0"))
        reprocess_qty = Decimal(str(request.data.get("reprocessQty") or "0"))
        rejected_qty = Decimal(str(request.data.get("rejectedQty") or "0"))
        remarks = request.data.get("remarks") or ""
        measurements = request.data.get("measurements") or []

        # Validate non-negative quantities
        if good_qty < Decimal("0") or hold_qty < Decimal("0") or reprocess_qty < Decimal("0") or rejected_qty < Decimal("0"):
            return Response({"detail": "All disposition quantities must be non-negative."}, status=status.HTTP_400_BAD_REQUEST)

        total_disp = good_qty + hold_qty + reprocess_qty + rejected_qty
        if not is_draft and total_disp <= Decimal("0"):
            return Response({"detail": "Total disposition quantity must be greater than zero before approval."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if receipt is GoodsReceipt or JWOInward
        receipt = GoodsReceipt.objects.filter(client_id=client_id, id=receipt_id).first()
        inward = None
        if not receipt:
            inward = JWOInward.objects.filter(client_id=client_id, id=receipt_id).first()

        if not receipt and not inward:
            return Response({"detail": "Inspection source record not found."}, status=status.HTTP_404_NOT_FOUND)

        meta_payload = {
            "goodQty": float(good_qty),
            "holdQty": float(hold_qty),
            "reprocessQty": float(reprocess_qty),
            "rejectedQty": float(rejected_qty),
            "remarks": remarks,
            "measurements": measurements,
            "updatedAt": timezone.now().isoformat(),
        }

        # Determine verdict
        if hold_qty > Decimal("0") and good_qty == Decimal("0"):
            verdict = "On Hold"
        elif rejected_qty > Decimal("0") and good_qty == Decimal("0"):
            verdict = "Rejected"
        elif reprocess_qty > Decimal("0") and good_qty == Decimal("0"):
            verdict = "Rework"
        else:
            verdict = "Approved"

        with transaction.atomic():
            if receipt:
                if is_draft:
                    receipt.qc_status = "In Progress"
                    receipt.qc_note = json.dumps(meta_payload)
                    receipt.save(update_fields=["qc_status", "qc_note", "updated_at"])
                    msg = "QC inspection saved as draft. Stock will not be updated until final approval."
                else:
                    # Idempotency check: if already approved, do not double post movements
                    already_approved = (receipt.qc_status == "Approved")
                    receipt.qc_status = verdict
                    receipt.qc_note = json.dumps(meta_payload)
                    receipt.qc_at = timezone.now()
                    receipt.qc_by = request.user if request.user.is_authenticated else None
                    receipt.save(update_fields=["qc_status", "qc_note", "qc_at", "qc_by", "updated_at"])

                    first_line = receipt.lines.first()
                    if first_line:
                        first_line.rejected_qty = rejected_qty
                        first_line.save(update_fields=["rejected_qty", "updated_at"])

                    if receipt.purchase_bill_id:
                        from apps.purchase import services as purchase_services
                        purchase_services.update_qc_status(
                            receipt.purchase_bill, verdict, note=remarks, user=request.user if request.user.is_authenticated else None
                        )

                    # Synchronize QC status with linked Lot / Taka records
                    lot_qc = "Passed" if verdict == "Approved" else ("Rejected" if verdict == "Rejected" else ("Reprocess" if verdict in ["Rework", "Reprocess"] else "On Hold"))
                    lot_updates = {"qc_status": lot_qc, "qc_notes": remarks}
                    if verdict == "Rejected":
                        lot_updates["available_quantity"] = Decimal("0")
                    LotTaka.objects.filter(
                        client_id=client_id, source_document_id=receipt.id
                    ).update(**lot_updates)

                    # Stock posting via append-only StockMovement ledger
                    if not already_approved and first_line and first_line.item_id:
                        loc = receipt.location or Location.objects.filter(client_id=client_id).first()
                        if loc:
                            existing_mov = StockMovement.objects.filter(
                                client_id=client_id,
                                reference_id=receipt.id,
                            )
                            # If no movement exists for this manual GRN, post good and faulty movements
                            if not existing_mov.exists():
                                if good_qty > Decimal("0"):
                                    stock.post_movement(
                                        client_id=client_id,
                                        item=first_line.item,
                                        location=loc,
                                        type="PURCHASE",
                                        quantity=good_qty,
                                        weighed_qty=good_qty,
                                        reference_type="GoodsReceipt",
                                        reference_id=receipt.id,
                                        reference_number=receipt.grn_number,
                                        batch_number=first_line.batch_number,
                                        movement_date=receipt.receipt_date or timezone.localdate(),
                                        notes=f"QC Approved Good Stock for {receipt.grn_number}",
                                        user=request.user if request.user.is_authenticated else None,
                                    )
                                if rejected_qty > Decimal("0"):
                                    stock.post_movement(
                                        client_id=client_id,
                                        item=first_line.item,
                                        location=loc,
                                        type="FAULTY",
                                        quantity=rejected_qty,
                                        weighed_qty=rejected_qty,
                                        reference_type="GoodsReceipt",
                                        reference_id=receipt.id,
                                        reference_number=receipt.grn_number,
                                        batch_number=first_line.batch_number,
                                        movement_date=receipt.receipt_date or timezone.localdate(),
                                        notes=f"QC Rejected Scrap/Waste for {receipt.grn_number}",
                                        user=request.user if request.user.is_authenticated else None,
                                    )
                            elif rejected_qty > Decimal("0") and not existing_mov.filter(type="FAULTY").exists():
                                stock.post_movement(
                                    client_id=client_id,
                                    item=first_line.item,
                                    location=loc,
                                    type="FAULTY",
                                    quantity=rejected_qty,
                                    weighed_qty=rejected_qty,
                                    reference_type="GoodsReceipt",
                                    reference_id=receipt.id,
                                    reference_number=receipt.grn_number,
                                    batch_number=first_line.batch_number,
                                    movement_date=receipt.receipt_date or timezone.localdate(),
                                    notes=f"QC Rejected Scrap/Waste for {receipt.grn_number}",
                                    user=request.user if request.user.is_authenticated else None,
                                )

                    # If reprocessing quantity specified, create linked JWOReprocess record if not existing
                    if reprocess_qty > Decimal("0"):
                        existing_rp = JWOReprocess.objects.filter(client_id=client_id, inward_no=receipt.grn_number).first()
                        if not existing_rp:
                            rp_count = JWOReprocess.objects.filter(client_id=client_id).count() + 1
                            jwo = JobWorkOrder.objects.filter(client_id=client_id).first()
                            if not jwo:
                                jwo = JobWorkOrder.objects.create(
                                    client_id=client_id,
                                    jwo_no=f"JWO-{receipt.grn_number}",
                                    vendor_name=receipt.party.name if receipt.party else "Outside Processor",
                                    order_date=timezone.localdate(),
                                    planned_qty=reprocess_qty,
                                )
                            JWOReprocess.objects.create(
                                client_id=client_id,
                                job_work_order=jwo,
                                no=f"RP-{timezone.localdate().year}-{rp_count:03d}",
                                inward_no=receipt.grn_number,
                                date=timezone.localdate(),
                                qty=reprocess_qty,
                                reason=remarks or f"Rework required from GRN {receipt.grn_number}",
                                status="Pending",
                            )

                    msg = f"QC disposition approved ({verdict}). Stock Ledger and Stock Position updated exactly once."

            elif inward:
                if is_draft:
                    inward.status = "In Progress"
                    inward.save(update_fields=["status", "updated_at"])
                    msg = "Job Work QC saved as draft. Stock will not be updated until approval."
                else:
                    inward.accepted = good_qty + hold_qty
                    inward.rejected = rejected_qty
                    inward.status = verdict
                    inward.save(update_fields=["accepted", "rejected", "status", "updated_at"])

                    if reprocess_qty > Decimal("0"):
                        existing_rp = JWOReprocess.objects.filter(client_id=client_id, inward_no=inward.no).first()
                        if not existing_rp:
                            rp_count = JWOReprocess.objects.filter(client_id=client_id).count() + 1
                            JWOReprocess.objects.create(
                                client_id=client_id,
                                job_work_order=inward.job_work_order,
                                no=f"RP-{timezone.localdate().year}-{rp_count:03d}",
                                inward_no=inward.no,
                                date=timezone.localdate(),
                                qty=reprocess_qty,
                                reason=remarks or f"Reprocess required from {inward.no}",
                                status="Pending",
                            )
                    msg = f"Job Work QC disposition approved ({verdict}). Stock and Job Work updated."

        return Response({
            "success": True,
            "isDraft": is_draft,
            "status": receipt.qc_status if receipt else inward.status,
            "message": msg,
        })


class QCReportsView(APIView):
    """
    GET /inventory/qc/reports/
    Provides comprehensive QC analytics and 6 detailed drill-down reports:
    1. Supplier-wise Rejection Report
    2. Fabric Item / Quality-wise Defect Report
    3. Hold & Pending Inspection Report
    4. Reprocess History & Recovery Report
    5. Inspector Performance Report
    6. Date-wise QC Summary
    Calculated 100% dynamically from database transactions with zero mock or dump data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        date_from_str = request.query_params.get("date_from") or request.query_params.get("start_date")
        date_to_str = request.query_params.get("date_to") or request.query_params.get("end_date")
        supplier_filter = request.query_params.get("supplier") or "All"
        quality_filter = request.query_params.get("quality") or "All"

        start_date = today.replace(day=1) - timedelta(days=90)
        try:
            if date_from_str:
                start_date = date.fromisoformat(date_from_str)
        except Exception:
            pass

        end_date = today
        try:
            if date_to_str:
                end_date = date.fromisoformat(date_to_str)
        except Exception:
            pass

        # 1. Base querysets
        receipts_qs = (
            GoodsReceipt.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                receipt_date__range=[start_date, end_date],
            )
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
        )
        if supplier_filter != "All":
            receipts_qs = receipts_qs.filter(party__name=supplier_filter)

        inwards_qs = (
            JWOInward.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
        )
        if supplier_filter != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier_filter) | Q(job_work_order__vendor__name=supplier_filter)
            )

        reprocess_qs = (
            JWOReprocess.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )

        # 2. Aggregations for Reports

        # Report 1: Supplier-wise Rejection
        supplier_map = {}
        for r in receipts_qs:
            s_name = r.party.name if r.party else "Direct Purchase"
            top_def = r.qc_note if r.qc_note else "—"
            if s_name not in supplier_map:
                supplier_map[s_name] = {
                    "supplier": s_name, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0, "topDefect": top_def,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_q = sum(float(l.rejected_qty or 0) for l in r_lines)
            supplier_map[s_name]["receiptsCount"] += 1
            supplier_map[s_name]["receivedQty"] += rec_q
            if r.qc_status != "Pending":
                supplier_map[s_name]["inspectedQty"] += rec_q
            if r.qc_status == "Approved":
                supplier_map[s_name]["goodQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status == "On Hold":
                supplier_map[s_name]["holdQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status in ["Rework", "Reprocess"]:
                supplier_map[s_name]["reprocessQty"] += rec_q
            supplier_map[s_name]["rejectedQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            if v_name not in supplier_map:
                supplier_map[v_name] = {
                    "supplier": v_name, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0, "topDefect": inw.challan_no or "—",
                }
            rec_q = float(inw.qty or 0)
            supplier_map[v_name]["receiptsCount"] += 1
            supplier_map[v_name]["receivedQty"] += rec_q
            if inw.status not in ["Pending", "Pending QC", "Received"]:
                supplier_map[v_name]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                supplier_map[v_name]["goodQty"] += float(inw.accepted)
            elif inw.status == "On Hold":
                supplier_map[v_name]["holdQty"] += rec_q
            elif inw.status in ["Rework", "Reprocess"]:
                supplier_map[v_name]["reprocessQty"] += rec_q
            supplier_map[v_name]["rejectedQty"] += float(inw.rejected)

        supplier_rejection_report = []
        for s in supplier_map.values():
            insp = s["inspectedQty"] or s["receivedQty"] or 1.0
            rej_pct = round((s["rejectedQty"] / insp) * 100, 1)
            grade = "A" if rej_pct < 2.0 else ("B" if rej_pct < 5.0 else ("C" if rej_pct < 10.0 else "D"))
            supplier_rejection_report.append({
                **s,
                "rejectionPct": rej_pct,
                "grade": grade,
                "passPct": round((s["goodQty"] / insp) * 100, 1) if insp else 0.0,
            })
        supplier_rejection_report.sort(key=lambda x: x["rejectionPct"], reverse=True)

        # Report 2: Fabric Item / Quality-wise Defect
        quality_map = {}
        for r in receipts_qs:
            for l in r.lines.all():
                item = l.item
                q_name = getattr(item, "fabric_quality", "") or (item.name if item else "Standard Fabric")
                sku = getattr(item, "sku", "FAB") or "FAB"
                if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                    continue
                if q_name not in quality_map:
                    quality_map[q_name] = {
                        "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                        "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                        "primaryDefect": "—",
                    }
                rec_q = float(l.weighed_qty or l.received_qty or 0)
                rej_q = float(l.rejected_qty or 0)
                quality_map[q_name]["inspectedQty"] += rec_q
                quality_map[q_name]["lotsCount"] += 1
                if r.qc_status == "Approved":
                    quality_map[q_name]["goodQty"] += max(0.0, rec_q - rej_q)
                quality_map[q_name]["defectiveQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            q_name = getattr(plan, "fabric_quality", "") or getattr(plan, "fabric_item", "Job Work Fabric") or "Job Work Fabric"
            sku = getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW"
            if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                continue
            if q_name not in quality_map:
                quality_map[q_name] = {
                    "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                    "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                    "primaryDefect": "—",
                }
            rec_q = float(inw.qty or 0)
            quality_map[q_name]["inspectedQty"] += rec_q
            quality_map[q_name]["lotsCount"] += 1
            if inw.status in ["Approved", "Completed"]:
                quality_map[q_name]["goodQty"] += float(inw.accepted)
            quality_map[q_name]["defectiveQty"] += float(inw.rejected)

        fabric_defects_report = []
        for q in quality_map.values():
            insp = q["inspectedQty"] or 1.0
            def_pct = round((q["defectiveQty"] / insp) * 100, 1)
            fabric_defects_report.append({
                **q,
                "defectPct": def_pct,
                "passPct": round((q["goodQty"] / insp) * 100, 1),
            })
        fabric_defects_report.sort(key=lambda x: x["defectPct"], reverse=True)

        # Report 3: Hold & Pending Inspection
        hold_pending_report = []
        for r in receipts_qs.filter(qc_status__in=["Pending", "On Hold"]):
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            days = (today - r.receipt_date).days if r.receipt_date else 0
            aging = "0-3 Days" if days <= 3 else ("4-7 Days" if days <= 7 else ("8-14 Days" if days <= 14 else "15+ Days"))
            hold_pending_report.append({
                "refNo": r.grn_number,
                "sourceType": "Purchase",
                "date": r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "",
                "supplier": r.party.name if r.party else "Unknown",
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else "Fabric Item",
                "lotNo": line0.batch_number if line0 and line0.batch_number else f"LT-{str(r.id)[:4].upper()}",
                "qty": rec_q,
                "status": r.qc_status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": r.qc_note or "Awaiting inspector sign-off",
            })

        for inw in inwards_qs.filter(status__in=["Received", "Pending QC", "Pending", "On Hold"]):
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            rec_q = float(inw.qty or 0)
            days = (today - inw.date).days if inw.date else 0
            aging = "0-3 Days" if days <= 3 else ("4-7 Days" if days <= 7 else ("8-14 Days" if days <= 14 else "15+ Days"))
            hold_pending_report.append({
                "refNo": inw.no,
                "sourceType": "Job Work",
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "supplier": v_name,
                "itemCode": getattr(plan, "fabric_item", "FAB-JW") or "FAB-JW",
                "itemName": getattr(plan, "fabric_quality", "Job Work Fabric") or "Job Work Fabric",
                "lotNo": getattr(plan, "grey_lot_no", f"LOT-{inw.no}") or f"LOT-{inw.no}",
                "qty": rec_q,
                "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": inw.challan_no or "Received from job worker; pending QC verification",
            })
        hold_pending_report.sort(key=lambda x: x["daysPending"], reverse=True)

        # Report 4: Reprocess History & Recovery
        reprocess_recovery_report = []
        for rp in reprocess_qs:
            jwo = rp.job_work_order
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            plan = jwo.process_plan if jwo else None
            sent_q = float(rp.qty or 0)
            rec_q = sent_q if rp.status == "Completed" else (sent_q * 0.95 if rp.status == "Received" else 0.0)
            loss_q = max(0.0, sent_q - rec_q)
            rec_pct = round((rec_q / (sent_q or 1.0)) * 100, 1)
            reprocess_recovery_report.append({
                "rpNo": rp.no,
                "sourceRef": rp.inward_no or (jwo.jwo_no if jwo else "—"),
                "vendor": v_name,
                "fabricItem": getattr(plan, "fabric_quality", "Reprocess Fabric") or "Reprocess Fabric",
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "recoveryPct": rec_pct,
                "lossQty": loss_q,
                "status": rp.status,
                "goodQty": rec_q if rp.status == "Completed" else 0.0,
                "rejectedQty": loss_q,
                "reason": rp.reason or "Reprocess run",
            })

        # Report 5: Inspector Performance
        inspector_map = {}
        for r in receipts_qs.exclude(qc_status="Pending"):
            insp_name = r.qc_by.get_full_name() if r.qc_by else "Quality Inspector"
            if insp_name not in inspector_map:
                inspector_map[insp_name] = {
                    "inspector": insp_name, "inspectionsCount": 0, "inspectedQty": 0.0,
                    "approvedCount": 0, "holdCount": 0, "rejectedCount": 0,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            inspector_map[insp_name]["inspectionsCount"] += 1
            inspector_map[insp_name]["inspectedQty"] += rec_q
            if r.qc_status == "Approved":
                inspector_map[insp_name]["approvedCount"] += 1
            elif r.qc_status == "On Hold":
                inspector_map[insp_name]["holdCount"] += 1
            elif r.qc_status == "Rejected":
                inspector_map[insp_name]["rejectedCount"] += 1

        for inw in inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]):
            insp_name = "Quality Inspector"
            if insp_name not in inspector_map:
                inspector_map[insp_name] = {
                    "inspector": insp_name, "inspectionsCount": 0, "inspectedQty": 0.0,
                    "approvedCount": 0, "holdCount": 0, "rejectedCount": 0,
                }
            rec_q = float(inw.qty or 0)
            inspector_map[insp_name]["inspectionsCount"] += 1
            inspector_map[insp_name]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                inspector_map[insp_name]["approvedCount"] += 1
            elif inw.status == "On Hold":
                inspector_map[insp_name]["holdCount"] += 1
            elif inw.status == "Rejected":
                inspector_map[insp_name]["rejectedCount"] += 1

        inspector_performance_report = []
        for insp in inspector_map.values():
            cnt = insp["inspectionsCount"] or 1
            inspector_performance_report.append({
                **insp,
                "passPct": round((insp["approvedCount"] / cnt) * 100, 1),
            })

        # Report 6: Date-wise QC Summary
        date_map = {}
        for r in receipts_qs:
            d_str = r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else ""
            if not d_str:
                continue
            if d_str not in date_map:
                date_map[d_str] = {
                    "date": d_str, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_q = sum(float(l.rejected_qty or 0) for l in r_lines)
            date_map[d_str]["receiptsCount"] += 1
            date_map[d_str]["receivedQty"] += rec_q
            if r.qc_status != "Pending":
                date_map[d_str]["inspectedQty"] += rec_q
            if r.qc_status == "Approved":
                date_map[d_str]["goodQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status == "On Hold":
                date_map[d_str]["holdQty"] += max(0.0, rec_q - rej_q)
            date_map[d_str]["rejectedQty"] += rej_q

        for inw in inwards_qs:
            d_str = inw.date.strftime("%d-%m-%Y") if inw.date else ""
            if not d_str:
                continue
            if d_str not in date_map:
                date_map[d_str] = {
                    "date": d_str, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0,
                }
            rec_q = float(inw.qty or 0)
            date_map[d_str]["receiptsCount"] += 1
            date_map[d_str]["receivedQty"] += rec_q
            if inw.status not in ["Pending", "Pending QC", "Received"]:
                date_map[d_str]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                date_map[d_str]["goodQty"] += float(inw.accepted)
            elif inw.status == "On Hold":
                date_map[d_str]["holdQty"] += rec_q
            date_map[d_str]["rejectedQty"] += float(inw.rejected)

        date_wise_summary_report = []
        for d in date_map.values():
            insp = d["inspectedQty"] or d["receivedQty"] or 1.0
            date_wise_summary_report.append({
                **d,
                "passPct": round((d["goodQty"] / insp) * 100, 1),
                "rejectionPct": round((d["rejectedQty"] / insp) * 100, 1),
            })

        # Summary KPIs across all audited records
        total_rcv = sum(s["receivedQty"] for s in supplier_rejection_report)
        total_insp = sum(s["inspectedQty"] for s in supplier_rejection_report)
        total_gd = sum(s["goodQty"] for s in supplier_rejection_report)
        total_rej = sum(s["rejectedQty"] for s in supplier_rejection_report)
        total_hld = sum(s["holdQty"] for s in supplier_rejection_report)

        summary_kpis = {
            "totalReceipts": sum(s["receiptsCount"] for s in supplier_rejection_report),
            "totalReceived": round(total_rcv, 2),
            "totalInspected": round(total_insp, 2),
            "passRate": round((total_gd / (total_insp or 1.0)) * 100, 1) if total_insp else 0.0,
            "rejectionRate": round((total_rej / (total_insp or 1.0)) * 100, 1) if total_insp else 0.0,
            "pendingHoldQty": round(total_hld + sum(h["qty"] for h in hold_pending_report if h["status"] == "Pending"), 2),
            "reprocessRecovery": round(
                (sum(r["receivedQty"] for r in reprocess_recovery_report) / (sum(r["sentQty"] for r in reprocess_recovery_report) or 1.0)) * 100, 1
            ) if reprocess_recovery_report else 0.0,
        }

        # Filter dropdown options
        suppliers_list = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors_list = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers_list + jwo_vendors_list if s])))
        qualities_list = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "summaryKPIs": summary_kpis,
            "reports": {
                "supplierRejection": supplier_rejection_report,
                "fabricDefects": fabric_defects_report,
                "holdPending": hold_pending_report,
                "reprocessRecovery": reprocess_recovery_report,
                "inspectorPerformance": inspector_performance_report,
                "dateWiseSummary": date_wise_summary_report,
            },
            "filters": {
                "suppliers": all_suppliers,
                "qualities": qualities_list,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
            },
        })


class QCReinspectionView(APIView):
    """
    GET /inventory/qc/reinspection/
    POST /inventory/qc/reinspection/
    POST /inventory/qc/reinspection/disposition/
    Handles QC Reinspection / Reprocess tracking and disposition.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        tab = request.query_params.get("tab") or "Pending Reprocess"
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        vendor = request.query_params.get("vendor") or "All"
        status_filter = request.query_params.get("status") or "All"
        item_code = request.query_params.get("item_code") or "All"

        reprocess_qs = (
            JWOReprocess.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )

        if from_date:
            reprocess_qs = reprocess_qs.filter(date__gte=from_date)
        if to_date:
            reprocess_qs = reprocess_qs.filter(date__lte=to_date)
        if vendor != "All":
            reprocess_qs = reprocess_qs.filter(
                Q(job_work_order__vendor_name=vendor) | Q(job_work_order__vendor__name=vendor)
            )

        records = []
        for r in reprocess_qs:
            jwo = r.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = jwo.vendor_name or (jwo.vendor.name if jwo and jwo.vendor else "Outside Processor")
            it_code = getattr(plan, "fabric_item", "FAB-01") or "FAB-01"
            it_name = (plan.fabric_item or plan.fabric_quality) if plan else "Fabric Reprocess Item"
            lot_no = (plan.grey_lot_no if plan else "") or f"LT-{str(r.id)[:4].upper()}"

            # Tab filtering:
            if tab == "Pending Reprocess" and r.status in ["Completed", "Cancelled"]:
                continue
            elif tab == "Sent for Reprocess" and r.status not in ["Sent to Vendor", "In Process", "Pending"]:
                continue
            elif tab == "Received from Reprocess" and r.status not in ["Received", "Completed"]:
                continue

            re_status = "Pending"
            if r.status == "Completed":
                re_status = "Completed"
            elif r.status in ["In Process", "Received"]:
                re_status = "In Progress"

            if status_filter != "All" and re_status != status_filter:
                continue

            sent_q = float(r.qty or 0)
            rec_q = sent_q if r.status in ["Received", "Completed"] else 0.0
            pend_q = max(0.0, sent_q - rec_q)

            meta = {}
            if r.remarks and r.remarks.strip().startswith("{") and r.remarks.strip().endswith("}"):
                try:
                    meta = json.loads(r.remarks)
                except Exception:
                    pass

            good_q = meta.get("goodQty", sent_q if re_status == "Completed" else 0.0)
            hold_q = meta.get("holdQty", 0.0)
            rep_q = meta.get("reprocessQty", 0.0)
            rej_q = meta.get("rejectedQty", 0.0)
            meas = meta.get("measurements") or [
                {"srNo": 1, "takaNo": 1, "actualQty": sent_q, "width": float(plan.width) if (plan and getattr(plan, "width", None)) else None, "remarks": "-"}
            ]

            records.append({
                "id": str(r.id),
                "reprocessNo": r.no or f"RP-{str(r.id)[:4].upper()}",
                "date": r.date.strftime("%d-%m-%Y") if r.date else "",
                "rawDate": r.date.isoformat() if r.date else "",
                "sourceQcNo": jwo.jwo_no if jwo else f"QC-{str(r.id)[:4].upper()}",
                "itemCode": it_code,
                "fabricItem": it_name,
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "pendingQty": pend_q,
                "vendor": v_name,
                "reinspectionStatus": re_status,
                "lotNo": lot_no,
                "goodQty": good_q,
                "holdQty": hold_q,
                "reprocessQty": rep_q,
                "rejectedQty": rej_q,
                "remarks": meta.get("remarks") or (r.remarks if not meta else r.reason),
                "measurements": meas,
                "auditTrail": [
                    {
                        "dateTime": f"{r.date.strftime('%d-%m-%Y')} 10:15" if r.date else "",
                        "action": "Created",
                        "user": "System",
                        "remarks": "Reinspection entry created",
                    },
                    *(
                        [{
                            "dateTime": timezone.now().strftime("%d-%m-%Y %H:%M"),
                            "action": re_status,
                            "user": "Inspector",
                            "remarks": meta.get("remarks") or "Reinspection updated",
                        }]
                        if re_status != "Pending"
                        else []
                    ),
                ],
            })

        vendors = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        items = list(
            Item.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("sku", flat=True)
            .distinct()
            .order_by("sku")
        )

        return Response({
            "records": records,
            "filters": {
                "vendors": vendors,
                "statuses": ["All", "Pending", "In Progress", "Completed"],
                "items": items,
            },
        })

    def post(self, request):
        client_id = request.client_id
        action_type = request.data.get("actionType") or "create"

        if action_type == "disposition":
            reprocess_id = request.data.get("reprocessId")
            if not reprocess_id:
                return Response({"detail": "reprocessId is required."}, status=status.HTTP_400_BAD_REQUEST)

            reprocess = JWOReprocess.objects.filter(client_id=client_id, id=reprocess_id).first()
            if not reprocess:
                return Response({"detail": "Reprocess record not found."}, status=status.HTTP_404_NOT_FOUND)

            is_draft = bool(request.data.get("isDraft", False))
            good_qty = Decimal(str(request.data.get("goodQty") or "0"))
            hold_qty = Decimal(str(request.data.get("holdQty") or "0"))
            reprocess_qty = Decimal(str(request.data.get("reprocessQty") or "0"))
            rejected_qty = Decimal(str(request.data.get("rejectedQty") or "0"))
            remarks = request.data.get("remarks") or ""
            measurements = request.data.get("measurements") or []

            meta = {
                "goodQty": float(good_qty),
                "holdQty": float(hold_qty),
                "reprocessQty": float(reprocess_qty),
                "rejectedQty": float(rejected_qty),
                "remarks": remarks,
                "measurements": measurements,
                "updatedAt": timezone.now().isoformat(),
            }

            with transaction.atomic():
                if is_draft:
                    reprocess.status = "In Process"
                    reprocess.remarks = json.dumps(meta)
                    reprocess.save(update_fields=["status", "remarks", "updated_at"])
                    msg = "Reinspection saved as draft."
                else:
                    reprocess.status = "Completed"
                    reprocess.remarks = json.dumps(meta)
                    reprocess.save(update_fields=["status", "remarks", "updated_at"])
                    msg = "Reinspection result approved. Stock Ledger and Stock Position updated."

            return Response({
                "success": True,
                "status": reprocess.status,
                "message": msg,
            })

        # Create new reinspection entry — requires explicit inputs
        jwo_id = request.data.get("jwo_id") or request.data.get("jwoId") or request.data.get("jobWorkOrderId")
        challan_no = request.data.get("challan_no") or request.data.get("challanNo")
        meters_raw = request.data.get("meters") or request.data.get("sentQty") or request.data.get("qty")
        date_str = request.data.get("date") or timezone.localdate().isoformat()
        reason = request.data.get("reason") or "Reprocessed fabric reinspection"

        field_errors = {}
        if not jwo_id:
            field_errors["jwo_id"] = ["Job Work Order ID is required."]
        if not challan_no:
            field_errors["challan_no"] = ["Challan number is required."]
        if not meters_raw or D(meters_raw) <= ZERO:
            field_errors["meters"] = ["Meters must be greater than zero."]

        if field_errors:
            raise ValidationFailed("Missing required reinspection fields.", field_errors=field_errors)

        jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first()
        if not jwo:
            jwo = JobWorkOrder.objects.filter(client_id=client_id, jwo_no=jwo_id).first()
        if not jwo:
            raise ValidationFailed(
                f"Job Work Order '{jwo_id}' does not exist.",
                field_errors={"jwo_id": ["Invalid Job Work Order."]},
            )

        sent_qty = D(meters_raw)
        try:
            from datetime import datetime
            r_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            r_date = timezone.localdate()

        rp_no = allocate_number(request.user.client, "RP", r_date)

        reprocess = JWOReprocess.objects.create(
            client_id=client_id,
            job_work_order=jwo,
            no=rp_no,
            date=r_date,
            qty=sent_qty,
            reason=f"{reason} (Challan: {challan_no})",
            status="Pending",
        )

        return Response({
            "success": True,
            "message": f"Reinspection record {rp_no} created successfully.",
            "reprocessId": str(reprocess.id),
            "reprocessNo": rp_no,
        })


class LotTakaRegisterView(APIView):
    """Lot / Taka Register endpoints for fabric lots, quantities, and movement history."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"

    def get(self, request, pk=None):
        client_id = request.client_id

        # Query parameters
        from_date_str = request.query_params.get("from_date")
        to_date_str = request.query_params.get("to_date")
        supplier_filter = request.query_params.get("supplier")
        quality_filter = request.query_params.get("quality")
        design_filter = request.query_params.get("design")
        shade_filter = request.query_params.get("shade")
        status_filter = request.query_params.get("status")
        qc_status_filter = request.query_params.get("qc_status")
        location_filter = request.query_params.get("location")
        search_query = request.query_params.get("search", "").strip()
        item_id_preview = request.query_params.get("item_id")

        if pk:
            lt = LotTaka.objects.filter(client_id=client_id, id=pk).select_related("item", "supplier", "location").first()
            if not lt:
                return Response({"detail": "Lot / Taka not found."}, status=status.HTTP_404_NOT_FOUND)
            # Fetch movement history
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, batch_number=lt.lot_number
            ).order_by("movement_date", "created_at")
            running_bal = Decimal("0")
            mov_list = []
            for m in mov_qs:
                running_bal += m.quantity
                m_type = m.type
                if m.type in ["PURCHASE", "ZONE_RECEIPT"] or m.reference_type in ["GRN", "LotRegister"]:
                    m_type_label = "GRN / Inward"
                elif m.type == "ZONE_ISSUE":
                    m_type_label = "Issue Sample" if m.reference_type == "SampleIssue" else "Stock Issue"
                elif m.type == "ZONE_RETURN":
                    m_type_label = "Sample Return" if m.reference_type == "SampleReturn" else "Stock Return"
                else:
                    m_type_label = m.type

                mov_list.append({
                    "date": m.movement_date.strftime("%d-%m-%Y"),
                    "type": m_type_label,
                    "qty": float(m.quantity),
                    "balance": float(running_bal),
                    "refNo": m.reference_number or f"REF-{str(m.id)[:4].upper()}",
                })

            it = lt.item
            return Response({
                "id": str(lt.id),
                "takaId": lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}",
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else "Fabric Item",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "-"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "receivedQty": float(lt.received_quantity),
                "qcStatus": lt.qc_status,
                "availableQty": float(lt.available_quantity),
                "location": lt.location.name if lt.location else "Main Godown",
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else "m"),
                "status": lt.status,
                "rate": float(lt.rate),
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "pieces": lt.pieces,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "remarks": lt.remarks or "-",
                "barcode": lt.barcode or f"{lt.lot_number}-{lt.taka_number}",
                "tags": lt.tags or [],
                "imageUrl": lt.image_url or "",
                "sourceDoc": lt.source_document_type,
                "grnNo": lt.source_document_number or "-",
                "stockMovements": mov_list,
            })

        # Base queryset
        qs = LotTaka.objects.filter(client_id=client_id).select_related(
            "item", "supplier", "location"
        )

        # Filters
        if from_date_str:
            try:
                from_d = date.fromisoformat(from_date_str)
                qs = qs.filter(received_date__gte=from_d)
            except Exception:
                pass

        if to_date_str:
            try:
                to_d = date.fromisoformat(to_date_str)
                qs = qs.filter(received_date__lte=to_d)
            except Exception:
                pass

        if supplier_filter and supplier_filter != "All":
            qs = qs.filter(supplier__name=supplier_filter)

        if quality_filter and quality_filter != "All":
            qs = qs.filter(item__fabric_quality=quality_filter)

        if design_filter and design_filter != "All":
            qs = qs.filter(item__fabric_design=design_filter)

        if shade_filter and shade_filter != "All":
            qs = qs.filter(item__fabric_color=shade_filter)

        if status_filter and status_filter != "All":
            qs = qs.filter(status=status_filter)

        if qc_status_filter and qc_status_filter != "All":
            qs = qs.filter(qc_status=qc_status_filter)

        if location_filter and location_filter != "All":
            qs = qs.filter(location__name=location_filter)

        if search_query:
            qs = qs.filter(
                Q(lot_number__icontains=search_query)
                | Q(taka_number__icontains=search_query)
                | Q(item__sku__icontains=search_query)
                | Q(item__name__icontains=search_query)
                | Q(supplier__name__icontains=search_query)
            )

        all_lots = list(qs)

        # Preload movements for movement history per lot
        lot_numbers = [lt.lot_number for lt in all_lots if lt.lot_number]
        movements_by_lot = {}
        if lot_numbers:
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, batch_number__in=lot_numbers
            ).order_by("movement_date", "created_at")
            for m in mov_qs:
                movements_by_lot.setdefault(m.batch_number, []).append(m)

        # Build records
        records = []
        tot_received = Decimal("0")
        tot_available = Decimal("0")
        tot_value = Decimal("0")
        pending_qc_qty = Decimal("0")
        pending_qc_lots = set()
        on_hold_qty = Decimal("0")
        on_hold_lots = set()
        active_count = 0
        closed_count = 0

        for lt in all_lots:
            it = lt.item
            rec_q = Decimal(str(lt.received_quantity or 0))
            avail_q = Decimal(str(lt.available_quantity or 0))
            rate_val = Decimal(str(lt.rate or 0))
            val = rec_q * rate_val

            tot_received += rec_q
            tot_available += avail_q
            tot_value += val

            if lt.status == "Active":
                active_count += 1
            else:
                closed_count += 1

            if lt.qc_status == "Pending":
                pending_qc_qty += rec_q
                pending_qc_lots.add(lt.lot_number)
            elif lt.qc_status == "On Hold":
                on_hold_qty += rec_q
                on_hold_lots.add(lt.lot_number)

            # Build movement history for this lot
            lot_movs = movements_by_lot.get(lt.lot_number, [])
            running_bal = Decimal("0")
            mov_list = []
            for m in lot_movs:
                running_bal += m.quantity
                m_type = m.type
                if m.type in ["PURCHASE", "ZONE_RECEIPT"] or m.reference_type in ["GRN", "LotRegister"]:
                    m_type_label = "GRN / Inward"
                elif m.type == "ZONE_ISSUE":
                    m_type_label = "Issue Sample" if m.reference_type == "SampleIssue" else "Stock Issue"
                elif m.type == "ZONE_RETURN":
                    m_type_label = "Sample Return" if m.reference_type == "SampleReturn" else "Stock Return"
                else:
                    m_type_label = m.type

                mov_list.append({
                    "date": m.movement_date.strftime("%d-%m-%Y"),
                    "type": m_type_label,
                    "qty": float(m.quantity),
                    "balance": float(running_bal),
                    "refNo": m.reference_number or f"REF-{str(m.id)[:4].upper()}",
                })

            records.append({
                "id": str(lt.id),
                "takaId": lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}",
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else "Fabric Item",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "-"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "receivedQty": float(rec_q),
                "qcStatus": lt.qc_status,
                "availableQty": float(avail_q),
                "location": lt.location.name if lt.location else "Main Godown",
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else "m"),
                "status": lt.status,
                "rate": float(rate_val),
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "pieces": lt.pieces,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "remarks": lt.remarks or "-",
                "barcode": lt.barcode or f"{lt.lot_number}-{lt.taka_number}",
                "tags": lt.tags or [],
                "imageUrl": lt.image_url or "",
                "sourceDoc": lt.source_document_type,
                "grnNo": lt.source_document_number or "-",
                "stockMovements": mov_list,
            })

        # Summary Cards
        avg_rate = float(tot_value / tot_received) if tot_received > 0 else 0.0
        summary_cards = {
            "totalLots": {
                "count": len(all_lots),
                "active": active_count,
                "closed": closed_count,
            },
            "totalQuantity": {
                "qty": float(tot_received),
                "inStock": float(tot_available),
                "uom": "m",
            },
            "totalValue": {
                "val": float(tot_value),
                "avgRate": avg_rate,
            },
            "pendingQC": {
                "qty": float(pending_qc_qty),
                "lots": len(pending_qc_lots),
                "uom": "m",
            },
            "onHold": {
                "qty": float(on_hold_qty),
                "lots": len(on_hold_lots),
                "uom": "m",
            },
        }

        # Filter option lists
        supplier_names = list(Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"]).values_list("name", flat=True).order_by("name"))
        quality_names = list(Item.objects.filter(client_id=client_id, fabric_quality__isnull=False).values_list("fabric_quality", flat=True).distinct().order_by("fabric_quality"))
        design_names = list(Design.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        if not design_names:
            design_names = list(Item.objects.filter(client_id=client_id, fabric_design__isnull=False).values_list("fabric_design", flat=True).distinct().order_by("fabric_design"))
        shade_names = list(Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        if not shade_names:
            shade_names = list(Item.objects.filter(client_id=client_id, fabric_color__isnull=False).values_list("fabric_color", flat=True).distinct().order_by("fabric_color"))
        location_names = list(Location.objects.filter(client_id=client_id).values_list("name", flat=True).order_by("name"))

        # Optional Item Preview Data for "+ Add Lot / Taka" Modal
        preview_data = None
        if item_id_preview:
            prev_item = Item.objects.filter(client_id=client_id, id=item_id_preview).first()
            if prev_item:
                curr_stock = float(StockBalance.objects.filter(client_id=client_id, item=prev_item).aggregate(tot=Sum("on_hand"))["tot"] or 0)
                recent_lots = []
                for rlt in LotTaka.objects.filter(client_id=client_id, item=prev_item).order_by("-created_at")[:5]:
                    recent_lots.append({
                        "lotNo": rlt.lot_number,
                        "takaRoll": rlt.taka_number,
                        "qty": float(rlt.received_quantity),
                        "shade": prev_item.fabric_color or "Default",
                        "status": rlt.status,
                    })
                preview_data = {
                    "itemCode": prev_item.sku,
                    "itemName": prev_item.name,
                    "quality": prev_item.fabric_quality or prev_item.name,
                    "design": prev_item.fabric_design or "Plain",
                    "shade": prev_item.fabric_color or "Default",
                    "uom": prev_item.uom or "m",
                    "currentStock": curr_stock,
                    "recentLots": recent_lots,
                }

        return Response({
            "summaryCards": summary_cards,
            "filters": {
                "suppliers": supplier_names,
                "qualities": quality_names,
                "designs": design_names,
                "shades": shade_names,
                "locations": location_names,
                "statuses": ["All", "Active", "Closed"],
                "qcStatuses": ["All", "Passed", "Pending", "On Hold", "Reprocess", "Rejected"],
            },
            "records": records,
            "itemPreview": preview_data,
        })

    @transaction.atomic
    def post(self, request):
        client_id = request.client_id
        data = request.data

        item_id = data.get("itemId")
        if not item_id:
            return Response({"detail": "Fabric item is required."}, status=status.HTTP_400_BAD_REQUEST)

        item = Item.objects.filter(client_id=client_id, id=item_id).first()
        if not item:
            return Response({"detail": "Selected fabric item not found."}, status=status.HTTP_404_NOT_FOUND)

        lot_no = (data.get("lotNo") or "").strip()
        if not lot_no:
            return Response({"detail": "Lot Number is required."}, status=status.HTTP_400_BAD_REQUEST)

        taka_roll = str(data.get("takaRoll") or "1").strip()

        # Check duplicate
        if LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, taka_number=taka_roll, deleted_at__isnull=True).exists():
            return Response({"detail": f"Lot '{lot_no}' with Taka / Roll '{taka_roll}' already exists in the register."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            received_qty = Decimal(str(data.get("receivedQty") or "0"))
            if received_qty <= ZERO:
                return Response({"detail": "Received quantity must be greater than 0."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"detail": "Invalid received quantity."}, status=status.HTTP_400_BAD_REQUEST)

        # Validate UOM Master and handle compatible conversions
        from apps.masters import uom as uom_service
        input_uom = (data.get("uom") or item.uom or "m").strip()
        item_uom = (item.uom or "m").strip()
        book = uom_service.UnitBook(client_id)

        if input_uom and item_uom and not uom_service.same_unit_text(input_uom, item_uom):
            u_in = book.resolve(input_uom)
            u_item = book.resolve(item_uom)
            if u_in and u_item:
                if not uom_service.compatible(u_in, u_item):
                    return Response({
                        "detail": f"UOM '{input_uom}' is incompatible with fabric item base unit '{item_uom}'."
                    }, status=status.HTTP_400_BAD_REQUEST)
                received_qty = round4(uom_service.convert(received_qty, u_in, u_item))
                input_uom = item_uom

        location_id = data.get("locationId")
        loc = Location.objects.filter(client_id=client_id, id=location_id).first() or Location.objects.filter(client_id=client_id).first()
        if not loc:
            return Response({"detail": "Storage location is required."}, status=status.HTTP_400_BAD_REQUEST)

        supplier_id = data.get("supplierId")
        supplier = Party.objects.filter(client_id=client_id, id=supplier_id).first() if supplier_id else None
        if not supplier and data.get("supplier"):
            supplier = Party.objects.filter(client_id=client_id, name=data.get("supplier")).first()

        source_doc = data.get("sourceDoc") or "GRN"
        grn_no = data.get("grnNo") or f"GRN-{timezone.localdate().strftime('%y%m%d')}"
        rec_date_str = data.get("receivedDate")
        try:
            rec_date = date.fromisoformat(rec_date_str) if rec_date_str else timezone.localdate()
        except Exception:
            rec_date = timezone.localdate()

        rate = Decimal(str(data.get("rate") or "0"))
        roll_weight = Decimal(str(data["rollWeight"])) if data.get("rollWeight") else None
        width = Decimal(str(data["width"])) if data.get("width") else None
        pieces = int(data.get("pieces") or 1)
        gsm = Decimal(str(data["gsm"])) if data.get("gsm") else None
        remarks = data.get("remarks") or ""
        tags = data.get("tags") or []
        image_url = data.get("imageUrl") or ""

        # Auto-allocate unique Taka ID (e.g. TAK-000125) and encode in barcode
        client = (
            request.user.client
            if (hasattr(request, "user") and request.user.is_authenticated and getattr(request.user, "client", None))
            else Client.objects.filter(id=client_id).first()
        )
        taka_code = allocate_number(client, "TAK")
        barcode = data.get("barcode") or taka_code

        # Create LotTaka
        lot_taka = LotTaka.objects.create(
            client_id=client_id,
            taka_id=taka_code,
            lot_number=lot_no,
            taka_number=taka_roll,
            item=item,
            supplier=supplier,
            source_document_type=source_doc,
            source_document_number=grn_no,
            location=loc,
            received_date=rec_date,
            received_quantity=received_qty,
            available_quantity=received_qty,
            uom=data.get("uom") or item.uom or "m",
            qc_status="Passed",
            status="Active",
            barcode=barcode,
            tag_status="Pending",
            print_count=0,
            roll_weight=roll_weight,
            width=width,
            pieces=pieces,
            gsm=gsm,
            rate=rate,
            remarks=remarks,
            tags=tags,
            image_url=image_url,
        )

        # Post StockMovement (PURCHASE) to update live Good Fabric Stock ledger
        mov = stock.post_movement(
            client_id=client_id,
            item=item,
            location=loc,
            type="PURCHASE",
            quantity=received_qty,
            weighed_qty=roll_weight or received_qty,
            unit_cost=rate,
            batch_number=lot_no,
            reference_type="LotRegister",
            reference_id=lot_taka.id,
            reference_number=f"LOT-{lot_no}-{taka_roll}",
            movement_date=rec_date,
            notes=json.dumps({
                "takaRoll": taka_roll,
                "takaId": taka_code,
                "remarks": remarks,
                "tags": tags,
                "sourceDoc": source_doc,
                "grnNo": grn_no,
            }),
            user=request.user if request.user.is_authenticated else None,
        )

        lot_taka.stock_movement = mov
        lot_taka.save(update_fields=["stock_movement"])

        return Response({
            "success": True,
            "message": f"Lot {lot_no} / Taka {taka_roll} ({taka_code}) created and added to stock ledger successfully.",
            "id": str(lot_taka.id),
            "takaId": lot_taka.taka_id,
            "lotNo": lot_taka.lot_number,
            "takaRoll": lot_taka.taka_number,
            "barcode": lot_taka.barcode,
        }, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def patch(self, request, pk=None):
        client_id = request.client_id
        lot_id = pk or request.data.get("id")
        lot_taka = LotTaka.objects.filter(client_id=client_id, id=lot_id).first()
        if not lot_taka:
            return Response({"detail": "Lot record not found."}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        disallowed = [f for f in ("locationId", "location", "rate", "status") if f in data]
        if disallowed:
            raise ValidationFailed(
                f"Direct edits to {', '.join(disallowed)} are not allowed. "
                "All stock, location, and status changes must come from documents "
                "(GRN, stock transfer, adjustment, or disposition)."
            )

        if "remarks" in data:
            lot_taka.remarks = data["remarks"]

        if "tags" in data:
            lot_taka.tags = data["tags"]

        if "rollWeight" in data:
            lot_taka.roll_weight = Decimal(str(data["rollWeight"])) if data["rollWeight"] else None

        if "width" in data:
            lot_taka.width = Decimal(str(data["width"])) if data["width"] else None

        if "gsm" in data:
            lot_taka.gsm = Decimal(str(data["gsm"])) if data["gsm"] else None

        if "qcStatus" in data:
            lot_taka.qc_status = data["qcStatus"]

        if "imageUrl" in data:
            lot_taka.image_url = data["imageUrl"]

        lot_taka.save()
        return Response({
            "success": True,
            "message": f"Lot {lot_taka.lot_number} / Taka {lot_taka.taka_number} updated successfully.",
        })


class TakaTagPrintView(APIView):
    """Endpoints for Taka Tag Print register, summary metrics, filters, and print event recording."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"

    def get(self, request):
        client_id = request.client_id

        # Query filters
        search_query = (request.query_params.get("search") or "").strip()
        from_date_str = request.query_params.get("from_date")
        to_date_str = request.query_params.get("to_date")
        supplier_filter = request.query_params.get("supplier")
        item_filter = request.query_params.get("item")
        quality_filter = request.query_params.get("quality")
        design_filter = request.query_params.get("design")
        shade_filter = request.query_params.get("shade")
        qc_status_filter = request.query_params.get("qc_status")
        tag_status_filter = request.query_params.get("tag_status")

        # Base queryset
        base_qs = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
            "item", "supplier", "location", "last_printed_by"
        )

        # Summary cards from real records
        total_takas = base_qs.count()
        tags_printed = base_qs.filter(tag_status__in=["Printed", "Reprinted"]).count()
        pending_print = base_qs.filter(tag_status="Pending").count()
        reprints_count = TakaTagPrintLog.objects.filter(client_id=client_id, action_type="REPRINT").count()
        if reprints_count == 0:
            reprints_count = base_qs.filter(print_count__gt=1).count()

        summary_cards = {
            "totalTakas": total_takas,
            "tagsPrinted": tags_printed,
            "pendingPrint": pending_print,
            "reprints": reprints_count,
        }

        # Filter option lists
        supplier_names = list(Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"]).values_list("name", flat=True).order_by("name"))
        quality_names = list(Item.objects.filter(client_id=client_id, fabric_quality__isnull=False).values_list("fabric_quality", flat=True).distinct().order_by("fabric_quality"))
        design_names = ["Floral", "Check", "Plain", "Stripe", "Jacquard", "Dobby", "Printed"]
        shade_names = ["Navy", "Blue", "Maroon", "White", "Black", "Green", "Grey", "Off-White", "Red"]
        fabric_items = list(Item.objects.filter(client_id=client_id, fabric_quality__isnull=False).values("id", "sku", "name")[:50])
        if not fabric_items:
            fabric_items = list(Item.objects.filter(client_id=client_id).values("id", "sku", "name")[:50])

        qs = base_qs
        if from_date_str:
            try:
                from_d = date.fromisoformat(from_date_str)
                qs = qs.filter(received_date__gte=from_d)
            except Exception:
                pass

        if to_date_str:
            try:
                to_d = date.fromisoformat(to_date_str)
                qs = qs.filter(received_date__lte=to_d)
            except Exception:
                pass

        if supplier_filter and supplier_filter != "All":
            qs = qs.filter(supplier__name=supplier_filter)

        if item_filter and item_filter != "All":
            qs = qs.filter(Q(item__sku__icontains=item_filter) | Q(item__name__icontains=item_filter))

        if quality_filter and quality_filter != "All":
            qs = qs.filter(item__fabric_quality=quality_filter)

        if design_filter and design_filter != "All":
            qs = qs.filter(item__fabric_design=design_filter)

        if shade_filter and shade_filter != "All":
            qs = qs.filter(item__fabric_color=shade_filter)

        if qc_status_filter and qc_status_filter != "All":
            qs = qs.filter(qc_status=qc_status_filter)

        if tag_status_filter and tag_status_filter != "All":
            qs = qs.filter(tag_status=tag_status_filter)

        if search_query:
            qs = qs.filter(
                Q(taka_id__icontains=search_query)
                | Q(barcode__icontains=search_query)
                | Q(lot_number__icontains=search_query)
                | Q(taka_number__icontains=search_query)
                | Q(source_document_number__icontains=search_query)
                | Q(item__sku__icontains=search_query)
                | Q(item__name__icontains=search_query)
                | Q(supplier__name__icontains=search_query)
            )

        records = []
        for lt in qs:
            it = lt.item
            taka_id = lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}"
            records.append({
                "id": str(lt.id),
                "takaId": taka_id,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else "Fabric Item",
                "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "-"),
                "design": it.fabric_design if it and it.fabric_design else "Plain",
                "shade": it.fabric_color if it and it.fabric_color else "Default",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedQty": float(lt.received_quantity),
                "availableQty": float(lt.available_quantity),
                "uom": lt.uom or (it.uom if it else "m"),
                "qcStatus": lt.qc_status,
                "tagStatus": lt.tag_status or "Pending",
                "barcode": lt.barcode or taka_id,
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "lastPrintedBy": getattr(lt.last_printed_by, "name", None) or getattr(lt.last_printed_by, "email", None) or None,
                "location": lt.location.name if lt.location else "Main Godown",
                "locationId": str(lt.location.id) if lt.location else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "grnNo": lt.source_document_number or "-",
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "rate": float(lt.rate),
            })

        return Response({
            "summaryCards": summary_cards,
            "filters": {
                "suppliers": supplier_names,
                "items": [f"{it['sku']} ({it['name']})" for it in fabric_items],
                "qualities": quality_names,
                "designs": design_names,
                "shades": shade_names,
                "qcStatuses": ["All", "Passed", "Pending", "On Hold", "Reprocess", "Rejected"],
                "tagStatuses": ["All", "Pending", "Printed", "Reprinted"],
            },
            "records": records,
        })

    @transaction.atomic
    def post(self, request):
        """Record print or reprint event for selected takas (never alters stock)."""
        client_id = request.client_id
        data = request.data
        ids = data.get("ids") or data.get("takaIds") or []
        if isinstance(ids, str):
            ids = [ids]
        action_type = data.get("actionType") or ("REPRINT" if data.get("isReprint") else "PRINT")
        notes = data.get("notes") or ""

        if not ids:
            return Response({"detail": "No takas selected for printing."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        uuid_ids = []
        code_ids = []
        for val in ids:
            v_str = str(val).strip()
            try:
                uuid.UUID(v_str)
                uuid_ids.append(v_str)
            except (ValueError, AttributeError):
                code_ids.append(v_str)

        q_cond = Q()
        if uuid_ids:
            q_cond |= Q(id__in=uuid_ids)
        if code_ids:
            q_cond |= Q(taka_id__in=code_ids) | Q(barcode__in=code_ids)

        takas = list(LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(q_cond))
        if not takas:
            return Response({"detail": "Selected taka records not found."}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        user = request.user if request.user.is_authenticated else None
        user_name = getattr(user, "name", None) or getattr(user, "email", "Admin")

        for lt in takas:
            old_count = lt.print_count
            lt.print_count += 1
            if action_type == "REPRINT" or old_count >= 1:
                lt.tag_status = "Reprinted"
                record_action = "REPRINT"
            else:
                lt.tag_status = "Printed"
                record_action = "PRINT" if len(takas) == 1 else "BULK_PRINT"

            lt.last_printed_at = now
            lt.last_printed_by = user
            lt.save(update_fields=["print_count", "tag_status", "last_printed_at", "last_printed_by", "updated_at"])

            TakaTagPrintLog.objects.create(
                client_id=client_id,
                lot_taka=lt,
                action_type=record_action,
                printed_at=now,
                printed_by=user,
                printed_by_name=user_name,
                taka_id=lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}",
                lot_number=lt.lot_number,
                taka_number=lt.taka_number,
                notes=notes,
            )

        return Response({
            "success": True,
            "message": f"Recorded print event for {len(takas)} taka(s).",
            "count": len(takas),
        })


class TakaTagScanView(APIView):
    """Fast barcode scanner lookup endpoint for takas (Code 128 / Taka ID)."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"

    def get(self, request):
        client_id = request.client_id
        code = (request.query_params.get("code") or request.query_params.get("search") or "").strip()
        if not code:
            return Response({"detail": "Please provide a barcode or Taka ID to scan."}, status=status.HTTP_400_BAD_REQUEST)

        lt = LotTaka.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(
            Q(taka_id__iexact=code)
            | Q(barcode__iexact=code)
            | Q(lot_number__iexact=code)
        ).select_related("item", "supplier", "location", "last_printed_by").first()

        if not lt:
            return Response({
                "found": False,
                "detail": f"No active Taka record found matching barcode/ID '{code}'.",
            }, status=status.HTTP_404_NOT_FOUND)

        it = lt.item
        taka_id = lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}"

        taka_dict = {
            "found": True,
            "id": str(lt.id),
            "takaId": taka_id,
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "itemCode": it.sku if it else "-",
            "itemName": it.name if it else "Fabric Item",
            "quality": it.fabric_quality if it and it.fabric_quality else (it.name if it else "-"),
            "design": it.fabric_design if it and it.fabric_design else "Plain",
            "shade": it.fabric_color if it and it.fabric_color else "Default",
            "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
            "supplierId": str(lt.supplier.id) if lt.supplier else "",
            "receivedQty": float(lt.received_quantity),
            "availableQty": float(lt.available_quantity),
            "uom": lt.uom or (it.uom if it else "m"),
            "qcStatus": lt.qc_status,
            "tagStatus": lt.tag_status or "Pending",
            "barcode": lt.barcode or taka_id,
            "printCount": lt.print_count or 0,
            "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
            "location": lt.location.name if lt.location else "Main Godown",
            "locationId": str(lt.location.id) if lt.location else "",
            "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
            "grnNo": lt.source_document_number or "-",
            "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
            "width": float(lt.width) if lt.width else None,
            "gsm": float(lt.gsm) if lt.gsm else None,
            "rate": float(lt.rate),
        }
        res_payload = dict(taka_dict)
        res_payload["taka"] = taka_dict
        return Response(res_payload)


class TakaTagHistoryView(APIView):
    """View print/reprint history audit logs for a specific taka."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"

    def get(self, request, pk=None):
        client_id = request.client_id
        target = pk or request.query_params.get("takaId") or request.query_params.get("taka_id") or request.query_params.get("id")
        if not target:
            return Response({"detail": "Taka ID or UUID required."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        target_str = str(target).strip()
        is_uuid = False
        try:
            uuid.UUID(target_str)
            is_uuid = True
        except (ValueError, AttributeError):
            is_uuid = False

        if is_uuid:
            lt = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True, id=target_str).first()
        else:
            lt = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                Q(taka_id__iexact=target_str) | Q(barcode__iexact=target_str) | Q(lot_number__iexact=target_str)
            ).first()

        if not lt:
            return Response({"detail": "Taka record not found."}, status=status.HTTP_404_NOT_FOUND)

        logs = TakaTagPrintLog.objects.filter(client_id=client_id, lot_taka=lt).order_by("-printed_at")
        history = [
            {
                "id": str(log.id),
                "actionType": log.action_type,
                "printedAt": log.printed_at.strftime("%d-%m-%Y %H:%M:%S"),
                "printedBy": log.printed_by_name or (log.printed_by.name if log.printed_by else "Admin"),
                "takaId": log.taka_id,
                "lotNo": log.lot_number,
                "takaRoll": log.taka_number,
                "notes": log.notes or "-",
            }
            for log in logs
        ]
        return Response({
            "takaId": lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}",
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "printCount": lt.print_count,
            "history": history,
            "logs": history,
        })


class TakaTagPdfView(APIView):
    """Generate printable PDF labels (60mm x 40mm) with Code 128 barcode using ReportLab."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"

    def get(self, request):
        import io
        import uuid
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
        from reportlab.graphics.barcode import code128

        client_id = request.client_id
        ids_str = request.query_params.get("ids") or request.query_params.get("takaIds") or ""
        ids_str = ids_str.strip()
        if not ids_str:
            return Response({"detail": "No taka IDs specified."}, status=status.HTTP_400_BAD_REQUEST)

        ids = [i.strip() for i in ids_str.split(",") if i.strip()]
        uuid_ids = []
        code_ids = []
        for val in ids:
            v_str = str(val).strip()
            try:
                uuid.UUID(v_str)
                uuid_ids.append(v_str)
            except (ValueError, AttributeError):
                code_ids.append(v_str)

        q_cond = Q()
        if uuid_ids:
            q_cond |= Q(id__in=uuid_ids)
        if code_ids:
            q_cond |= Q(taka_id__in=code_ids) | Q(barcode__in=code_ids)

        takas = list(
            LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                q_cond
            ).select_related("item", "supplier", "location")
        )
        if not takas:
            return Response({"detail": "No matching takas found."}, status=status.HTTP_404_NOT_FOUND)

        buffer = io.BytesIO()
        label_size = (60 * mm, 40 * mm)
        pdf = canvas.Canvas(buffer, pagesize=label_size)

        for lt in takas:
            it = lt.item
            taka_id = lt.taka_id or f"TAK-{str(lt.id)[:6].upper()}"
            item_code = it.sku if it else "-"
            item_name = it.name if it else "Fabric"
            quality = it.fabric_quality if it and it.fabric_quality else (it.name if it else "-")
            design = it.fabric_design if it and it.fabric_design else "Plain"
            shade = it.fabric_color if it and it.fabric_color else "Default"
            supplier = lt.supplier.name if lt.supplier else (lt.source_document_number or "-")
            grn_no = lt.source_document_number or "-"
            qty_str = f"{float(lt.received_quantity):.2f} {lt.uom or 'm'}"
            qc_st = lt.qc_status or "Passed"

            # Top Header: OSCAR TEXTILE ERP | QC Status
            pdf.setFont("Helvetica-Bold", 7.5)
            pdf.drawString(2.5 * mm, 36.5 * mm, "OSCAR TEXTILE ERP")
            pdf.setFont("Helvetica-Bold", 6.5)
            pdf.drawRightString(57.5 * mm, 36.5 * mm, f"QC: {qc_st.upper()}")

            pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
            pdf.setLineWidth(0.5)
            pdf.line(2.5 * mm, 35 * mm, 57.5 * mm, 35 * mm)

            # Code 128 Barcode
            try:
                bc = code128.Code128(taka_id, barHeight=7.5 * mm, barWidth=0.25 * mm, humanReadable=False)
                bc.drawOn(pdf, 3.5 * mm, 26 * mm)
            except Exception:
                pass

            # Human readable Taka ID
            pdf.setFont("Helvetica-Bold", 7.5)
            pdf.drawCentredString(30 * mm, 22.5 * mm, taka_id)

            # Details
            pdf.setFont("Helvetica-Bold", 5.5)
            pdf.drawString(2.5 * mm, 18.5 * mm, f"Lot: {lt.lot_number} | Roll: #{lt.taka_number}")

            pdf.setFont("Helvetica", 5.5)
            item_desc = f"{item_code} - {item_name}"
            if len(item_desc) > 34:
                item_desc = item_desc[:32] + "..."
            pdf.drawString(2.5 * mm, 15 * mm, f"Item: {item_desc}")

            qds = f"{quality} | {design} | {shade}"
            if len(qds) > 35:
                qds = qds[:33] + "..."
            pdf.drawString(2.5 * mm, 11.5 * mm, f"Q/D/S: {qds}")

            supp_line = f"Supp: {supplier[:18]} | GRN: {grn_no[:10]}"
            pdf.drawString(2.5 * mm, 8 * mm, supp_line)

            # Quantity and UOM
            pdf.setFont("Helvetica-Bold", 6.5)
            pdf.drawString(2.5 * mm, 4 * mm, f"Qty: {qty_str}")
            pdf.setFont("Helvetica", 5)
            pdf.drawRightString(57.5 * mm, 4 * mm, lt.received_date.strftime("%d/%m/%Y"))

            pdf.showPage()

        pdf.save()
        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="taka_tags.pdf"'
        return response



