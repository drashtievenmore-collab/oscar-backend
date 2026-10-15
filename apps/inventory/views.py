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
from apps.accounts.models import User
from apps.masters import uom
from apps.masters.models import Design, Fabric, FabricQuality, Item, ItemCategory, ItemPart, Location, Party, Shade
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseBill, PurchaseBillLine
from apps.sales.models import SalesReturn, SalesReturnLine
from apps.jobwork.models import JWOInward, JWOOutward, JWOReprocess, JobWorkOrder

from . import costing_services, logistics_services, sampling_services
from . import services as stock
from .models import (
    FaultyPart,
    LotCostEntry,
    LotTaka,
    QCInspection,
    QCInspectionLine,
    QCInspectionLineDefect,
    QualityStandard,
    Sample,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    TakaTagPrintLog,
    Trip,
    TripMaterialLine,
    ZoneRequest,
    ZoneRequestLine,
)
from .serializers import (
    FaultyPartSerializer,
    ItemPartSerializer,
    LotCostEntrySerializer,
    QCInspectionLineDefectSerializer,
    QCInspectionLineSerializer,
    QCInspectionSerializer,
    QualityStandardSerializer,
    SampleSerializer,
    ServiceUsageSerializer,
    StockAdjustmentSerializer,
    StockAuditSerializer,
    StockMovementSerializer,
    StockTransferSerializer,
    TripMaterialLineSerializer,
    TripSerializer,
    ZoneRequestSerializer,
)

QTY = DecimalField(max_digits=18, decimal_places=4)

def _qc_meta(receipt):
    """The QC disposition record stored on ``GoodsReceipt.qc_note`` (JSON)."""
    note = (getattr(receipt, "qc_note", None) or "").strip()
    if note.startswith("{") and note.endswith("}"):
        try:
            return json.loads(note)
        except ValueError:
            return {}
    return {}


def _qc_remarks(receipt):
    """Free-text QC remarks of a receipt (from the JSON record or a plain note)."""
    meta = _qc_meta(receipt)
    if meta:
        return meta.get("remarks") or None
    return (getattr(receipt, "qc_note", None) or None)

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

        lot = None
        lot_id = request.data.get("lotTakaId")
        if lot_id:
            lot = LotTaka.objects.filter(
                client_id=request.client_id, id=lot_id, item=item, deleted_at__isnull=True
            ).first()
            if lot is None:
                raise ValidationFailed("That taka does not exist for this item.", field_errors={"lotTakaId": ["Not found."]})
            location = lot.location

        current = stock.calculate_item_stock(request.client_id, item, location.id)
        current_on_hand = D(current["onHand"])
        # The base for an absolute count is the taka itself when one is named.
        base = D(lot.available_quantity) if lot is not None else current_on_hand
        if data.get("isAbsolute"):
            target_qty = round4(D(data["quantity"]))
            if target_qty < ZERO:
                raise ValidationFailed(
                    "Stock quantity cannot be adjusted to a negative value.",
                    field_errors={"quantity": ["Stock cannot be negative."]},
                )
            delta = round4(target_qty - base)
        else:
            delta = round4(data["quantity"])
        if base + delta < ZERO:
            raise ValidationFailed(
                f"Adjustment of {delta} would result in negative stock ({base + delta}). Current stock: {base}.",
                field_errors={"quantity": ["Adjustment results in negative stock."]},
            )
        if delta < ZERO:
            # Stock promised to open sales orders cannot be adjusted away.
            reserved = D(current["reserved"])
            free = D(current["onHand"]) - reserved
            if (
                location.code not in stock.BLOCKED_LOCATION_CODES
                and reserved > ZERO
                and abs(delta) > free
            ):
                raise BusinessRuleViolation(
                    f"Only {round4(max(free, ZERO))} of {item.name} is free; {round4(reserved)} is "
                    "reserved for open sales orders.",
                    code=Codes.INSUFFICIENT_STOCK,
                    payload={"reserved": str(round4(reserved)), "free": str(round4(max(free, ZERO)))},
                )

        if delta == ZERO:
            raise BusinessRuleViolation(
                "That adjustment would not change the stock figure.",
                code="NO_CHANGE",
            )

        # TODO(Phase 4 approval engine): adjustments whose value exceeds
        # settings.STOCK_ADJUSTMENT_APPROVAL_LIMIT must be routed for approval
        # instead of posting directly. The engine does not exist yet, so the
        # limit is only reported back to the caller.
        from django.conf import settings as django_settings

        approval_limit = getattr(django_settings, "STOCK_ADJUSTMENT_APPROVAL_LIMIT", None)
        adjustment_value = round2(abs(delta) * D(data.get("unitCost") or item.cost_price or 0))

        movement = stock.post_movement(
            client_id=request.client_id,
            item=item,
            location=location,
            type="ADJUSTMENT",
            quantity=delta,
            unit_cost=data.get("unitCost") or item.cost_price,
            notes=data["reason"],
            user=request.user,
            lot_taka=lot,
        )

        from apps.accounting.services import post_stock_adjustment

        post_stock_adjustment(movement, user=request.user)

        return Response(
            {
                "movement": StockMovementSerializer(movement).data,
                "stock": stock.calculate_item_stock(request.client_id, item),
                "adjustmentValue": adjustment_value,
                "exceedsApprovalLimit": (
                    approval_limit is not None and adjustment_value > D(str(approval_limit))
                ),
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

    def _validate_takas(self, line, from_location_id):
        """Normalise a line's ``takas`` and check them against the source."""
        rows = []
        total = ZERO
        for raw in line.get("takas") or []:
            lot_id = raw.get("lotTakaId") or raw.get("id")
            lot = LotTaka.objects.filter(
                client_id=self.get_client_id(), id=lot_id, deleted_at__isnull=True
            ).first() if lot_id else None
            if lot is None:
                raise ValidationFailed("A selected taka does not exist.", field_errors={"takas": [f"Unknown taka {lot_id}."]})
            if lot.item_id != line["item"].id:
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number} is not {line['item'].name}.",
                    field_errors={"takas": ["Item mismatch."]},
                )
            if str(lot.location_id) != str(from_location_id):
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number} is not at the source location.",
                    field_errors={"takas": ["Wrong location."]},
                )
            qty = round4(D(raw.get("qty") if raw.get("qty") not in (None, "") else lot.available_quantity))
            if qty <= ZERO or qty > D(lot.available_quantity):
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number}: quantity must be between 0 and "
                    f"{round4(lot.available_quantity)}.",
                    field_errors={"takas": ["Invalid taka quantity."]},
                )
            rows.append({"lotTakaId": str(lot.id), "qty": str(qty)})
            total += qty
        if rows and abs(total - round4(D(line["qty"]))) > Decimal("0.0001"):
            raise ValidationFailed(
                f"Selected takas ({total}) must add up to the line quantity ({round4(D(line['qty']))}).",
                field_errors={"takas": ["Taka total differs from the line quantity."]},
            )
        if not rows and getattr(line["item"], "tracking_type", "NONE") in ("LOT", "TAKA"):
            raise ValidationFailed(
                f"{line['item'].name} is taka-tracked: select the takas to transfer.",
                field_errors={"takas": ["Required for this item."]},
            )
        return rows

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("items", [])
        if not lines:
            raise ValidationFailed(
                "A transfer needs at least one item.",
                field_errors={"items": ["Add at least one line."]},
            )
        from_location = serializer.validated_data["from_location"]
        for line in lines:
            line["takas"] = self._validate_takas(line, from_location.id)
        serializer.validated_data["transfer_number"] = allocate_number(
            self.request.user.client, "TR", serializer.validated_data.get("transfer_date")
        )
        # Stock moves only through the dispatch action.
        serializer.validated_data["status"] = "Requested"
        serializer.validated_data.setdefault("requested_by", self.request.user)
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
        """Posts ``TRANSFER_OUT`` / ``TRANSFER_IN`` per selected taka (api.md §7.2).

        A taka moved in part is split: the moved part becomes a child taka at
        the destination. Lines without takas move item quantity; items with
        takas at the source but none selected take them oldest-first.
        """
        transfer = self.get_object()
        if transfer.status != "Requested":
            raise Conflict(
                f"This transfer is already {transfer.status}.", code=Codes.BAD_TARGET
            )

        with transaction.atomic():
            common = dict(
                client_id=request.client_id,
                reference_type="StockTransfer",
                reference_id=transfer.id,
                reference_number=transfer.transfer_number,
                source_document_type="StockTransfer",
                source_document_id=transfer.id,
                movement_date=transfer.transfer_date,
                user=request.user,
            )
            for line in transfer.items.filter(deleted_at__isnull=True).select_related("item"):
                stock.assert_sufficient_stock(
                    request.client_id, line.item, line.qty,
                    transfer.from_location_id, line.item.name,
                )
                selections = [(r["lotTakaId"], D(r["qty"])) for r in (line.takas or [])]
                if not selections:
                    # FIFO over the takas at the source, if the item has any.
                    remaining = D(line.qty)
                    for lot in LotTaka.objects.filter(
                        client_id=request.client_id, item_id=line.item_id,
                        location_id=transfer.from_location_id, available_quantity__gt=0,
                        deleted_at__isnull=True,
                    ).order_by("received_date", "created_at"):
                        if remaining <= ZERO:
                            break
                        take = min(D(lot.available_quantity), remaining)
                        selections.append((lot.id, take))
                        remaining -= take
                    on_takas = sum((q for _, q in selections), ZERO)
                    loose = round4(D(line.qty) - on_takas)
                else:
                    loose = ZERO
                for lot_id, qty in selections:
                    stock.relocate_lot(
                        lot=lot_id, quantity=qty, to_location_id=transfer.to_location_id,
                        out_type="TRANSFER_OUT", in_type="TRANSFER_IN", split_suffix="TR",
                        **common,
                    )
                if loose > ZERO:
                    stock.relocate_item_qty(
                        item=line.item, quantity=loose,
                        from_location_id=transfer.from_location_id,
                        to_location_id=transfer.to_location_id,
                        out_type="TRANSFER_OUT", in_type="TRANSFER_IN",
                        unit_cost=line.item.cost_price, **common,
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
        """Reverses exactly this transfer's movements (taka by taka, newest
        first) and folds the split-off child takas back into their parents."""
        transfer = self.get_object()
        if transfer.status == "Cancelled":
            raise Conflict("This transfer is already cancelled.", code=Codes.ALREADY_CANCELLED)

        with transaction.atomic():
            if transfer.status in ("In Transit", "Received", "Completed"):
                stock.reverse_movements(
                    reference_type="StockTransfer",
                    reference_id=transfer.id,
                    client_id=request.client_id,
                    user=request.user,
                    notes="Transfer cancelled",
                )
                # Split children created by this transfer are now empty: the
                # quantity is back on the parent taka.
                LotTaka.objects.filter(
                    client_id=request.client_id,
                    source_document_type="StockTransfer",
                    source_document_id=transfer.id,
                    available_quantity=0,
                    deleted_at__isnull=True,
                ).update(deleted_at=timezone.now(), status="Closed", updated_at=timezone.now())
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


#: Stock ageing buckets (Phase 1.1) -- age is days since the inward date of
#: the lot/taka, or of the FIFO receipt layer for stock not held on takas.
STOCK_AGE_BUCKETS = ("0-30", "31-60", "61-90", "91-180", "180+")


def stock_age_bucket(days):
    days = int(days or 0)
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    if days <= 180:
        return "91-180"
    return "180+"


class ValuationView(APIView):
    """``GET /inventory/valuation/`` -- ``?method=WAC``, ``?bucket=``, ``?locationId=``.

    Value is WAC from the movement ``unit_cost`` (db.md Appendix B decision 5);
    FIFO valuation would need per-receipt cost layers and is reported as
    unsupported rather than silently returning WAC under a FIFO label.

    Ageing is real: each lot/taka with stock is aged from its received date;
    stock that is not held on takas is aged by FIFO over the location's
    inward movements (the newest receipts are assumed to be what is left).
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    INWARD_TYPES = (
        "PURCHASE", "TRANSFER_IN", "JOBWORK_INWARD", "ADJUSTMENT", "SALES_RETURN",
        "SALES_RETURN_GOOD", "SAMPLE_RETURN", "QC_PASS",
    )

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
        bucket_filter = request.query_params.get("bucket")
        if bucket_filter and bucket_filter not in STOCK_AGE_BUCKETS:
            raise ValidationFailed(
                "Unknown ageing bucket.",
                field_errors={"bucket": [f"Expected one of {', '.join(STOCK_AGE_BUCKETS)}."]},
            )
        location_filter = request.query_params.get("locationId")

        client_id = request.client_id
        today = timezone.localdate()
        balances = (
            StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("item", "item__category", "location")
            .exclude(item__item_kind="Service")
        )
        if location_filter:
            balances = balances.filter(location_id=location_filter)

        lots_by_key = {}
        for lot in LotTaka.objects.filter(
            client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0
        ).order_by("received_date"):
            lots_by_key.setdefault((lot.item_id, lot.location_id), []).append(lot)

        rows = []
        bucket_totals = {label: {"qty": ZERO, "value": ZERO} for label in STOCK_AGE_BUCKETS}
        total_value = ZERO

        def add_row(balance, qty, unit_cost, inward_date, lot=None):
            nonlocal total_value
            qty = round4(qty)
            if qty <= ZERO:
                return
            age_days = max(0, (today - inward_date).days) if inward_date else None
            bucket = stock_age_bucket(age_days) if age_days is not None else None
            value = round2(qty * D(unit_cost))
            if bucket is not None:
                bucket_totals[bucket]["qty"] += qty
                bucket_totals[bucket]["value"] += value
            total_value += value
            item = balance.item
            rows.append({
                "id": str(lot.id) if lot else f"{balance.item_id}:{balance.location_id}:{inward_date}",
                "itemId": str(balance.item_id),
                "sku": item.sku,
                "name": item.name,
                "category": item.category.name if item.category_id else None,
                "locationId": str(balance.location_id),
                "location": balance.location.name,
                "lotNo": lot.lot_number if lot else None,
                "takaId": (lot.taka_id or lot.barcode) if lot else None,
                "uom": item.uom,
                "qty": qty,
                "unitCost": round4(unit_cost),
                "value": value,
                "inwardDate": inward_date.isoformat() if inward_date else None,
                "ageDays": age_days,
                "agingBucket": bucket,
            })

        for balance in balances:
            on_hand = D(balance.on_hand)
            if on_hand <= ZERO:
                continue
            unit_cost = balance.weighted_average_cost or D(balance.item.cost_price)
            remaining = on_hand
            for lot in lots_by_key.get((balance.item_id, balance.location_id), []):
                if remaining <= ZERO:
                    break
                qty = min(D(lot.available_quantity), remaining)
                add_row(balance, qty, unit_cost, lot.received_date, lot)
                remaining -= qty
            if remaining <= ZERO:
                continue
            # FIFO layers for the part not held on takas: walk inward
            # movements newest-first until the remaining quantity is covered.
            inwards = StockMovement.objects.filter(
                client_id=client_id,
                item_id=balance.item_id,
                location_id=balance.location_id,
                quantity__gt=0,
                type__in=self.INWARD_TYPES,
                lot_taka__isnull=True,
            ).order_by("-movement_date", "-created_at").values_list("movement_date", "quantity")
            for movement_date, quantity in inwards:
                if remaining <= ZERO:
                    break
                qty = min(D(quantity), remaining)
                add_row(balance, qty, unit_cost, movement_date)
                remaining -= qty
            if remaining > ZERO:
                # Stock with no inward record at this location (e.g. opening
                # balance) has no known age; it is valued but not bucketed.
                add_row(balance, remaining, unit_cost, None)

        if bucket_filter:
            rows = [row for row in rows if row["agingBucket"] == bucket_filter]
        rows.sort(key=lambda row: (row["ageDays"] is None, -(row["ageDays"] or 0)))
        return Response(
            envelope(
                rows,
                aggregates={
                    "method": "WAC",
                    "totalValuation": round2(total_value),
                    "buckets": [
                        {
                            "bucket": label,
                            "qty": round4(bucket_totals[label]["qty"]),
                            "value": round2(bucket_totals[label]["value"]),
                        }
                        for label in STOCK_AGE_BUCKETS
                    ],
                    "currentStockValue": round2(bucket_totals["0-30"]["value"]),
                    "staleValuation": round2(
                        bucket_totals["91-180"]["value"] + bucket_totals["180+"]["value"]
                    ),
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

        # Every stock figure below comes from stock_balances, split by the
        # *location* the stock is in: QC Hold / Reprocess / Second Quality /
        # Reject are system locations, everything else is good stock. The
        # categories partition on_hand, so nothing is counted twice.
        # Every stock figure below comes from stock_balances, split by the
        # location the stock is in: QC Hold / Reprocess / Second Quality /
        # Reject are system locations, SYS-WIP-* are external processor/agency
        # WIP locations, and standard godowns hold good/available stock.
        role_by_code = {
            code: role for role, (code, _, _) in stock.SYSTEM_LOCATIONS.items()
        }
        all_locations = list(Location.objects.filter(client_id=client_id, deleted_at__isnull=True))
        location_role = {}
        wip_locations = {}
        for loc in all_locations:
            if loc.code in role_by_code:
                location_role[loc.id] = role_by_code[loc.code]
            elif (loc.code and loc.code.startswith("SYS-WIP-")) or (loc.name and loc.name.startswith("WIP - ")):
                location_role[loc.id] = "EXTERNAL_WIP"
                wip_locations[loc.id] = loc
            else:
                location_role[loc.id] = "GOOD"

        def wac(balance):
            return D(balance.weighted_average_cost or get_item_cost(balance.item_id))

        def get_item_cost(item_id):
            item = item_map.get(item_id)
            return D(item.cost_price or 0) if item else ZERO

        role_qty = {role: ZERO for role in ("GOOD", "EXTERNAL_WIP", "QC_HOLD", "REPROCESS", "SECOND_QUALITY", "REJECT")}
        role_val = {role: ZERO for role in role_qty}

        wip_by_party_dict = {}
        parties = {p.code: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True) if p.code}
        parties_by_name = {p.name: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True)}

        for b in balance_rows:
            role = location_role.get(b.location_id, "GOOD")
            on_hand = D(b.on_hand)
            val = on_hand * wac(b)
            role_qty[role] += on_hand
            role_val[role] += val

            if role == "EXTERNAL_WIP":
                loc = wip_locations.get(b.location_id)
                loc_code = loc.code if loc else ""
                loc_name = loc.name if loc else ""
                suffix = loc_code.replace("SYS-WIP-", "") if loc_code.startswith("SYS-WIP-") else ""
                party = parties.get(suffix)
                if not party and loc_name.startswith("WIP - "):
                    party = parties_by_name.get(loc_name[6:].strip())
                party_id = str(party.id) if party else None
                party_name = party.name if party else (loc_name[6:].strip() if loc_name.startswith("WIP - ") else (loc_name or "External Processor"))
                entry = wip_by_party_dict.setdefault(b.location_id, {
                    "partyId": party_id,
                    "partyName": party_name,
                    "locationCode": loc_code,
                    "locationName": loc_name,
                    "qty": ZERO,
                    "value": ZERO,
                })
                entry["qty"] += on_hand
                entry["value"] += val

        wip_by_party = [
            {
                "partyId": v["partyId"],
                "partyName": v["partyName"],
                "locationCode": v["locationCode"],
                "locationName": v["locationName"],
                "qty": float(v["qty"]),
                "value": float(round2(v["value"])),
            }
            for v in wip_by_party_dict.values()
        ]

        total_on_hand = sum(role_qty.values(), ZERO)
        total_damaged = sum((D(b.damaged) for b in balance_rows), ZERO)

        good_qty, good_val = role_qty["GOOD"], role_val["GOOD"]
        wip_qty, wip_val = role_qty["EXTERNAL_WIP"], role_val["EXTERNAL_WIP"]
        qc_hold_qty, qc_hold_val = role_qty["QC_HOLD"], role_val["QC_HOLD"]
        reprocess_qty, reprocess_val = role_qty["REPROCESS"], role_val["REPROCESS"]
        second_qty, second_val = role_qty["SECOND_QUALITY"], role_val["SECOND_QUALITY"]
        rejected_qty, rejected_val = role_qty["REJECT"], role_val["REJECT"]

        # Taka-level analysis of GOOD stock for real Grey / Finished / Reserved partitioning (Phase 2.3)
        good_takas = LotTaka.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            available_quantity__gt=0,
            item_id__in=item_ids,
        )
        if godown_id:
            good_takas = good_takas.filter(location_id=godown_id)

        reserved_qty = ZERO
        reserved_val = ZERO
        finished_qty = ZERO
        finished_val = ZERO
        grey_qty = ZERO
        grey_val = ZERO

        good_takas_list = [lt for lt in good_takas if location_role.get(lt.location_id, "GOOD") == "GOOD"]
        taka_on_hand_by_item_loc = {}
        for lt in good_takas_list:
            taka_on_hand_by_item_loc[(lt.item_id, lt.location_id)] = (
                taka_on_hand_by_item_loc.get((lt.item_id, lt.location_id), ZERO) + D(lt.available_quantity)
            )
            lt_qty = D(lt.available_quantity)
            lt_cost = D(lt.rate) if lt.rate else get_item_cost(lt.item_id)
            lt_val = lt_qty * lt_cost

            if lt.status == "Reserved":
                reserved_qty += lt_qty
                reserved_val += lt_val
            elif (
                lt.status == "Finished"
                or getattr(item_map.get(lt.item_id), "fabric_stage", "") == "Finished"
                or (lt.source_document_type in ("JobWorkInward", "JWOInward", "Production"))
                or lt.parent_taka_id is not None
            ):
                finished_qty += lt_qty
                finished_val += lt_val
            else:
                grey_qty += lt_qty
                grey_val += lt_val

        for b in balance_rows:
            if location_role.get(b.location_id, "GOOD") != "GOOD":
                continue
            loose = D(b.on_hand) - taka_on_hand_by_item_loc.get((b.item_id, b.location_id), ZERO)
            if loose > ZERO:
                loose_val = loose * wac(b)
                it = item_map.get(b.item_id)
                if getattr(it, "fabric_stage", "") == "Finished" or "finished" in (it.fabric_quality or "").lower():
                    finished_qty += loose
                    finished_val += loose_val
                else:
                    grey_qty += loose
                    grey_val += loose_val

        # Wastage = FAULTY (damaged) stock, tracked separately from on_hand.
        wastage_qty = total_damaged
        wastage_val = sum((D(b.damaged) * wac(b) for b in balance_rows if D(b.damaged) > ZERO), ZERO)

        # Samples out with customers: issued minus returned. They have left
        # the godown, so they are reported but not part of the stock total.
        sample_issue_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, reference_type="SampleIssue",
        )
        sample_return_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, reference_type="SampleReturn",
        )
        if godown_id:
            sample_issue_qs = sample_issue_qs.filter(location_id=godown_id)
        sample_issued = abs(sample_issue_qs.aggregate(q=Coalesce(Sum("quantity"), Value(Decimal("0.0000")), output_field=QTY))["q"])
        sample_returned = sample_return_qs.filter(
            source_document_id__in=sample_issue_qs.values("id")
        ).aggregate(q=Coalesce(Sum("quantity"), Value(Decimal("0.0000")), output_field=QTY))["q"]
        sample_qty = max(ZERO, sample_issued - sample_returned)
        sample_val = round2(sum(
            (abs(D(m.quantity)) * D(m.unit_cost or get_item_cost(m.item_id)) for m in sample_issue_qs),
            ZERO,
        ) * (sample_qty / sample_issued if sample_issued > ZERO else ZERO))

        # Customer returns posted (all conditions) -- informational; the goods
        # themselves are inside the location categories above.
        ret_lines = SalesReturnLine.objects.filter(
            client_id=client_id,
            sales_return__status="Posted",
            sales_return__deleted_at__isnull=True,
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

        total_stock_qty = total_on_hand
        total_stock_val = sum(role_val.values(), ZERO)

        # Prior month comparison (ledger position at the end of last month).
        prev_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, movement_date__lte=last_day_prev_month,
        )
        if godown_id:
            prev_qs = prev_qs.filter(location_id=godown_id)
        prev_total = prev_qs.exclude(type="FAULTY").aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        prev_good = prev_qs.exclude(type="FAULTY").exclude(
            location__code__in=stock.BLOCKED_LOCATION_CODES
        ).aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]

        def calc_pct_change(curr, prev):
            curr_f = float(curr)
            prev_f = float(prev)
            if prev_f > 0:
                return round(((curr_f - prev_f) / prev_f) * 100, 1)
            return None  # no prior stock -- a percentage would be meaningless

        total_change_pct = calc_pct_change(total_stock_qty, prev_total)
        good_change_pct = calc_pct_change(good_qty, prev_good)

        tot_q = float(total_stock_qty) or 1.0
        tot_v = float(total_stock_val) or 1.0

        def dist(name, qty, val, color):
            return {
                "name": name,
                "qty": float(qty),
                "value": float(round2(val)),
                "pct": round((float(qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": color,
            }

        # The slices partition on-hand stock exactly.
        dist_items = [
            dist("Grey Fabric", grey_qty, grey_val, "#6366f1"),
            dist("Finished Fabric", finished_qty, finished_val, "#2563eb"),
            dist("External WIP", wip_qty, wip_val, "#8b5cf6"),
            dist("QC Hold", qc_hold_qty, qc_hold_val, "#94a3b8"),
            dist("Reprocessing", reprocess_qty, reprocess_val, "#f59e0b"),
            dist("Second Quality", second_qty, second_val, "#06b6d4"),
            dist("Rejected", rejected_qty, rejected_val, "#f43f5e"),
        ]

        # Stock Trend: last 6 months, the ledger position at each month end
        # split by location role.
        trend_months = []
        curr_m = today.replace(day=1)
        code_role = {code: role for code, role in role_by_code.items()}
        for i in range(5, -1, -1):
            y = curr_m.year
            m = curr_m.month - i
            while m <= 0:
                m += 12
                y -= 1
            next_m = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
            m_end = next_m - timedelta(days=1)

            m_mov = StockMovement.objects.filter(
                client_id=client_id, item_id__in=item_ids, movement_date__lte=m_end,
            ).exclude(type="FAULTY")
            if godown_id:
                m_mov = m_mov.filter(location_id=godown_id)
            by_role = {"GOOD": ZERO, "EXTERNAL_WIP": ZERO, "QC_HOLD": ZERO, "REPROCESS": ZERO, "SECOND_QUALITY": ZERO, "REJECT": ZERO}
            for row in m_mov.values("location__code").annotate(
                qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
            ):
                loc_c = row["location__code"] or ""
                r = role_by_code.get(loc_c) or ("EXTERNAL_WIP" if loc_c.startswith("SYS-WIP-") else "GOOD")
                by_role[r] += D(row["qty"])
            trend_months.append({
                "month": m_end.strftime("%b"),
                "year": m_end.year,
                "date": m_end.isoformat(),
                "good": float(max(ZERO, by_role["GOOD"])),
                "greyWip": float(max(ZERO, by_role["QC_HOLD"] + by_role["EXTERNAL_WIP"])),
                "reprocessing": float(max(ZERO, by_role["REPROCESS"])),
                "secondQuality": float(max(ZERO, by_role["SECOND_QUALITY"])),
                "wastage": float(max(ZERO, by_role["REJECT"])),
                "total": float(max(ZERO, sum(by_role.values(), ZERO))),
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

        category_wise_stock = []
        for name, data in sorted(cat_stock.items(), key=lambda x: x[1]["qty"], reverse=True):
            category_wise_stock.append({
                "category": name,
                "qty": float(data["qty"]),
                "value": float(round2(data["value"])),
                "pct": round((float(data["qty"]) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
            })

        # Stock by Godown -- each location's on-hand lands in exactly one
        # category (its role); wastage is the damaged (FAULTY) figure.
        stock_by_godown = []
        locations_qs = Location.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if godown_id:
            locations_qs = locations_qs.filter(id=godown_id)
        loc_map = {loc.id: loc.name for loc in locations_qs}
        loc_balances = {}
        for b in balance_rows:
            lb = loc_balances.setdefault(b.location_id, {
                "total": ZERO, "good": ZERO, "externalWip": ZERO, "wastage": ZERO, "reprocess": ZERO,
                "second": ZERO, "rejected": ZERO, "grey": ZERO, "val": ZERO,
            })
            role = location_role.get(b.location_id, "GOOD")
            on_hand = D(b.on_hand)
            lb["total"] += on_hand
            lb["wastage"] += D(b.damaged)
            lb["val"] += on_hand * wac(b)
            key = {
                "GOOD": "good",
                "EXTERNAL_WIP": "externalWip",
                "QC_HOLD": "grey",
                "REPROCESS": "reprocess",
                "SECOND_QUALITY": "second",
                "REJECT": "rejected",
            }.get(role, "good")
            lb[key] = lb.get(key, ZERO) + on_hand

        for lid, lname in loc_map.items():
            lb = loc_balances.get(lid)
            if lb is None:
                continue
            stock_by_godown.append({
                "locationId": str(lid),
                "godown": lname,
                "totalStock": float(lb["total"]),
                "totalValue": float(round2(lb["val"])),
                "good": float(lb.get("good", ZERO)),
                "externalWip": float(lb.get("externalWip", ZERO)),
                "wastage": float(lb.get("wastage", ZERO)),
                "reprocess": float(lb.get("reprocess", ZERO)),
                "secondQuality": float(lb.get("second", ZERO)),
                "rejected": float(lb.get("rejected", ZERO)),
                "greyWip": float(lb.get("grey", ZERO)),
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
            goods_receipt__qc_status__in=["Approved", "Partially Approved"]
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
            .order_by("-movement_date", "-created_at")
        )
        if godown_id:
            recent_mov_qs = recent_mov_qs.filter(location_id=godown_id)

        type_category = {
            "FAULTY": "Wastage",
            "TRANSFER_IN": "Transfer",
            "TRANSFER_OUT": "Transfer",
            "QC_PASS": "QC",
            "QC_HOLD": "QC",
            "QC_REPROCESS": "QC",
            "QC_REJECT": "QC",
            "QC_SECOND_QUALITY": "QC",
            "SAMPLE_ISSUE": "Sample",
        }
        recent_movements = []
        for m in recent_mov_qs[:12]:
            qty_val = float(m.effective_quantity)
            if m.type in type_category:
                category_label = type_category[m.type]
            elif m.type.startswith("SALES_RETURN"):
                category_label = "Customer Return"
            elif m.type.startswith("SAMPLE_RETURN") or m.reference_type in ("SampleIssue", "SampleReturn"):
                category_label = "Sample"
            else:
                category_label = "Inward" if qty_val > 0 else "Outward"

            recent_movements.append({
                "id": str(m.id),
                "date": m.movement_date.strftime("%d %b %Y"),
                "type": "GRN" if m.type == "PURCHASE" else m.type.replace("_", " ").title(),
                "refNo": m.reference_number,
                "fabric": m.item.fabric_quality or m.item.name,
                "qty": abs(qty_val),
                "category": category_label,
                "godown": m.location.name if m.location else None,
            })

        def per_unit(rows, qty_of):
            totals = {}
            for row in rows:
                label = item_unit.get(row.item_id)
                if label is None:
                    continue
                totals[label] = totals.get(label, ZERO) + D(qty_of(row))
            return [{"uom": k, "qty": float(round4(v))} for k, v in sorted(totals.items())]

        def card(qty, val, change=None, of_total=True):
            return {
                "qty": float(qty),
                "uom": dominant_uom,
                "value": float(round2(val)),
                "pct": round((float(qty) / tot_q) * 100, 1) if (of_total and float(total_stock_qty) > 0) else None,
                "changePct": change,
            }

        summary_cards = {
            "totalStock": card(total_stock_qty, total_stock_val, total_change_pct),
            "goodStock": card(good_qty, good_val, good_change_pct),
            "greyStock": card(grey_qty, grey_val),
            "finishedStock": card(finished_qty, finished_val),
            "reservedStock": card(reserved_qty, reserved_val),
            "externalWipStock": card(wip_qty, wip_val),
            "wipStock": card(wip_qty, wip_val),
            # Samples are out with customers and returns are already inside
            # the location categories: neither is a share of on-hand stock.
            "sampleStock": card(sample_qty, sample_val, of_total=False),
            "wastageStock": card(wastage_qty, wastage_val, of_total=False),
            "reprocessingStock": card(reprocess_qty, reprocess_val),
            "secondQualityStock": card(second_qty, second_val),
            "customerReturnStock": card(customer_return_qty, customer_return_val, of_total=False),
            "qcHoldStock": card(qc_hold_qty, qc_hold_val),
            "rejectedStock": card(rejected_qty, rejected_val),
        }

        # 10 Real Location/Status-based Stock Buckets (Phase 2.3)
        stock_buckets = {
            "grey": card(grey_qty, grey_val),
            "externalWip": {
                **card(wip_qty, wip_val),
                "byParty": wip_by_party,
            },
            "qcHold": card(qc_hold_qty, qc_hold_val),
            "reprocess": card(reprocess_qty, reprocess_val),
            "finished": card(finished_qty, finished_val),
            "reserved": card(reserved_qty, reserved_val),
            "secondQuality": card(second_qty, second_val),
            "reject": card(rejected_qty, rejected_val),
            "sample": card(sample_qty, sample_val, of_total=False),
            "customerReturn": card(customer_return_qty, customer_return_val, of_total=False),
        }

        # Per-item figures for the quality table and the records below.
        items_with_stock = list(item_map.values())
        stock.annotate_items_with_stock(client_id, items_with_stock)
        sample_out_by_item = {}
        for m in sample_issue_qs:
            sample_out_by_item[m.item_id] = sample_out_by_item.get(m.item_id, ZERO) + abs(D(m.quantity))
        for row in sample_return_qs.filter(source_document_id__in=sample_issue_qs.values("id")).values("item_id").annotate(q=Sum("quantity")):
            sample_out_by_item[row["item_id"]] = sample_out_by_item.get(row["item_id"], ZERO) - D(row["q"] or 0)
        return_by_item = {
            row["item_id"]: D(row["q"] or 0)
            for row in ret_lines.values("item_id").annotate(q=Sum("returned_qty"))
        }

        # 1. Quality-wise stock breakdown (for Fabric Stock Overview table)
        quality_stock = []
        quality_groups = {}
        for item in item_map.values():
            quality_groups.setdefault(item.fabric_quality or item.name, []).append(item)
        for q_name, group in sorted(quality_groups.items(), key=lambda kv: kv[0] or ""):
            ids = {i.id for i in group}
            by_role = {role: ZERO for role in role_qty}
            damaged = ZERO
            for b in balance_rows:
                if b.item_id in ids:
                    by_role[location_role.get(b.location_id, "GOOD")] += D(b.on_hand)
                    damaged += D(b.damaged)
            total = sum(by_role.values(), ZERO)
            if total == ZERO and damaged == ZERO:
                continue
            quality_stock.append({
                "id": str(group[0].id),
                "qualityCode": group[0].sku if len(group) == 1 else None,
                "qualityName": q_name,
                "goodStock": float(round4(by_role["GOOD"])),
                "sample": float(round4(max(ZERO, sum((sample_out_by_item.get(i, ZERO) for i in ids), ZERO)))),
                "wastage": float(round4(damaged)),
                "reprocess": float(round4(by_role["REPROCESS"])),
                "secondQuality": float(round4(by_role["SECOND_QUALITY"])),
                "return": float(round4(sum((return_by_item.get(i, ZERO) for i in ids), ZERO))),
                "qcHold": float(round4(by_role["QC_HOLD"])),
                "rejected": float(round4(by_role["REJECT"])),
                "total": float(round4(total)),
            })

        # 2. Sample Stock Records
        sample_issues_qs = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type="SampleIssue",
            quantity__lt=0,
        ).select_related("item", "location", "lot_taka").order_by("-movement_date", "-created_at")

        sample_returns_all = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type="SampleReturn",
            quantity__gt=0,
        ).select_related("location").order_by("movement_date", "created_at")

        return_map = {}
        return_history_map = {}
        for r in sample_returns_all:
            s_doc_id = str(r.source_document_id) if r.source_document_id else ""
            if not s_doc_id:
                continue
            return_map[s_doc_id] = return_map.get(s_doc_id, ZERO) + D(r.quantity or 0)
            note = r.notes or ""
            condition = note[1:note.index("]")] if note.startswith("[") and "]" in note else None
            return_history_map.setdefault(s_doc_id, []).append({
                "id": str(r.id),
                "returnNo": r.reference_number,
                "qty": float(r.quantity),
                "uom": r.uom,
                "date": r.movement_date.strftime("%d-%m-%Y"),
                "condition": condition,
                "location": r.location.name if r.location_id else None,
                "remarks": note[note.index("]") + 1:].strip() if condition else note,
            })

        sample_records = []
        for m in sample_issues_qs[:100]:
            it = m.item
            qty_val = abs(D(m.quantity))
            ret_val = return_map.get(str(m.id), ZERO)
            rem_val = max(ZERO, qty_val - ret_val)

            cust_name = None
            expected_date = ""
            purpose = m.notes or ""
            if m.notes and m.notes.strip().startswith("{") and m.notes.strip().endswith("}"):
                try:
                    meta = json.loads(m.notes)
                    cust_name = meta.get("customer") or None
                    expected_date = meta.get("expectedReturnDate") or ""
                    purpose = meta.get("purpose") or ""
                except ValueError:
                    pass

            if ret_val <= ZERO:
                s_status = "Issued"
            elif rem_val <= ZERO:
                s_status = "Returned"
            else:
                s_status = "Partial"
            if expected_date and rem_val > ZERO:
                try:
                    if date.fromisoformat(expected_date) < today:
                        s_status = "Overdue"
                except ValueError:
                    pass

            sample_records.append({
                "id": str(m.id),
                "issueNo": m.reference_number,
                "itemCode": it.sku if it else None,
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": m.batch_number or None,
                "takaId": (m.lot_taka.taka_id or m.lot_taka.barcode) if m.lot_taka_id else None,
                "locationId": str(m.location_id),
                "issuedQty": float(qty_val),
                "returnedQty": float(ret_val),
                "remainingQty": float(rem_val),
                "customer": cust_name,
                "issueDate": m.movement_date.strftime("%d-%m-%Y"),
                "rawDate": m.movement_date.isoformat(),
                "expectedReturnDate": expected_date,
                "availableGoodStock": float(getattr(item_map.get(m.item_id), "available_qty", ZERO) or ZERO),
                "status": s_status,
                "uom": m.uom or (it.uom if it else None),
                "remarks": purpose,
                "history": return_history_map.get(str(m.id), []),
            })

        # 3. Wastage / Reject Stock Records: FAULTY (damaged) movements plus
        # everything routed to the Reject location.
        wastage_records = []
        waste_movs = (
            StockMovement.objects.filter(client_id=client_id, item_id__in=item_ids)
            .filter(Q(type="FAULTY") | Q(location__code=stock.SYSTEM_LOCATIONS["REJECT"][0], quantity__gt=0))
            .select_related("item", "location")
            .order_by("-movement_date", "-created_at")
        )
        if godown_id:
            waste_movs = waste_movs.filter(location_id=godown_id)
        for m in waste_movs[:50]:
            it = m.item
            wastage_records.append({
                "id": str(m.id),
                "itemCode": it.sku if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": m.batch_number or None,
                "quantity": abs(float(m.effective_quantity)),
                "uom": m.uom or (it.uom if it else None),
                "reason": m.notes or None,
                "type": m.type,
                "sourceDoc": m.reference_number,
                "date": m.movement_date.strftime("%d-%m-%Y"),
            })

        # 4. Good Fabric Stock Records (taka-level from LotTaka at good
        # locations, plus balances not held on takas)
        active_takas = list(
            LotTaka.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                status="Active",
                item_id__in=item_ids,
                available_quantity__gt=0,
            ).select_related("item", "location")
        )
        taka_by_item_loc = {}
        for lt in active_takas:
            taka_by_item_loc.setdefault((lt.item_id, lt.location_id), []).append(lt)

        def lot_status(item, qty):
            reorder = D(getattr(item, "reorder_level", 0) or 0)
            if qty <= ZERO:
                return "Nil"
            return "Low Stock" if reorder > ZERO and qty <= reorder else "Available"

        good_records = []
        for b in balance_rows:
            if D(b.on_hand) <= ZERO or location_role.get(b.location_id, "GOOD") != "GOOD":
                continue
            it = b.item
            takas = taka_by_item_loc.get((b.item_id, b.location_id), [])
            on_takas = ZERO
            for lt in takas:
                lt_avail = D(lt.available_quantity)
                on_takas += lt_avail
                good_records.append({
                    "id": str(lt.id),
                    "takaId": lt.taka_id or lt.barcode,
                    "itemCode": it.sku if it else None,
                    "quality": (it.fabric_quality or it.name) if it else None,
                    "design": (it.fabric_design or None) if it else None,
                    "shade": (it.fabric_color or None) if it else None,
                    "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
                    "totalQty": float(lt.received_quantity),
                    "available": float(lt_avail),
                    "reserved": None,
                    "uom": lt.uom or (it.uom if it else None),
                    "godown": lt.location.name if lt.location else None,
                    "status": lot_status(it, lt_avail),
                })
            loose = D(b.on_hand) - on_takas
            if loose > ZERO:
                good_records.append({
                    "id": str(b.id),
                    "takaId": None,
                    "itemCode": it.sku if it else None,
                    "quality": (it.fabric_quality or it.name) if it else None,
                    "design": (it.fabric_design or None) if it else None,
                    "shade": (it.fabric_color or None) if it else None,
                    "lotNo": None,
                    "totalQty": float(loose),
                    "available": float(loose),
                    "reserved": None,
                    "uom": it.uom if it else None,
                    "godown": b.location.name if b.location else None,
                    "status": lot_status(it, loose),
                })

        # 5. Reprocessing Stock Records (reprocess orders)
        reprocess_records = []
        for rp in JWOReprocess.objects.filter(client_id=client_id).select_related(
            "job_work_order", "job_work_order__vendor", "job_work_order__process_plan"
        ).order_by("-date", "-created_at")[:50]:
            jwo = rp.job_work_order
            plan = jwo.process_plan if jwo else None
            sent_q = float(rp.qty or 0)
            rec_q = sent_q if rp.status in ["Received", "Completed"] else 0.0
            reprocess_records.append({
                "id": str(rp.id),
                "reprocessNo": rp.no,
                "itemCode": (plan.fabric_item or None) if plan else None,
                "quality": (plan.fabric_quality or plan.fabric_item or None) if plan else None,
                "design": None,
                "shade": (plan.shade or None) if plan else None,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "pendingQty": max(0.0, sent_q - rec_q),
                "process": (jwo.process or None) if jwo else None,
                "vendor": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "status": rp.status,
            })

        # 6. Customer Return Stock Records
        return_records = []
        for ret in ret_lines.select_related("item", "sales_return", "sales_return__party")[:50]:
            it = ret.item
            return_records.append({
                "id": str(ret.id),
                "returnNo": ret.sales_return.return_number if ret.sales_return_id else None,
                "itemCode": it.sku if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": None,
                "returnQty": float(ret.returned_qty or 0),
                "uom": ret.uom or (it.uom if it else None),
                "reason": getattr(ret, "reason", None) or None,
                "qcStatus": getattr(ret, "condition", None),
                "customer": ret.sales_return.party.name if (ret.sales_return_id and ret.sales_return.party_id) else None,
                "date": ret.sales_return.doc_date.strftime("%d-%m-%Y") if ret.sales_return_id and ret.sales_return.doc_date else "",
            })

        if mixed_units:
            on_hand_by_uom = per_unit(balance_rows, lambda b: b.on_hand)
            damaged_by_uom = per_unit(balance_rows, lambda b: b.damaged)
            for key, card_row in summary_cards.items():
                card_row["qty"] = None
                card_row["changePct"] = None
                card_row["byUom"] = []
            summary_cards["totalStock"]["byUom"] = on_hand_by_uom
            summary_cards["wastageStock"]["byUom"] = damaged_by_uom

        # Takas available for sample issue: good locations only.
        available_lots = [
            {
                "id": str(lt.id),
                "lotTakaId": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "itemId": str(lt.item_id),
                "sku": lt.item.sku if lt.item else None,
                "name": lt.item.name if lt.item else None,
                "quality": (lt.item.fabric_quality or lt.item.name) if lt.item else None,
                "design": (lt.item.fabric_design or None) if lt.item else None,
                "shade": (lt.item.fabric_color or None) if lt.item else None,
                "uom": lt.uom or (lt.item.uom if lt.item else None),
                "locationId": str(lt.location_id),
                "godown": lt.location.name if lt.location else None,
                "available": float(lt.available_quantity),
                "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
            }
            for lt in active_takas
            if location_role.get(lt.location_id, "GOOD") == "GOOD"
        ]
        # Items with stock not held on takas are issued by location only.
        for b in balance_rows:
            if location_role.get(b.location_id, "GOOD") != "GOOD" or D(b.on_hand) <= ZERO:
                continue
            on_takas = sum((D(lt.available_quantity) for lt in taka_by_item_loc.get((b.item_id, b.location_id), [])), ZERO)
            loose = D(b.on_hand) - on_takas
            if loose > ZERO:
                available_lots.append({
                    "id": None,
                    "lotTakaId": None,
                    "takaId": None,
                    "itemId": str(b.item_id),
                    "sku": b.item.sku if b.item else None,
                    "name": b.item.name if b.item else None,
                    "quality": (b.item.fabric_quality or b.item.name) if b.item else None,
                    "design": (b.item.fabric_design or None) if b.item else None,
                    "shade": (b.item.fabric_color or None) if b.item else None,
                    "uom": b.item.uom if b.item else None,
                    "locationId": str(b.location_id),
                    "godown": b.location.name if b.location else None,
                    "available": float(loose),
                    "lotNo": None,
                })

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
                "availableLots": available_lots,
            },
            "stockBuckets": stock_buckets,
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
        action = request.data.get("action") or "issue_sample"
        if action == "issue_sample":
            return self._issue_sample(request)
        if action == "return_sample":
            return self._return_sample(request)
        raise ValidationFailed("Invalid action.", field_errors={"action": ["Expected issue_sample or return_sample."]})

    def _issue_sample(self, request):
        """A sample cut from good stock: SAMPLE_ISSUE out of the godown (and
        off the taka, when one is named or the item is taka-tracked)."""
        client_id = request.client_id
        data = request.data
        field_errors = {}
        item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first()
        if item is None:
            field_errors["itemId"] = ["Choose the item."]
        customer_name = (data.get("customer") or "").strip()
        if not customer_name:
            field_errors["customer"] = ["Who is the sample issued to?"]
        try:
            qty = round4(D(data.get("qty")))
        except Exception:  # noqa: BLE001
            qty = ZERO
        if qty <= ZERO:
            field_errors["qty"] = ["Enter a quantity greater than zero."]
        lot = None
        if data.get("lotTakaId"):
            lot = LotTaka.objects.filter(
                client_id=client_id, id=data.get("lotTakaId"), deleted_at__isnull=True
            ).first()
            if lot is None:
                field_errors["lotTakaId"] = ["That taka does not exist."]
        location = None
        location_id = (lot.location_id if lot else None) or data.get("locationId")
        if location_id:
            location = Location.objects.filter(client_id=client_id, id=location_id).first()
        if location is None:
            field_errors["locationId"] = ["Choose the godown the sample is cut from."]
        if field_errors:
            raise ValidationFailed("Sample issue is incomplete.", field_errors=field_errors)
        if location.code in stock.BLOCKED_LOCATION_CODES:
            raise ValidationFailed(
                "Samples are issued from good stock only.",
                field_errors={"locationId": [f"{location.name} is not a good-stock location."]},
            )

        stock.assert_sufficient_stock(
            client_id=client_id, item=item, quantity=qty, location_id=location.id,
            label=f"{item.name} ({lot.lot_number if lot else location.name})",
        )
        issue_date = timezone.localdate()
        issue_ref = allocate_number(request.user.client, "SMPL", issue_date)
        import uuid as _uuid

        mov = stock.post_movement(
            client_id=client_id,
            item=item,
            location=location,
            type="SAMPLE_ISSUE",
            quantity=-qty,
            unit_cost=D(lot.rate) if lot and lot.rate else item.cost_price,
            reference_type="SampleIssue",
            reference_id=_uuid.uuid4(),
            reference_number=issue_ref,
            batch_number=lot.lot_number if lot else (data.get("lotNo") or None),
            lot_taka=lot,
            movement_date=issue_date,
            notes=json.dumps({
                "customer": customer_name,
                "expectedReturnDate": data.get("expectedReturnDate") or "",
                "purpose": data.get("notes") or "",
            }),
            user=request.user,
        )
        return Response({
            "success": True,
            "message": f"Sample {issue_ref}: {qty} {item.uom} issued to {customer_name}.",
            "movementId": str(mov.id),
            "issueNo": issue_ref,
        })

    def _return_sample(self, request):
        """Route a returned sample by condition (Phase 1.4): Good -> back to the
        godown it came from, Hold -> QC Hold, Reprocess -> Reprocess, Second
        Quality -> Second Quality, Reject/Scrap -> Reject."""
        client_id = request.client_id
        data = request.data
        movement_id = data.get("movementId") or data.get("issueId")
        try:
            return_qty = round4(D(data.get("returnQty")))
        except Exception:  # noqa: BLE001
            return_qty = ZERO
        if return_qty <= ZERO:
            raise ValidationFailed(
                "Return quantity must be greater than 0.",
                field_errors={"returnQty": ["Must be greater than zero."]},
            )
        condition = stock.normalize_condition(data.get("condition"))
        remarks = (data.get("remarks") or "").strip()

        orig_mov = StockMovement.objects.select_related("item", "lot_taka").filter(
            client_id=client_id, id=movement_id, reference_type="SampleIssue"
        ).first()
        if orig_mov is None:
            raise ValidationFailed(
                "Original sample issue not found.", field_errors={"movementId": ["Not found."]}
            )

        already_returned = StockMovement.objects.filter(
            client_id=client_id,
            reference_type="SampleReturn",
            source_document_id=orig_mov.id,
            quantity__gt=0,
        ).aggregate(tot=Sum("quantity"))["tot"] or ZERO
        remaining = abs(D(orig_mov.quantity)) - D(already_returned)
        if return_qty > remaining:
            raise ValidationFailed(
                f"Return quantity ({return_qty}) cannot exceed the outstanding "
                f"quantity ({remaining} {orig_mov.uom or ''}).",
                field_errors={"returnQty": [f"At most {remaining}."]},
            )

        destination = stock.condition_location_id(client_id, condition, orig_mov.location_id)
        _, lot_qc_status = stock.CONDITION_ROUTING[condition]
        movement_type = {
            "Good": "SAMPLE_RETURN",
            "Hold": "SAMPLE_RETURN_HOLD",
            "Reprocess": "SAMPLE_RETURN_REPROCESS",
            "Second Quality": "SAMPLE_RETURN_SECOND_QUALITY",
            "Reject": "SAMPLE_RETURN_REJECT",
        }[condition]
        return_no = allocate_number(request.user.client, "SMPR")
        common = dict(
            client_id=client_id,
            reference_type="SampleReturn",
            reference_id=orig_mov.id,
            reference_number=return_no,
            source_document_type="StockMovement",
            source_document_id=orig_mov.id,
            movement_date=timezone.localdate(),
            notes=f"[{condition}] {remarks}".strip(),
            user=request.user,
        )
        if orig_mov.lot_taka_id:
            _, ret_mov = stock.receive_into_lot(
                lot=orig_mov.lot_taka, quantity=return_qty, location_id=destination,
                type=movement_type, qc_status=lot_qc_status, split_suffix="SR",
                unit_cost=orig_mov.unit_cost, **common,
            )
        else:
            ret_mov = stock.post_movement(
                item=orig_mov.item_id, location=destination, type=movement_type,
                quantity=return_qty, unit_cost=orig_mov.unit_cost,
                batch_number=orig_mov.batch_number, **common,
            )
        return Response({
            "success": True,
            "message": f"Sample return {return_no}: {return_qty} {orig_mov.uom or ''} routed as {condition}.",
            "movementId": str(ret_mov.id),
            "returnNo": return_no,
            "condition": condition,
            "remainingOutstanding": float(remaining - return_qty),
        })


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
        dominant_uom = first_uom or None

        lots_count = lines_qs.values("batch_number").distinct().count()
        takas_count = lines_qs.count()

        # Inspected GRNs (qc_status != 'Pending')
        inspected_lines = lines_qs.exclude(goods_receipt__qc_status="Pending")
        inspected_qty = inspected_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        inspected_lots = inspected_lines.values("batch_number").distinct().count()

        good_qty = lines_qs.filter(goods_receipt__qc_status__in=["Approved", "Partially Approved"]).aggregate(
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
            m_good = m_lines.filter(goods_receipt__qc_status__in=["Approved", "Partially Approved"]).aggregate(
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
            s_name = s["goods_receipt__party__name"]
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
            reason = _qc_remarks(l.goods_receipt) or "No reason recorded"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(l.weighed_qty or l.received_qty or 0)
            reason_map[reason]["lots"].add(l.goods_receipt_id)

        for inw in inwards_qs.filter(status="On Hold"):
            reason = f"Challan {inw.challan_no}" if inw.challan_no else "No reason recorded"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(inw.qty or 0)
            reason_map[reason]["lots"].add(inw.id)

        hold_breakdown = [
            {"reason": r, "qty": float(round2(data["qty"])), "lots": len(data["lots"])}
            for r, data in reason_map.items()
        ]

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
                "supplier": r.party.name if r.party else None,
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else "—",
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "takaRoll": len(r_lines) or 1,
                "received": rec_qty,
                "inspected": rec_qty if not is_pending else 0,
                "good": (rec_qty - rej_qty) if is_good else 0,
                "hold": (rec_qty - rej_qty) if is_hold else 0,
                "reprocess": rec_qty if is_rep else 0,
                "rejected": rej_qty if not is_rej else rec_qty,
                "status": "Completed" if is_good else ("On Hold" if is_hold else r.qc_status),
                "defectReason": _qc_remarks(r),
                "inspector": r.qc_by.name if r.qc_by else None,
                "uom": line0.uom if line0 and line0.uom else None,
            })

        for inw in inwards_qs.order_by("-date", "-created_at")[:20]:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            it_code = (getattr(plan, "fabric_item", None) or None)
            it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
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
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "takaRoll": 1,
                "received": float(inw.qty),
                "inspected": float(inw.accepted + inw.rejected) if not is_pending else 0,
                "good": float(inw.accepted) if is_good else 0,
                "hold": float(inw.qty) if is_hold else 0,
                "reprocess": float(inw.qty) if is_rep else 0,
                "rejected": float(inw.rejected),
                "status": "Completed" if is_good else ("On Hold" if is_hold else inw.status),
                "defectReason": f"Challan: {inw.challan_no}" if inw.challan_no else None,
                "inspector": None,
                "uom": None,
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
                        "supplier": r.party.name if r.party else None,
                        "itemCode": item.sku if item else "—",
                        "itemName": item.name if item else None,
                        "quality": item_qual,
                        "design": item_des,
                        "shade": item_shd,
                        "lotNo": line.batch_number or None,
                        "takaRoll": idx + 1,
                        "receivedQty": rec_q,
                        "pendingQty": pend_q,
                        "uom": line.uom or (item.uom if item else None),
                        "days": days_pending,
                        "status": r.qc_status,
                        "inspector": r.qc_by.name if r.qc_by else None,
                        "shift": None,
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
                v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
                it_code = (getattr(plan, "fabric_item", None) or None)
                it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
                it_qual = getattr(plan, "fabric_quality", "") or ""
                it_des = ""
                it_shd = getattr(plan, "shade", "") or ""
                lot_no = (getattr(plan, "grey_lot_no", None) or None)

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
                    "uom": None,
                    "days": days_pending,
                    "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                    "inspector": "",
                    "shift": None,
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
        shift = request.data.get("shift") or ""
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
    Submit the QC result of a goods receipt: the stock still at QC Hold is
    moved to Good / Second Quality / Reprocess / Reject (Hold stays) with real
    movements, across every GRN line.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    def post(self, request):
        from . import qc as qc_service

        client_id = request.client_id
        receipt_id = request.data.get("receiptId")
        bill_id = request.data.get("billId")
        quantities = qc_service.parse_quantities(request.data)

        with transaction.atomic():
            if not receipt_id and bill_id:
                # Goods not yet received on the bill: receive them (into QC
                # Hold) first, then apply this result to the new GRN.
                from apps.purchase import services as purchase_services

                bill = PurchaseBill.objects.filter(
                    client_id=client_id, id=bill_id, deleted_at__isnull=True
                ).first()
                if bill is None:
                    raise ValidationFailed("Bill not found.", field_errors={"billId": ["Not found."]})
                if not bill.goods_received:
                    result = purchase_services.receive_bill_goods(
                        bill, lines_payload=[], qc_status=None, user=request.user,
                    )
                    receipt_id = result["receipt"].id
                else:
                    receipt = GoodsReceipt.objects.filter(
                        client_id=client_id, purchase_bill=bill, deleted_at__isnull=True
                    ).order_by("-created_at").first()
                    receipt_id = receipt.id if receipt else None

            receipt = GoodsReceipt.objects.filter(
                client_id=client_id, id=receipt_id, deleted_at__isnull=True
            ).first() if receipt_id else None
            if receipt is None:
                raise ValidationFailed(
                    "Goods receipt not found.", field_errors={"receiptId": ["Not found."]}
                )

            jwo = None
            jwo_id = request.data.get("jwoId") or request.data.get("jobWorkOrderId")
            if jwo_id:
                jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first()
                if jwo is None:
                    raise ValidationFailed("Job work order not found.", field_errors={"jwoId": ["Not found."]})

            result = qc_service.dispose_receipt(
                receipt,
                quantities=quantities,
                line_quantities=request.data.get("lines") or None,
                user=request.user,
                remarks=request.data.get("remarks") or None,
                job_work_order=jwo,
            )

        return Response({
            "success": True,
            "status": result["verdict"],
            "stockPosted": result["stockPosted"],
            "message": (
                f"QC recorded for {receipt.grn_number}: {result['verdict']}."
                + ("" if result["stockPosted"] else " This receipt has no stock posted, so no stock moved.")
            ),
            **{f"{k}Qty": float(v) for k, v in result["quantities"].items()},
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
            .select_related("party", "purchase_bill", "purchase_order", "qc_by", "created_by")
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

            if meta and "goodQty" in meta:
                # Recorded by the QC disposition service (cumulative figures).
                good_qty = float(meta.get("goodQty") or 0) + float(meta.get("secondQualityQty") or 0)
                hold_qty = float(meta.get("holdQty") or 0)
                rep_qty = float(meta.get("reprocessQty") or 0)
                rej_qty = float(meta.get("rejectedQty") or 0)
                insp_qty = good_qty + hold_qty + rep_qty + rej_qty
            elif r.qc_status == "Pending":
                insp_qty = good_qty = hold_qty = rep_qty = 0.0
                rej_qty = 0.0
            else:
                # Older receipts carry only the verdict and the per-line
                # rejected quantity; nothing else was recorded.
                insp_qty = rec_qty
                good_qty = rec_qty - rej_qty if r.qc_status == "Approved" else 0.0
                hold_qty = rec_qty - rej_qty if r.qc_status == "On Hold" else 0.0
                rep_qty = rec_qty - rej_qty if r.qc_status == "Rework" else 0.0

            measurements = meta.get("measurements") or []
            if not measurements and r_lines:
                for idx, l in enumerate(r_lines):
                    l_item = l.item
                    measurements.append({
                        "srNo": idx + 1,
                        "lineId": str(l.id),
                        "takaNo": None,
                        "actualQty": float(l.weighed_qty or l.received_qty or 0),
                        "width": float(l_item.fabric_width) if (l_item and getattr(l_item, "fabric_width", None)) else None,
                        "weight": float(l.weighed_qty) if l.weighed_qty else None,
                        "rejected": float(l.rejected_qty or 0),
                        "remarks": None,
                    })

            created_at = timezone.localtime(r.created_at) if r.created_at else None
            audit_trail = [
                {
                    "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                    "action": "Created",
                    "user": r.created_by.name if r.created_by_id else None,
                    "remarks": f"GRN {r.grn_number} received",
                }
            ]
            if r.qc_at:
                audit_trail.append({
                    "dateTime": timezone.localtime(r.qc_at).strftime("%d-%m-%Y %H:%M"),
                    "action": display_status,
                    "user": r.qc_by.name if r.qc_by else None,
                    "remarks": _qc_remarks(r),
                })

            records.append({
                "id": str(r.id),
                "qcNo": r.grn_number,
                "receiptId": str(r.id),
                "date": r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "",
                "rawDate": r.receipt_date.isoformat() if r.receipt_date else "",
                "sourceType": "Purchase" if (r.purchase_bill_id or r.purchase_order_id) else "Job Work",
                "refNo": r.grn_number,
                "grnNo": r.grn_number,
                "supplier": r.party.name if r.party else None,
                "itemCode": item.sku if item else "—",
                "itemName": item.name if item else None,
                "quality": item_qual or (item.name if item else ""),
                "design": item_des or None,
                "shade": item_shd or None,
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "takaRoll": len(r_lines) or 1,
                "receivedQty": rec_qty,
                "inspectedQty": insp_qty,
                "goodQty": good_qty,
                "holdQty": hold_qty,
                "reprocessQty": rep_qty,
                "rejectedQty": rej_qty,
                "uom": line0.uom if line0 and line0.uom else None,
                "status": display_status,
                "inspector": r.qc_by.name if r.qc_by else None,
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
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            it_code = (getattr(plan, "fabric_item", None) or None)
            it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
            it_qual = getattr(plan, "fabric_quality", "") or ""
            it_des = ""
            it_shd = getattr(plan, "shade", "") or ""
            lot_no = (getattr(plan, "grey_lot_no", None) or None)

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

            created_at = timezone.localtime(inw.created_at) if inw.created_at else None
            audit_trail = [
                {
                    "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                    "action": "Created",
                    "user": inw.created_by.name if inw.created_by_id else None,
                    "remarks": f"Job Work Inward {inw.no} received",
                }
            ]
            if inw.status in ["Completed", "Approved"] and inw.updated_at:
                audit_trail.append({
                    "dateTime": timezone.localtime(inw.updated_at).strftime("%d-%m-%Y %H:%M"),
                    "action": "Completed",
                    "user": inw.updated_by.name if inw.updated_by_id else None,
                    "remarks": None,
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
                "uom": None,
                "status": disp_status,
                "inspector": inw.updated_by.name if inw.updated_by_id else None,
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
                "statuses": ["All", "Completed", "Partially Approved", "Pending", "On Hold", "Rework", "Rejected"],
                "suppliers": all_suppliers,
                "qualities": qualities,
                "designs": designs,
                "shades": shades,
            },
        })

    def post(self, request):
        """Manual QC register entry: a goods receipt *record* for inspection.

        It posts no stock (goods received without a bill have no valuation
        document); the QC disposition of such a receipt records the result
        only and says so. Every input is explicit -- no defaults.
        """
        client_id = request.client_id
        data = request.data
        field_errors = {}

        party = None
        if data.get("supplierId"):
            party = Party.objects.filter(client_id=client_id, id=data.get("supplierId"), deleted_at__isnull=True).first()
        elif data.get("supplier"):
            party = Party.objects.filter(client_id=client_id, name=data.get("supplier"), deleted_at__isnull=True).first()
        if party is None:
            field_errors["supplierId"] = ["Choose the supplier."]

        location = None
        if data.get("locationId"):
            location = Location.objects.filter(client_id=client_id, id=data.get("locationId"), deleted_at__isnull=True).first()
        if location is None:
            field_errors["locationId"] = ["Choose the receiving godown."]

        item = None
        if data.get("itemId"):
            item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first()
        elif data.get("itemCode"):
            item = Item.objects.filter(client_id=client_id, sku=data.get("itemCode"), deleted_at__isnull=True).first()
        if item is None:
            field_errors["itemId"] = ["Choose the item."]

        lot_no = (data.get("lotNo") or "").strip()
        if not lot_no:
            field_errors["lotNo"] = ["Enter the lot number."]
        try:
            received_qty = round4(D(data.get("receivedQty")))
        except Exception:  # noqa: BLE001
            received_qty = ZERO
        if received_qty <= ZERO:
            field_errors["receivedQty"] = ["Enter the received quantity."]
        try:
            taka_count = int(data.get("takaRoll") or 0)
        except (TypeError, ValueError):
            taka_count = 0
        if taka_count <= 0:
            field_errors["takaRoll"] = ["Enter the number of takas / rolls."]
        try:
            rec_date = date.fromisoformat(str(data.get("inspectionDate") or data.get("receiptDate") or ""))
        except ValueError:
            rec_date = None
        if rec_date is None:
            field_errors["inspectionDate"] = ["Enter the receipt date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("The QC register entry is incomplete.", field_errors=field_errors)

        with transaction.atomic():
            grn_no = allocate_number(request.user.client, "GRN", rec_date)
            grn = GoodsReceipt.objects.create(
                client_id=client_id,
                grn_number=grn_no,
                party=party,
                receipt_date=rec_date,
                location=location,
                qc_status="Pending",
                created_by=request.user,
            )
            # One line for the received quantity; the taka count is recorded
            # on the line rather than inventing an equal split per taka.
            line_kwargs = dict(
                client_id=client_id,
                goods_receipt=grn,
                item=item,
                uom=item.uom,
                stock_uom=item.uom,
                stock_qty=received_qty,
                ordered_qty=received_qty,
                received_qty=received_qty,
                batch_number=lot_no,
            )
            if "roll_count" in {f.name for f in GoodsReceiptLine._meta.get_fields()}:
                line_kwargs["roll_count"] = taka_count
            GoodsReceiptLine.objects.create(**line_kwargs)

        return Response({
            "success": True,
            "message": f"QC inspection record {grn_no} created (no stock posted).",
            "receiptId": str(grn.id),
            "grnNo": grn_no,
        }, status=status.HTTP_201_CREATED)


class QCDispositionView(APIView):
    """
    GET  /inventory/qc/disposition/  -- receipts (and job-work inwards) to disposition
    POST /inventory/qc/disposition/  -- save a draft, or approve and move stock

    Approval goes through ``apps.inventory.qc.dispose_receipt``: every GRN line
    and every taka still at QC Hold is moved to Good / Second Quality /
    Reprocess / Reject with real movements; Hold stays at QC Hold.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    def _receipt_row(self, r):
        from . import qc as qc_service

        r_lines = list(r.lines.all())
        line0 = r_lines[0] if r_lines else None
        item = line0.item if line0 else None
        meta = _qc_meta(r)
        pending = qc_service.pool_quantity(r)
        has_stock = qc_service.receipt_has_stock(r)
        tot_rec = sum(float(l.stock_qty or l.received_qty or 0) for l in r_lines)
        # What the form must account for now: what is still at QC Hold, or
        # (for a record-only receipt) the received quantity.
        to_inspect = float(pending) if has_stock else (tot_rec if r.qc_status in ("Pending", "On Hold") else 0.0)
        draft = meta.get("draft") or {}
        return {
            "id": str(r.id),
            "qcNo": r.grn_number,
            "grnNo": r.grn_number,
            "date": r.receipt_date.isoformat() if r.receipt_date else "",
            "status": "Completed" if r.qc_status == "Approved" else r.qc_status,
            "sourceType": "Purchase (GRN)",
            "supplier": r.party.name if r.party_id else None,
            "itemCode": item.sku if item else None,
            "itemName": item.name if item else None,
            "quality": getattr(item, "fabric_quality", None) or None,
            "design": getattr(item, "fabric_design", None) or None,
            "shade": getattr(item, "fabric_color", None) or None,
            "lotNo": line0.batch_number if line0 else None,
            # Takas received on the GRN (QC splits carry another source type).
            "takaRoll": LotTaka.objects.filter(
                client_id=r.client_id, source_document_id=r.id, source_document_type="GRN",
                deleted_at__isnull=True,
            ).count(),
            "receivedQty": tot_rec,
            "inspectedQty": to_inspect,
            "pendingQcQty": float(pending),
            "stockPosted": has_stock,
            # The form starts from the saved draft, else everything pending
            # is unassigned (0) -- the inspector enters the split.
            "goodQty": float(draft.get("goodQty", 0) or 0),
            "secondQualityQty": float(draft.get("secondQualityQty", 0) or 0),
            "holdQty": float(draft.get("holdQty", 0) or 0),
            "reprocessQty": float(draft.get("reprocessQty", 0) or 0),
            "rejectedQty": float(draft.get("rejectedQty", 0) or 0),
            "previousDisposition": {
                k: meta.get(k) for k in ("goodQty", "secondQualityQty", "reprocessQty", "rejectedQty", "holdQty")
            } if meta.get("cumulative") else None,
            "remarks": (draft.get("remarks") if draft else None) or _qc_remarks(r),
            "lines": [
                {
                    "lineId": str(l.id),
                    "itemCode": l.item.sku if l.item_id else None,
                    "itemName": l.item.name if l.item_id else None,
                    "lotNo": l.batch_number,
                    "uom": l.stock_uom or l.uom,
                    "receivedQty": float(l.stock_qty or l.received_qty or 0),
                    "rejectedQty": float(l.rejected_qty or 0),
                }
                for l in r_lines
            ],
            "measurements": draft.get("measurements") or meta.get("measurements") or [],
        }

    def get(self, request):
        client_id = request.client_id
        receipt_id = request.query_params.get("receipt_id")

        receipts_qs = (
            GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
            .order_by("-receipt_date", "-created_at")
        )
        receipts = list(receipts_qs[:40])
        if receipt_id and not any(str(r.id) == str(receipt_id) for r in receipts):
            receipts += list(receipts_qs.filter(id=receipt_id))

        receipts_list = []
        target_receipt = None
        for r in receipts:
            row = self._receipt_row(r)
            receipts_list.append(row)
            if receipt_id and str(r.id) == str(receipt_id):
                target_receipt = row

        # Job Work Inwards are listed for their QC status; their stock is
        # posted by the job-work module.
        inwards_qs = (
            JWOInward.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        for inw in inwards_qs[:20]:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            rec_q = float(inw.qty or 0)
            inw_obj = {
                "id": str(inw.id),
                "qcNo": inw.no,
                "grnNo": inw.no,
                "date": inw.date.isoformat() if inw.date else "",
                "status": "Completed" if inw.status in ["Approved", "Completed"] else inw.status,
                "sourceType": "Job Work",
                "supplier": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "itemCode": (plan.fabric_item or None) if plan else None,
                "itemName": (plan.fabric_item or plan.fabric_quality or None) if plan else None,
                "quality": (plan.fabric_quality or None) if plan else None,
                "design": None,
                "shade": (plan.shade or None) if plan else None,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "takaRoll": (plan.no_of_rolls if plan else None),
                "inspectedQty": rec_q,
                "receivedQty": rec_q,
                "goodQty": float(inw.accepted) if inw.status in ["Approved", "Completed"] else 0.0,
                "holdQty": 0.0,
                "reprocessQty": 0.0,
                "rejectedQty": float(inw.rejected),
                "remarks": inw.challan_no or None,
                "measurements": [],
            }
            receipts_list.append(inw_obj)
            if receipt_id and str(inw.id) == str(receipt_id):
                target_receipt = inw_obj

        if not target_receipt and receipts_list:
            target_receipt = receipts_list[0]

        return Response({"inspections": receipts_list, "selected": target_receipt})

    def post(self, request):
        from . import qc as qc_service

        client_id = request.client_id
        receipt_id = request.data.get("receiptId")
        if not receipt_id:
            raise ValidationFailed("receiptId is required.", field_errors={"receiptId": ["Required."]})

        is_draft = bool(request.data.get("isDraft", False))
        quantities = qc_service.parse_quantities(request.data)
        remarks = request.data.get("remarks") or ""
        measurements = request.data.get("measurements") or []

        receipt = GoodsReceipt.objects.filter(client_id=client_id, id=receipt_id, deleted_at__isnull=True).first()
        inward = None
        if receipt is None:
            inward = JWOInward.objects.filter(client_id=client_id, id=receipt_id).first()
        if receipt is None and inward is None:
            raise ValidationFailed("Inspection source record not found.", field_errors={"receiptId": ["Not found."]})

        with transaction.atomic():
            if receipt is not None:
                if is_draft:
                    # A draft changes nothing but the saved form: status and
                    # stock stay exactly as they are.
                    meta = _qc_meta(receipt)
                    meta["draft"] = {
                        "goodQty": float(quantities["good"]),
                        "secondQualityQty": float(quantities["second"]),
                        "holdQty": float(quantities["hold"]),
                        "reprocessQty": float(quantities["reprocess"]),
                        "rejectedQty": float(quantities["reject"]),
                        "remarks": remarks,
                        "measurements": measurements,
                        "savedAt": timezone.now().isoformat(),
                    }
                    if not _qc_meta(receipt) and receipt.qc_note:
                        meta["remarks"] = receipt.qc_note
                    receipt.qc_note = json.dumps(meta)
                    receipt.save(update_fields=["qc_note", "updated_at"])
                    return Response({
                        "success": True, "isDraft": True, "status": receipt.qc_status,
                        "message": "Draft saved. Stock is not moved until the disposition is approved.",
                    })

                jwo = None
                jwo_id = request.data.get("jwoId") or request.data.get("jobWorkOrderId")
                if jwo_id:
                    jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first()
                    if jwo is None:
                        raise ValidationFailed("Job work order not found.", field_errors={"jwoId": ["Not found."]})
                result = qc_service.dispose_receipt(
                    receipt,
                    quantities=quantities,
                    line_quantities=request.data.get("lines") or None,
                    user=request.user,
                    remarks=remarks or None,
                    measurements=measurements,
                    job_work_order=jwo,
                )
                message = (
                    f"QC disposition approved ({result['verdict']}). "
                    + (f"{result['movements']} stock movement(s) posted."
                       if result["stockPosted"] else "This receipt has no stock posted, so no stock moved.")
                )
                if result["reprocessOrder"] is not None:
                    message += f" Reprocess order {result['reprocessOrder'].no} created."
                return Response({
                    "success": True, "isDraft": False, "status": result["verdict"],
                    "stockPosted": result["stockPosted"], "message": message,
                })

            # Job work inward: the job-work module owns its stock; this records
            # the QC verdict and, for reprocess, the reprocess order.
            total = sum(quantities.values(), ZERO)
            if not is_draft and total <= ZERO:
                raise ValidationFailed(
                    "Enter the disposition quantities before approving.",
                    field_errors={"goodQty": ["Total must be greater than zero."]},
                )
            if is_draft:
                return Response({
                    "success": True, "isDraft": True, "status": inward.status,
                    "message": "Draft not stored for job-work inwards; nothing changed.",
                })
            verdict = qc_service.verdict_for(
                {k: quantities[k] for k in ("good", "second", "reprocess", "reject")},
                quantities["hold"],
            )
            inward.accepted = quantities["good"] + quantities["second"]
            inward.rejected = quantities["reject"]
            inward.status = verdict
            inward.save(update_fields=["accepted", "rejected", "status", "updated_at"])
            rp_no = None
            if quantities["reprocess"] > ZERO:
                from apps.jobwork.models import JWOReprocess

                if not JWOReprocess.objects.filter(client_id=client_id, inward_no=inward.no).exists():
                    on_date = timezone.localdate()
                    rp_no = allocate_number(request.user.client, "RP", on_date)
                    JWOReprocess.objects.create(
                        client_id=client_id,
                        job_work_order=inward.job_work_order,
                        no=rp_no,
                        inward_no=inward.no,
                        date=on_date,
                        qty=quantities["reprocess"],
                        reason=remarks or f"Reprocess required from {inward.no}",
                        status="Pending",
                        created_by=request.user,
                    )
            return Response({
                "success": True, "isDraft": False, "status": verdict,
                "stockPosted": False,
                "message": f"Job work QC recorded ({verdict})."
                + (f" Reprocess order {rp_no} created." if rp_no else ""),
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
            s_name = r.party.name if r.party else None
            top_def = _qc_remarks(r)
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
            if r.qc_status in ("Approved", "Partially Approved"):
                supplier_map[s_name]["goodQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status == "On Hold":
                supplier_map[s_name]["holdQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status in ["Rework", "Reprocess"]:
                supplier_map[s_name]["reprocessQty"] += rec_q
            supplier_map[s_name]["rejectedQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            if v_name not in supplier_map:
                supplier_map[v_name] = {
                    "supplier": v_name, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0, "topDefect": None,
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
                q_name = getattr(item, "fabric_quality", "") or (item.name if item else None)
                sku = getattr(item, "sku", None)
                if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                    continue
                if q_name not in quality_map:
                    quality_map[q_name] = {
                        "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                        "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                        "primaryDefect": None,
                    }
                rec_q = float(l.weighed_qty or l.received_qty or 0)
                rej_q = float(l.rejected_qty or 0)
                quality_map[q_name]["inspectedQty"] += rec_q
                quality_map[q_name]["lotsCount"] += 1
                if r.qc_status in ("Approved", "Partially Approved"):
                    quality_map[q_name]["goodQty"] += max(0.0, rec_q - rej_q)
                quality_map[q_name]["defectiveQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            q_name = (getattr(plan, "fabric_quality", "") or getattr(plan, "fabric_item", "") or None)
            sku = (getattr(plan, "fabric_item", None) or None)
            if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                continue
            if q_name not in quality_map:
                quality_map[q_name] = {
                    "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                    "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                    "primaryDefect": None,
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
                "supplier": r.party.name if r.party else None,
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else None,
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "qty": rec_q,
                "status": r.qc_status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": _qc_remarks(r),
            })

        for inw in inwards_qs.filter(status__in=["Received", "Pending QC", "Pending", "On Hold"]):
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            rec_q = float(inw.qty or 0)
            days = (today - inw.date).days if inw.date else 0
            aging = "0-3 Days" if days <= 3 else ("4-7 Days" if days <= 7 else ("8-14 Days" if days <= 14 else "15+ Days"))
            hold_pending_report.append({
                "refNo": inw.no,
                "sourceType": "Job Work",
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "supplier": v_name,
                "itemCode": (getattr(plan, "fabric_item", None) or None),
                "itemName": (getattr(plan, "fabric_quality", "") or None),
                "lotNo": (getattr(plan, "grey_lot_no", None) or None),
                "qty": rec_q,
                "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": f"Challan {inw.challan_no}" if inw.challan_no else None,
            })
        hold_pending_report.sort(key=lambda x: x["daysPending"], reverse=True)

        # Report 4: Reprocess History & Recovery
        reprocess_recovery_report = []
        for rp in reprocess_qs:
            jwo = rp.job_work_order
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            plan = jwo.process_plan if jwo else None
            sent_q = float(rp.qty or 0)
            # Only reinspected reprocess orders have a recorded result; until
            # then received/loss are unknown (None), never estimated.
            rp_meta = {}
            if rp.remarks and rp.remarks.strip().startswith("{"):
                try:
                    rp_meta = json.loads(rp.remarks)
                except ValueError:
                    rp_meta = {}
            if rp.status == "Completed" and rp_meta:
                rec_q = float(rp_meta.get("goodQty", 0) or 0) + float(rp_meta.get("holdQty", 0) or 0) + float(rp_meta.get("reprocessQty", 0) or 0)
                loss_q = float(rp_meta.get("rejectedQty", 0) or 0)
                rec_pct = round((rec_q / sent_q) * 100, 1) if sent_q else None
            else:
                rec_q = None
                loss_q = None
                rec_pct = None
            reprocess_recovery_report.append({
                "rpNo": rp.no,
                "sourceRef": rp.inward_no or (jwo.jwo_no if jwo else "—"),
                "vendor": v_name,
                "fabricItem": (getattr(plan, "fabric_quality", "") or None),
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "recoveryPct": rec_pct,
                "lossQty": loss_q,
                "status": rp.status,
                "goodQty": float(rp_meta.get("goodQty", 0) or 0) if rp_meta else None,
                "rejectedQty": loss_q,
                "reason": rp.reason or None,
            })

        # Report 5: Inspector Performance
        inspector_map = {}
        for r in receipts_qs.exclude(qc_status="Pending"):
            insp_name = r.qc_by.name if r.qc_by else "Not recorded"
            if insp_name not in inspector_map:
                inspector_map[insp_name] = {
                    "inspector": insp_name, "inspectionsCount": 0, "inspectedQty": 0.0,
                    "approvedCount": 0, "holdCount": 0, "rejectedCount": 0,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            inspector_map[insp_name]["inspectionsCount"] += 1
            inspector_map[insp_name]["inspectedQty"] += rec_q
            if r.qc_status in ("Approved", "Partially Approved"):
                inspector_map[insp_name]["approvedCount"] += 1
            elif r.qc_status == "On Hold":
                inspector_map[insp_name]["holdCount"] += 1
            elif r.qc_status == "Rejected":
                inspector_map[insp_name]["rejectedCount"] += 1

        for inw in inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]):
            insp_name = inw.updated_by.name if inw.updated_by_id else "Not recorded"
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
            if r.qc_status in ("Approved", "Partially Approved"):
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

        done_rp = [r for r in reprocess_recovery_report if r["receivedQty"] is not None]

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
                (sum(r["receivedQty"] for r in done_rp) / (sum(r["sentQty"] for r in done_rp) or 1.0)) * 100, 1
            ) if done_rp else None,
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
    GET  /inventory/qc/reinspection/  -- reprocess orders and their reinspection
    POST /inventory/qc/reinspection/  -- create (explicit inputs only) or record
                                         the reinspection disposition

    A reprocess order raised by a GRN QC disposition is linked to the goods
    sitting at the Reprocess location (by GRN); its reinspection moves them on
    with real movements. Orders with no such stock record the result only, and
    the response says so.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    @staticmethod
    def _meta(reprocess):
        if reprocess.remarks and reprocess.remarks.strip().startswith("{"):
            try:
                return json.loads(reprocess.remarks)
            except ValueError:
                return {}
        return {}

    @staticmethod
    def _reprocess_units(reprocess):
        """Takas / item quantities of this order's GRN still at Reprocess."""
        receipt = GoodsReceipt.objects.filter(
            client_id=reprocess.client_id, grn_number=reprocess.inward_no, deleted_at__isnull=True
        ).first() if reprocess.inward_no else None
        if receipt is None:
            return None, []
        location = stock.system_location(reprocess.client_id, "REPROCESS")
        units = [
            {"lot": lot, "item_id": lot.item_id, "qty": round4(lot.available_quantity), "line": None,
             "from": location.id}
            for lot in LotTaka.objects.filter(
                client_id=reprocess.client_id, source_document_id=receipt.id,
                location_id=location.id, available_quantity__gt=0, deleted_at__isnull=True,
            ).order_by("created_at")
        ]
        lot_items = {u["item_id"] for u in units}
        for row in StockMovement.objects.filter(
            client_id=reprocess.client_id, source_document_id=receipt.id,
            location_id=location.id, lot_taka__isnull=True,
        ).values("item_id").annotate(q=Sum("quantity")):
            if row["item_id"] not in lot_items and D(row["q"] or 0) > ZERO:
                units.append({"lot": None, "item_id": row["item_id"], "qty": round4(row["q"]),
                              "line": None, "from": location.id})
        return receipt, units

    def get(self, request):
        client_id = request.client_id
        tab = request.query_params.get("tab") or "Pending Reprocess"
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        vendor = request.query_params.get("vendor") or "All"
        status_filter = request.query_params.get("status") or "All"

        reprocess_qs = (
            JWOReprocess.objects.filter(client_id=client_id)
            .select_related(
                "job_work_order", "job_work_order__vendor", "job_work_order__process_plan",
                "created_by", "updated_by",
            )
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

            meta = self._meta(r)
            sent_q = float(r.qty or 0)
            result = meta if r.status == "Completed" else (meta.get("draft") or {})
            created_at = timezone.localtime(r.created_at) if r.created_at else None
            audit = [{
                "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                "action": "Created",
                "user": r.created_by.name if r.created_by_id else None,
                "remarks": r.reason or None,
            }]
            if r.status != "Pending" and r.updated_at:
                audit.append({
                    "dateTime": timezone.localtime(r.updated_at).strftime("%d-%m-%Y %H:%M"),
                    "action": re_status,
                    "user": r.updated_by.name if r.updated_by_id else None,
                    "remarks": meta.get("remarks") or None,
                })
            records.append({
                "id": str(r.id),
                "reprocessNo": r.no,
                "date": r.date.strftime("%d-%m-%Y") if r.date else "",
                "rawDate": r.date.isoformat() if r.date else "",
                "sourceQcNo": r.inward_no or (jwo.jwo_no if jwo else None),
                "itemCode": (plan.fabric_item or None) if plan else None,
                "fabricItem": (plan.fabric_item or plan.fabric_quality or None) if plan else None,
                "sentQty": sent_q,
                "receivedQty": float(sum(float(result.get(k, 0) or 0) for k in ("goodQty", "holdQty", "reprocessQty", "rejectedQty"))) if r.status == "Completed" else None,
                "pendingQty": 0.0 if r.status == "Completed" else sent_q,
                "vendor": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "reinspectionStatus": re_status,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "goodQty": float(result.get("goodQty", 0) or 0),
                "holdQty": float(result.get("holdQty", 0) or 0),
                "reprocessQty": float(result.get("reprocessQty", 0) or 0),
                "rejectedQty": float(result.get("rejectedQty", 0) or 0),
                "remarks": result.get("remarks") or (r.remarks if not meta else None),
                "measurements": result.get("measurements") or [],
                "auditTrail": audit,
            })

        vendors = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True).distinct().order_by("name")
        )
        items = list(
            Item.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("sku", flat=True).distinct().order_by("sku")
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
        from . import qc as qc_service

        client_id = request.client_id
        action_type = request.data.get("actionType") or "create"

        if action_type == "disposition":
            reprocess_id = request.data.get("reprocessId")
            reprocess = JWOReprocess.objects.filter(client_id=client_id, id=reprocess_id).first() if reprocess_id else None
            if reprocess is None:
                raise ValidationFailed("Reprocess record not found.", field_errors={"reprocessId": ["Not found."]})
            if reprocess.status == "Completed":
                raise Conflict(f"Reprocess {reprocess.no} is already reinspected.", code=Codes.ALREADY_DONE)

            is_draft = bool(request.data.get("isDraft", False))
            quantities = qc_service.parse_quantities(request.data)
            remarks = request.data.get("remarks") or ""
            measurements = request.data.get("measurements") or []
            result = {
                "goodQty": float(quantities["good"]),
                "secondQualityQty": float(quantities["second"]),
                "holdQty": float(quantities["hold"]),
                "reprocessQty": float(quantities["reprocess"]),
                "rejectedQty": float(quantities["reject"]),
                "remarks": remarks,
                "measurements": measurements,
                "updatedAt": timezone.now().isoformat(),
            }

            with transaction.atomic():
                meta = self._meta(reprocess)
                if is_draft:
                    meta["draft"] = result
                    reprocess.remarks = json.dumps(meta)
                    reprocess.save(update_fields=["remarks", "updated_at"])
                    return Response({
                        "success": True, "status": reprocess.status,
                        "message": "Reinspection draft saved. Stock is not moved until approval.",
                    })

                total = sum(quantities.values(), ZERO)
                if total <= ZERO:
                    raise ValidationFailed(
                        "Enter the reinspection quantities.",
                        field_errors={"goodQty": ["Total must be greater than zero."]},
                    )
                receipt, units = self._reprocess_units(reprocess)
                pool = round4(sum((u["qty"] for u in units), ZERO))
                moved = 0
                if units:
                    if abs(total - pool) > Decimal("0.001"):
                        raise ValidationFailed(
                            f"Reinspection total ({total}) must equal the quantity at the "
                            f"Reprocess location for {reprocess.inward_no} ({pool}).",
                            field_errors={"goodQty": [f"Expected a total of {pool}."]},
                        )
                    good_location_id = receipt.location_id
                    for unit, category, qty in qc_service._allocate(units, quantities):
                        if category == "reprocess":
                            continue  # stays at Reprocess for another round
                        condition = qc_service.CATEGORY_CONDITION[category]
                        _, lot_qc = stock.CONDITION_ROUTING[condition]
                        destination = stock.condition_location_id(client_id, condition, good_location_id)
                        move_type = qc_service.CATEGORY_MOVEMENT[category]
                        common = dict(
                            client_id=client_id, reference_type="JWOReprocess",
                            reference_id=reprocess.id, reference_number=reprocess.no,
                            source_document_type="GoodsReceipt", source_document_id=receipt.id,
                            notes=f"Reinspection {reprocess.no}" + (f": {remarks}" if remarks else ""),
                            user=request.user,
                        )
                        if unit["lot"] is not None:
                            _, movements = stock.relocate_lot(
                                lot=unit["lot"], quantity=qty, to_location_id=destination,
                                out_type=move_type, in_type=move_type, qc_status=lot_qc,
                                split_suffix="RI", **common,
                            )
                        else:
                            movements = stock.relocate_item_qty(
                                item=unit["item_id"], quantity=qty, from_location_id=unit["from"],
                                to_location_id=destination, out_type=move_type, in_type=move_type,
                                **common,
                            )
                        moved += len(movements)

                result["stockPosted"] = bool(units)
                reprocess.status = "Completed"
                reprocess.remarks = json.dumps(result)
                reprocess.updated_by = request.user
                reprocess.save(update_fields=["status", "remarks", "updated_by", "updated_at"])

            message = (
                f"Reinspection of {reprocess.no} approved; {moved} stock movement(s) posted."
                if units else
                f"Reinspection of {reprocess.no} recorded. No stock of this order is held at the "
                "Reprocess location, so no stock moved."
            )
            return Response({"success": True, "status": reprocess.status, "stockPosted": bool(units), "message": message})

        # Create a reprocess / reinspection entry -- explicit inputs only.
        jwo_id = request.data.get("jwo_id") or request.data.get("jwoId") or request.data.get("jobWorkOrderId")
        challan_no = request.data.get("challan_no") or request.data.get("challanNo")
        meters_raw = request.data.get("meters") or request.data.get("sentQty") or request.data.get("qty")
        reason = (request.data.get("reason") or "").strip()

        field_errors = {}
        if not jwo_id:
            field_errors["jwo_id"] = ["Job Work Order is required."]
        if not challan_no:
            field_errors["challan_no"] = ["Challan number is required."]
        try:
            sent_qty = round4(D(meters_raw))
        except Exception:  # noqa: BLE001
            sent_qty = ZERO
        if sent_qty <= ZERO:
            field_errors["meters"] = ["Meters must be greater than zero."]
        try:
            r_date = date.fromisoformat(str(request.data.get("date") or ""))
        except ValueError:
            r_date = None
        if r_date is None:
            field_errors["date"] = ["Enter the date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("Missing required reinspection fields.", field_errors=field_errors)

        jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first() if _is_uuid(jwo_id) else None
        if jwo is None:
            jwo = JobWorkOrder.objects.filter(client_id=client_id, jwo_no=jwo_id).first()
        if jwo is None:
            raise ValidationFailed(
                f"Job Work Order '{jwo_id}' does not exist.",
                field_errors={"jwo_id": ["Invalid Job Work Order."]},
            )

        with transaction.atomic():
            rp_no = allocate_number(request.user.client, "RP", r_date)
            reprocess = JWOReprocess.objects.create(
                client_id=client_id,
                job_work_order=jwo,
                no=rp_no,
                date=r_date,
                qty=sent_qty,
                reason=f"{reason} (Challan: {challan_no})" if reason else f"Challan: {challan_no}",
                status="Pending",
                created_by=request.user,
            )
        return Response({
            "success": True,
            "message": f"Reinspection record {rp_no} created.",
            "reprocessId": str(reprocess.id),
            "reprocessNo": rp_no,
        }, status=status.HTTP_201_CREATED)


def _is_uuid(value):
    import uuid as _uuid

    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class LotTakaRegisterView(APIView):
    """Lot / Taka Register endpoints for fabric lots, quantities, and movement history."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["adjust_stock"], "PATCH": ["view_inventory"]}

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
            lt = LotTaka.objects.filter(client_id=client_id, id=pk, deleted_at__isnull=True).select_related("item", "supplier", "location").first()
            if not lt:
                return Response({"detail": "Lot / Taka not found."}, status=status.HTTP_404_NOT_FOUND)
            # Fetch movement history
            # This taka's own ledger (every lot movement names its taka).
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, lot_taka_id=lt.id
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
                    "refNo": m.reference_number,
                })

            it = lt.item
            return Response({
                "id": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemId": str(lt.item_id),
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "receivedQty": float(lt.received_quantity),
                "qcStatus": lt.qc_status,
                "availableQty": float(lt.available_quantity),
                "location": lt.location.name if lt.location else None,
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else None),
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
        qs = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
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

        # Used by the transfer taka picker: takas with stock at one location.
        location_id_filter = request.query_params.get("location_id")
        if location_id_filter:
            qs = qs.filter(location_id=location_id_filter)
        if request.query_params.get("available_only") == "true":
            qs = qs.filter(available_quantity__gt=0, deleted_at__isnull=True)
        item_filter = request.query_params.get("itemId")
        if item_filter:
            qs = qs.filter(item_id=item_filter)

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
        movements_by_lot = {}
        if all_lots:
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, lot_taka_id__in=[lt.id for lt in all_lots]
            ).order_by("movement_date", "created_at")
            for m in mov_qs:
                movements_by_lot.setdefault(m.lot_taka_id, []).append(m)

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
            lot_movs = movements_by_lot.get(lt.id, [])
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
                    "refNo": m.reference_number,
                })

            records.append({
                "id": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemId": str(lt.item_id),
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "receivedQty": float(rec_q),
                "qcStatus": lt.qc_status,
                "availableQty": float(avail_q),
                "location": lt.location.name if lt.location else None,
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else None),
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

        # Summary Cards -- quantities are only summed in one unit.
        lot_units = sorted({(lt.uom or (lt.item.uom if lt.item_id else "")) for lt in all_lots} - {""})
        summary_uom = lot_units[0] if len(lot_units) == 1 else ("Mixed" if lot_units else None)
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
                "uom": summary_uom,
            },
            "totalValue": {
                "val": float(tot_value),
                "avgRate": avg_rate,
            },
            "pendingQC": {
                "qty": float(pending_qc_qty),
                "lots": len(pending_qc_lots),
                "uom": summary_uom,
            },
            "onHold": {
                "qty": float(on_hold_qty),
                "lots": len(on_hold_lots),
                "uom": summary_uom,
            },
        }

        # Filter option lists
        supplier_names = list(Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"]).values_list("name", flat=True).order_by("name"))
        quality_names = list(FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
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
                for rlt in LotTaka.objects.filter(client_id=client_id, item=prev_item, deleted_at__isnull=True).order_by("-created_at")[:5]:
                    recent_lots.append({
                        "lotNo": rlt.lot_number,
                        "takaRoll": rlt.taka_number,
                        "qty": float(rlt.received_quantity),
                        "shade": prev_item.fabric_color or None,
                        "status": rlt.status,
                    })
                preview_data = {
                    "itemCode": prev_item.sku,
                    "itemName": prev_item.name,
                    "quality": prev_item.fabric_quality or prev_item.name,
                    "design": prev_item.fabric_design or None,
                    "shade": prev_item.fabric_color or None,
                    "uom": prev_item.uom or None,
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
        """Manual lot / taka entry (opening stock or a receipt with no bill).

        The taka starts empty and the PURCHASE movement fills it, so
        ``available_quantity`` comes from the ledger. ``qcStatus`` must be
        given: ``Passed`` puts the stock in the chosen godown, ``Pending``
        puts it at QC Hold until QC releases it.
        """
        client_id = request.client_id
        data = request.data
        field_errors = {}

        item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first() if data.get("itemId") else None
        if item is None:
            field_errors["itemId"] = ["Choose the fabric item."]
        try:
            received_qty = round4(D(data.get("receivedQty")))
        except Exception:  # noqa: BLE001
            received_qty = ZERO
        if received_qty <= ZERO:
            field_errors["receivedQty"] = ["Received quantity must be greater than 0."]
        loc = Location.objects.filter(client_id=client_id, id=data.get("locationId"), deleted_at__isnull=True).first() if data.get("locationId") else None
        if loc is None:
            field_errors["locationId"] = ["Choose the storage location."]
        elif loc.code in stock.BLOCKED_LOCATION_CODES:
            field_errors["locationId"] = ["Choose a godown; system locations are filled by QC documents."]
        qc_status = data.get("qcStatus")
        if qc_status not in ("Passed", "Pending"):
            field_errors["qcStatus"] = ["Choose Passed (goes to stock) or Pending (goes to QC Hold)."]
        try:
            rec_date = date.fromisoformat(str(data.get("receivedDate") or ""))
        except ValueError:
            rec_date = None
        if rec_date is None:
            field_errors["receivedDate"] = ["Enter the received date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("The lot entry is incomplete.", field_errors=field_errors)

        # Quantities are stored in the item's stock unit.
        from apps.masters import uom as uom_service

        input_uom = (data.get("uom") or item.uom or "").strip()
        item_uom = (item.uom or "").strip()
        if input_uom and item_uom and not uom_service.same_unit_text(input_uom, item_uom):
            book = uom_service.UnitBook(client_id)
            u_in, u_item = book.resolve(input_uom), book.resolve(item_uom)
            if not (u_in and u_item and uom_service.compatible(u_in, u_item)):
                raise ValidationFailed(
                    f"UOM '{input_uom}' is incompatible with the item's stock unit '{item_uom}'.",
                    field_errors={"uom": ["Incompatible unit."]},
                )
            received_qty = round4(uom_service.convert(received_qty, u_in, u_item))

        client = request.user.client
        lot_no = (data.get("lotNo") or "").strip() or allocate_number(client, "LOT", rec_date)
        taka_roll = str(data.get("takaRoll") or "").strip() or str(
            LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, deleted_at__isnull=True).count() + 1
        )
        if LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, taka_number=taka_roll, deleted_at__isnull=True).exists():
            raise ValidationFailed(
                f"Lot '{lot_no}' with Taka / Roll '{taka_roll}' already exists in the register.",
                field_errors={"takaRoll": ["Already exists for this lot."]},
            )

        supplier = None
        if data.get("supplierId"):
            supplier = Party.objects.filter(client_id=client_id, id=data.get("supplierId")).first()
        elif data.get("supplier"):
            supplier = Party.objects.filter(client_id=client_id, name=data.get("supplier")).first()

        def dec_or_none(key):
            value = data.get(key)
            return D(str(value)) if value not in (None, "") else None

        rate = dec_or_none("rate") or ZERO
        taka_code = allocate_number(client, "TAK")
        target_location = loc if qc_status == "Passed" else stock.system_location(client_id, "QC_HOLD")
        source_doc = data.get("sourceDoc") or "Manual"
        reference_number = data.get("grnNo") or f"{lot_no}/{taka_roll}"

        lot_taka = LotTaka.objects.create(
            client_id=client_id,
            taka_id=taka_code,
            lot_number=lot_no,
            taka_number=taka_roll,
            item=item,
            supplier=supplier,
            source_document_type=source_doc,
            source_document_number=data.get("grnNo") or None,
            location=target_location,
            received_date=rec_date,
            received_quantity=received_qty,
            available_quantity=ZERO,
            uom=item.uom,
            qc_status=qc_status,
            status="Active",
            barcode=data.get("barcode") or taka_code,
            tag_status="Pending",
            print_count=0,
            # kg is recorded only as the roll weight -- never as the
            # movement's weighed quantity for a meter item.
            roll_weight=dec_or_none("rollWeight"),
            width=dec_or_none("width"),
            pieces=int(data.get("pieces") or 1),
            gsm=dec_or_none("gsm"),
            rate=rate,
            remarks=data.get("remarks") or None,
            tags=data.get("tags") or [],
            image_url=data.get("imageUrl") or None,
            created_by=request.user,
        )
        mov = stock.post_movement(
            client_id=client_id,
            item=item,
            location=target_location,
            type="PURCHASE",
            quantity=received_qty,
            unit_cost=rate,
            batch_number=lot_no,
            lot_taka=lot_taka,
            reference_type="LotRegister",
            reference_id=lot_taka.id,
            reference_number=reference_number,
            movement_date=rec_date,
            notes=f"Manual lot entry {lot_no} / {taka_roll} ({taka_code})",
            user=request.user,
        )
        lot_taka.stock_movement = mov
        lot_taka.save(update_fields=["stock_movement", "updated_at"])

        from apps.core.audit import record_audit

        record_audit(
            client=client_id, actor=request.user, action="create", entity_type="LotTaka",
            entity_id=lot_taka.id, entity_label=taka_code,
            description=f"Manual lot {lot_no} / {taka_roll}: {received_qty} {item.uom} at {target_location.name}",
        )
        return Response({
            "success": True,
            "message": f"Lot {lot_no} / Taka {taka_roll} ({taka_code}) created at {target_location.name}.",
            "id": str(lot_taka.id),
            "takaId": lot_taka.taka_id,
            "lotNo": lot_taka.lot_number,
            "takaRoll": lot_taka.taka_number,
            "barcode": lot_taka.barcode,
        }, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def patch(self, request, pk=None):
        """Descriptive fields only. Location, rate, status and QC status change
        only through documents (transfer, adjustment, QC disposition)."""
        client_id = request.client_id
        lot_id = pk or request.data.get("id")
        lot_taka = LotTaka.objects.filter(client_id=client_id, id=lot_id, deleted_at__isnull=True).first()
        if not lot_taka:
            raise ValidationFailed("Lot record not found.", field_errors={"id": ["Not found."]})

        data = request.data
        disallowed = [
            f for f in ("locationId", "location", "rate", "status", "qcStatus", "qc_status",
                        "availableQty", "receivedQty")
            if f in data
        ]
        if disallowed:
            raise ValidationFailed(
                f"Direct edits to {', '.join(disallowed)} are not allowed. "
                "Stock, location and status changes come from documents "
                "(GRN, stock transfer, adjustment or QC disposition).",
                field_errors={f: ["Not editable."] for f in disallowed},
            )

        before = {"remarks": lot_taka.remarks, "rollWeight": str(lot_taka.roll_weight),
                  "width": str(lot_taka.width), "gsm": str(lot_taka.gsm)}
        if "remarks" in data:
            lot_taka.remarks = data["remarks"]
        if "tags" in data:
            lot_taka.tags = data["tags"]
        if "rollWeight" in data:
            lot_taka.roll_weight = D(str(data["rollWeight"])) if data["rollWeight"] not in (None, "") else None
        if "width" in data:
            lot_taka.width = D(str(data["width"])) if data["width"] not in (None, "") else None
        if "gsm" in data:
            lot_taka.gsm = D(str(data["gsm"])) if data["gsm"] not in (None, "") else None
        if "imageUrl" in data:
            lot_taka.image_url = data["imageUrl"]
        lot_taka.updated_by = request.user
        lot_taka.save()

        from apps.core.audit import record_audit

        record_audit(
            client=client_id, actor=request.user, action="update", entity_type="LotTaka",
            entity_id=lot_taka.id, entity_label=lot_taka.taka_id, before=before,
            after={"remarks": lot_taka.remarks, "rollWeight": str(lot_taka.roll_weight),
                   "width": str(lot_taka.width), "gsm": str(lot_taka.gsm)},
        )
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
        quality_names = list(FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        design_names = list(Design.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        shade_names = list(Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
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
            taka_id = lt.taka_id or lt.barcode
            records.append({
                "id": str(lt.id),
                "takaId": taka_id,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedQty": float(lt.received_quantity),
                "availableQty": float(lt.available_quantity),
                "uom": lt.uom or (it.uom if it else None),
                "qcStatus": lt.qc_status,
                "tagStatus": lt.tag_status or "Pending",
                "barcode": lt.barcode or taka_id,
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "lastPrintedBy": getattr(lt.last_printed_by, "name", None) or getattr(lt.last_printed_by, "email", None) or None,
                "location": lt.location.name if lt.location else None,
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
        user_name = getattr(user, "name", None) or getattr(user, "email", None)

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
                taka_id=lt.taka_id or lt.barcode,
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
        taka_id = lt.taka_id or lt.barcode

        taka_dict = {
            "found": True,
            "id": str(lt.id),
            "takaId": taka_id,
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "itemCode": it.sku if it else "-",
            "itemName": it.name if it else None,
            "quality": (it.fabric_quality or it.name) if it else None,
            "design": (it.fabric_design or None) if it else None,
            "shade": (it.fabric_color or None) if it else None,
            "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
            "supplierId": str(lt.supplier.id) if lt.supplier else "",
            "receivedQty": float(lt.received_quantity),
            "availableQty": float(lt.available_quantity),
            "uom": lt.uom or (it.uom if it else None),
            "qcStatus": lt.qc_status,
            "tagStatus": lt.tag_status or "Pending",
            "barcode": lt.barcode or taka_id,
            "printCount": lt.print_count or 0,
            "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
            "location": lt.location.name if lt.location else None,
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
                "printedBy": log.printed_by_name or (log.printed_by.name if log.printed_by else None),
                "takaId": log.taka_id,
                "lotNo": log.lot_number,
                "takaRoll": log.taka_number,
                "notes": log.notes or "-",
            }
            for log in logs
        ]
        return Response({
            "takaId": lt.taka_id or lt.barcode,
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
            taka_id = lt.taka_id or lt.barcode
            item_code = it.sku if it else "-"
            item_name = it.name if it else "-"
            quality = (it.fabric_quality or it.name) if it else "-"
            design = (it.fabric_design or "-") if it else "-"
            shade = (it.fabric_color or "-") if it else "-"
            supplier = lt.supplier.name if lt.supplier else (lt.source_document_number or "-")
            grn_no = lt.source_document_number or "-"
            qty_str = f"{float(lt.received_quantity):.2f} {lt.uom or (it.uom if it else '')}"
            qc_st = lt.qc_status or "-"

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
                bc = code128.Code128(taka_id or f"{lt.lot_number}-{lt.taka_number}", barHeight=7.5 * mm, barWidth=0.25 * mm, humanReadable=False)
                bc.drawOn(pdf, 3.5 * mm, 26 * mm)
            except Exception:
                pass

            # Human readable Taka ID
            pdf.setFont("Helvetica-Bold", 7.5)
            pdf.drawCentredString(30 * mm, 22.5 * mm, taka_id or '-')

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


class LotTraceabilityView(APIView):
    """
    GET /inventory/traceability/?target=<lot_or_taka>
    GET /inventory/lots/<lot_id>/traceability/
    Complete traceability tree, movements, job work and QC history (PDF §07, §08, Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, lot_id=None):
        target = lot_id or request.query_params.get("target") or request.query_params.get("lotNo") or request.query_params.get("takaId")
        if not target:
            return Response({"error": "Target lot number or taka ID required."}, status=status.HTTP_400_BAD_REQUEST)

        lineage = stock.trace_lot_lineage(request.client_id, target)
        if lineage is None:
            return Response({"error": f"Lot/Taka '{target}' not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(lineage)


class StockAgeingView(APIView):
    """
    GET /inventory/stock/ageing/
    Real ageing breakdown into buckets (0-30, 31-60, 61-90, 91-180, 180+) computed
    from received_date of each LotTaka (PDF §07, Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        loc_id = request.query_params.get("locationId")
        item_id = request.query_params.get("itemId")
        data = stock.get_stock_ageing(request.client_id, location_id=loc_id, item_id=item_id)
        return Response(data)


class SlowMovingStockView(APIView):
    """
    GET /inventory/stock/slow-moving/
    List takas held longer than threshold days with zero recent movements (Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        threshold = int(request.query_params.get("thresholdDays") or 60)
        data = stock.get_slow_moving_stock(request.client_id, threshold_days=threshold)
        return Response(data)


class QCInspectionViewSet(TenantModelViewSet):
    """Piece-by-piece fabric inspection register (PDF §08, Phase 2.6).

    Calculates 4-point penalty score per roll, assigns grade (A, B, C, Second Quality),
    logs defects against DefectMaster, and posts real stock movements upon disposition.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {
        "write": ["manage_inventory"],
        "approve": ["approve_qc"],
        "dispose": ["approve_qc"],
    }
    queryset = QCInspection.objects.prefetch_related(
        "lines__defects__defect", "lines__lot_taka"
    )
    serializer_class = QCInspectionSerializer
    audit_entity_type = "QCInspection"
    audit_label_field = "inspection_number"
    status_field = "status"
    default_date_field = "inspection_date"
    search_fields = ["inspection_number", "source_document_number"]
    ordering = ["-inspection_date", "-created_at"]

    @transaction.atomic
    def perform_create(self, serializer):
        lines_data = serializer.validated_data.pop("lines", [])
        serializer.validated_data["inspection_number"] = allocate_number(
            self.request.user.client, "QC", serializer.validated_data.get("inspection_date")
        )
        if not serializer.validated_data.get("inspector") and not serializer.validated_data.get("inspector_employee"):
            serializer.validated_data["inspector"] = self.request.user

        inspection = super().perform_create(serializer)
        self._save_lines(inspection, lines_data)
        self._recompute_totals(inspection)
        return inspection

    @transaction.atomic
    def perform_update(self, serializer):
        lines_data = serializer.validated_data.pop("lines", None)
        inspection = super().perform_update(serializer)
        if lines_data is not None:
            for line in inspection.lines.all():
                line.defects.all().delete()
                line.delete()
            self._save_lines(inspection, lines_data)
            self._recompute_totals(inspection)
        return inspection

    def _save_lines(self, inspection, lines_data):
        for line_d in lines_data:
            line_dict = dict(line_d)
            defects_data = line_dict.pop("defects", [])
            lot_taka = line_dict.get("lot_taka")

            actual_meter = D(line_dict.get("actual_meter") or (lot_taka.available_quantity if lot_taka else 0) or 0)
            line_dict["actual_meter"] = actual_meter

            width = D(line_dict.get("width") or (lot_taka.width if lot_taka else 0) or 44)
            line_dict["width"] = width

            total_points = sum(int(d.get("points") or 1) * int(d.get("defect_count") or 1) for d in defects_data)
            line_dict["penalty_points"] = total_points

            width_meters = width * Decimal("0.0254")
            area = actual_meter * width_meters
            if area > ZERO:
                pts_100m = round2((Decimal(total_points) * Decimal("100")) / area)
            else:
                pts_100m = Decimal("0.00")
            line_dict["points_per_100_sq_meter"] = pts_100m

            if not line_dict.get("grade"):
                if pts_100m <= Decimal("20.0"):
                    line_dict["grade"] = "A"
                elif pts_100m <= Decimal("28.0"):
                    line_dict["grade"] = "B"
                elif pts_100m <= Decimal("40.0"):
                    line_dict["grade"] = "C"
                else:
                    line_dict["grade"] = "Second Quality"

            if not line_dict.get("disposition") or line_dict.get("disposition") == "Pass":
                if line_dict["grade"] in ("A", "B"):
                    line_dict["disposition"] = "Pass"
                elif line_dict["grade"] == "C":
                    line_dict["disposition"] = "Second Quality"
                else:
                    line_dict["disposition"] = "Reject"

            line_obj = QCInspectionLine.objects.create(
                client_id=inspection.client_id,
                inspection=inspection,
                **line_dict,
            )

            for def_d in defects_data:
                QCInspectionLineDefect.objects.create(
                    client_id=inspection.client_id,
                    inspection_line=line_obj,
                    **def_d,
                )

    def _recompute_totals(self, inspection):
        lines = list(inspection.lines.all())
        inspection.total_inspected_qty = sum((l.actual_meter or ZERO for l in lines), ZERO)
        inspection.passed_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Pass"), ZERO)
        inspection.second_quality_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Second Quality"), ZERO)
        inspection.reprocess_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Reprocess"), ZERO)
        inspection.rejected_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Reject"), ZERO)
        inspection.hold_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Hold"), ZERO)
        inspection.save(update_fields=[
            "total_inspected_qty", "passed_qty", "second_quality_qty",
            "reprocess_qty", "rejected_qty", "hold_qty", "updated_at",
        ])

    @action(detail=True, methods=["post"], url_path="approve")
    @transaction.atomic
    def approve(self, request, pk=None):
        inspection = self.get_object()
        if inspection.status in ("Approved", "Completed"):
            raise Conflict("Inspection is already approved.", code="ALREADY_APPROVED")

        lines = list(inspection.lines.select_related("lot_taka", "lot_taka__location", "lot_taka__item"))
        qc_loc = Location.objects.filter(client_id=inspection.client_id, code="SYS-QC-HOLD").first()

        for line in lines:
            lt = line.lot_taka
            if not lt:
                continue

            disp = line.disposition
            m = line.actual_meter or lt.available_quantity or ZERO
            if m <= ZERO:
                continue

            target_loc = None
            mov_type = None

            status_val = "Available"
            qc_status_val = "Pass"

            if disp == "Pass":
                status_val = "Finished" if lt.item and getattr(lt.item, "fabric_stage", "") == "Finished" else "Available"
                qc_status_val = "Pass"
                mov_type = "QC_PASS"
                target_loc = Location.objects.filter(client_id=inspection.client_id, code="WH-MAIN").first() or Location.objects.filter(client_id=inspection.client_id, is_active=True).exclude(code__startswith="SYS-").first()
            elif disp == "Second Quality":
                status_val = "Second Quality"
                qc_status_val = "Second Quality"
                mov_type = "QC_SECOND_QUALITY"
                target_loc = stock.resolve_system_location(inspection.client_id, "SECOND_QUALITY")
            elif disp == "Reprocess":
                status_val = "Reprocess"
                qc_status_val = "Reprocess"
                mov_type = "QC_REPROCESS"
                target_loc = stock.resolve_system_location(inspection.client_id, "REPROCESS")
            elif disp == "Reject":
                status_val = "Reject"
                qc_status_val = "Reject"
                mov_type = "QC_REJECT"
                target_loc = stock.resolve_system_location(inspection.client_id, "REJECT")
            else:  # Hold
                status_val = "QC Hold"
                qc_status_val = "Hold"
                target_loc = lt.location

            if mov_type and target_loc and str(lt.location_id) != str(target_loc.id):
                stock.relocate_lot(
                    client_id=inspection.client_id,
                    lot=lt,
                    quantity=m,
                    to_location_id=target_loc.id,
                    out_type=mov_type,
                    in_type=mov_type,
                    reference_type="QCInspection",
                    reference_id=inspection.id,
                    reference_number=inspection.inspection_number,
                    user=request.user,
                )

            lt.refresh_from_db()
            lt.status = status_val
            lt.qc_status = qc_status_val
            lt.save(update_fields=["status", "qc_status", "updated_at"])

        inspection.status = "Approved"
        inspection.inspector = request.user
        inspection.save(update_fields=["status", "inspector", "updated_at"])
        return Response(self.get_serializer(inspection).data)


class LotCostEntryViewSet(TenantModelViewSet):
    queryset = LotCostEntry.objects.select_related("lot_taka")
    serializer_class = LotCostEntrySerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "LotCostEntry"
    filter_map = {
        "lotNumber": "lot_number",
        "costType": "cost_type",
        "isApproved": "is_approved",
    }

    def perform_create(self, serializer):
        entry = costing_services.post_lot_cost(
            client_id=self.request.client_id,
            lot_number=serializer.validated_data.get("lot_number"),
            cost_type=serializer.validated_data.get("cost_type"),
            amount=serializer.validated_data.get("amount"),
            quantity=serializer.validated_data.get("quantity", 0),
            source_document_type=serializer.validated_data.get("source_document_type", "MANUAL"),
            source_document_number=serializer.validated_data.get("source_document_number"),
            lot_taka=serializer.validated_data.get("lot_taka"),
            user=self.request.user,
            is_approved=serializer.validated_data.get("is_approved", True),
            remarks=serializer.validated_data.get("remarks"),
        )
        serializer.instance = entry

    @action(detail=False, methods=["get"], url_path="landed-cost")
    def landed_cost(self, request):
        lot_number = request.query_params.get("lotNumber") or request.query_params.get("lot_number")
        if not lot_number:
            raise ValidationFailed("lotNumber query parameter is required.")
        return Response(costing_services.get_lot_landed_cost(request.client_id, lot_number))

    @action(detail=False, methods=["get"], url_path="profitability")
    def profitability(self, request):
        lot_number = request.query_params.get("lotNumber") or request.query_params.get("lot_number")
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        report = costing_services.get_lot_profitability_report(
            request.client_id, lot_number=lot_number, date_from=date_from, date_to=date_to
        )
        return Response(report)


class SampleViewSet(TenantModelViewSet):
    queryset = Sample.objects.select_related(
        "party", "salesperson", "broker", "source_lot_taka", "fabric_quality", "design", "shade"
    )
    serializer_class = SampleSerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "Sample"
    audit_label_field = "sample_number"
    filter_map = {
        "partyId": "party_id",
        "salespersonId": "salesperson_id",
        "brokerId": "broker_id",
        "status": "status",
        "isReturnable": "is_returnable",
    }

    def create(self, request, *args, **kwargs):
        data = request.data
        sample = sampling_services.issue_sample(
            client_id=request.client_id,
            party_id=data.get("partyId") or data.get("party_id"),
            salesperson_id=data.get("salespersonId") or data.get("salesperson_id"),
            broker_id=data.get("brokerId") or data.get("broker_id"),
            source_lot_taka_id=data.get("sourceLotTakaId") or data.get("source_lot_taka_id"),
            fabric_quality_id=data.get("fabricQualityId") or data.get("fabric_quality_id"),
            design_id=data.get("designId") or data.get("design_id"),
            shade_id=data.get("shadeId") or data.get("shade_id"),
            sample_type=data.get("sampleType") or data.get("sample_type", "CUT_PIECE"),
            quantity=data.get("quantity", 1),
            uom=data.get("uom", "MTR"),
            is_returnable=data.get("isReturnable", True) if "isReturnable" in data else data.get("is_returnable", True),
            due_date=data.get("dueDate") or data.get("due_date"),
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response(self.get_serializer(sample).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return-sample")
    def return_sample_action(self, request, pk=None):
        sample = self.get_object()
        return_qty = request.data.get("returnQuantity") or request.data.get("quantity") or request.data.get("return_quantity")
        condition = request.data.get("condition", "Good")
        remarks = request.data.get("remarks")
        sample = sampling_services.return_sample(
            sample, return_qty, condition=condition, user=request.user, remarks=remarks
        )
        return Response(self.get_serializer(sample).data)

    @action(detail=True, methods=["post"], url_path="consume-sample")
    def consume_sample_action(self, request, pk=None):
        sample = self.get_object()
        consume_qty = request.data.get("consumeQuantity") or request.data.get("consume_quantity")
        remarks = request.data.get("remarks")
        sample = sampling_services.consume_sample(
            sample, consume_qty, user=request.user, remarks=remarks
        )
        return Response(self.get_serializer(sample).data)

    @action(detail=False, methods=["get"], url_path="dashboard")
    def dashboard(self, request):
        return Response(sampling_services.get_sample_expense_dashboard(request.client_id))


class TripViewSet(TenantModelViewSet):
    queryset = Trip.objects.select_related("transporter", "transporter_party").prefetch_related("materials")
    serializer_class = TripSerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "Trip"
    audit_label_field = "trip_number"
    filter_map = {
        "status": "status",
        "tripType": "trip_type",
        "transporterId": "transporter_id",
        "vehicleNumber": "vehicle_number",
    }

    def create(self, request, *args, **kwargs):
        data = request.data
        trip = logistics_services.create_trip(
            client_id=request.client_id,
            from_location=data.get("fromLocation") or data.get("from_location"),
            to_location=data.get("toLocation") or data.get("to_location"),
            vehicle_number=data.get("vehicleNumber") or data.get("vehicle_number"),
            trip_date=data.get("tripDate") or data.get("trip_date"),
            transporter_id=data.get("transporterId") or data.get("transporter_id"),
            transporter_party_id=data.get("transporterPartyId") or data.get("transporter_party_id"),
            driver_name=data.get("driverName") or data.get("driver_name"),
            driver_mobile=data.get("driverMobile") or data.get("driver_mobile"),
            lr_number=data.get("lrNumber") or data.get("lr_number"),
            lr_date=data.get("lrDate") or data.get("lr_date"),
            trip_type=data.get("tripType") or data.get("trip_type", "OUTWARD"),
            freight_amount=data.get("freightAmount") or data.get("freight_amount", 0),
            loading_charges=data.get("loadingCharges") or data.get("loading_charges", 0),
            unloading_charges=data.get("unloadingCharges") or data.get("unloading_charges", 0),
            other_charges=data.get("otherCharges") or data.get("other_charges", 0),
            paid_by=data.get("paidBy") or data.get("paid_by", "Company"),
            cost_allocation_target=data.get("costAllocationTarget") or data.get("cost_allocation_target", "LOT"),
            material_lines=data.get("materials", []),
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response(self.get_serializer(trip).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="allocate-freight")
    def allocate_freight(self, request, pk=None):
        trip = self.get_object()
        trip = logistics_services.allocate_trip_freight(trip, user=request.user)
        return Response(self.get_serializer(trip).data)





