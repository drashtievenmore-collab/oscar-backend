"""Masters endpoints (api.md §4)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import Conflict, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.viewsets import TenantModelViewSet
from apps.inventory import services as stock
from apps.masters import uom

from .models import (
    Agency,
    Broker,
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
from .serializers import (
    AgencySerializer,
    BrokerSerializer,
    CategoryPartSerializer,
    DefectMasterSerializer,
    DesignSerializer,
    FabricQualitySerializer,
    FabricSerializer,
    FabricTypeSerializer,
    FabricVariantSerializer,
    ImportRowsSerializer,
    ItemCategorySerializer,
    ItemPartSerializer,
    ItemSerializer,
    ItemSerialSerializer,
    JobWorkRateMasterSerializer,
    LocationSerializer,
    LossReasonMasterSerializer,
    PartyContactSerializer,
    PartySerializer,
    ProcessMasterSerializer,
    ReturnReasonMasterSerializer,
    ShadeSerializer,
    TransporterSerializer,
    UnitSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field):
    return Coalesce(Sum(field), Value(Decimal("0.00")), output_field=MONEY)


# ---------------------------------------------------------------------------
# Parties (api.md §4.1)
# ---------------------------------------------------------------------------
class PartyViewSet(TenantModelViewSet):
    queryset = Party.objects.select_related("ledger_account").prefetch_related("contacts")
    serializer_class = PartySerializer
    audit_entity_type = "Party"
    audit_label_field = "name"
    search_fields = ["name", "code", "phone", "email", "gstin"]
    ordering_fields = ["name", "code", "balance", "created_at"]
    ordering = ["name"]
    status_field = "status"
    filter_map = {
        "type": "type",
        "state": "place_of_supply",
        "gstTreatment": "gst_treatment",
        "gst_treatment": "gst_treatment",
    }
    default_date_field = "created_at"

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            total=Count("id"),
            customers=Count("id", filter=Q(type__in=["Customer", "Both"])),
            vendors=Count("id", filter=Q(type__in=["Vendor", "Both"])),
            receivable=money_sum("balance"),
        )
        return rows

    def create_defaults(self):
        defaults = super().create_defaults()
        return defaults

    def perform_create(self, serializer):
        # The server owns the party code (api.md §1.7): the client must never
        # invent one, and a collision on a hand-typed code is a support ticket.
        if not serializer.validated_data.get("code"):
            series = {
                "Vendor": "VEND",
                "Processor": "PROC",
            }.get(serializer.validated_data.get("type"), "CUST")
            serializer.validated_data["code"] = allocate_number(
                self.request.user.client, series
            )
        return super().perform_create(serializer)

    @action(detail=False, methods=["get"])
    def customers(self, request):
        """``type in (Customer, Both)`` -- the one customer list.

        Also what Administration -> Clients reads, so the admin screen and the
        Parties module can never disagree about who the customers are. The
        ``lead_count`` annotation says which of these arrived through a lead
        conversion (db.md §4.1: a converted lead creates the Customer row).
        """
        queryset = self.filter_queryset(
            self.get_queryset().annotate(
                lead_count=Count(
                    "leads", filter=Q(leads__deleted_at__isnull=True), distinct=True
                )
            )
        ).filter(type__in=["Customer", "Both"])
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=False, methods=["get"])
    def vendors(self, request):
        queryset = self.filter_queryset(self.get_queryset()).filter(
            type__in=["Vendor", "Both", "Processor"]
        )
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=False, methods=["get"])
    def processors(self, request):
        """Job workers / processors -- type in (Processor) or is_processor=True or vendor_type='Dyeing - Other Process'."""
        queryset = self.filter_queryset(self.get_queryset()).filter(
            Q(is_processor=True) | Q(type="Processor") | Q(vendor_type="Dyeing - Other Process")
        )
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        """``GET /parties/{id}/ledger/`` -- replaces getCustomerLedger / getVendorLedger."""
        from apps.accounting.services import party_ledger

        self.get_object()
        return Response(
            party_ledger(
                request.client_id,
                pk,
                date_from=request.query_params.get("date_from"),
                date_to=request.query_params.get("date_to"),
            )
        )

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """``GET /parties/{id}/summary/`` -- the Customer 360 drawer."""
        from apps.sales.models import PaymentIn, SalesInvoice, SalesOrder

        party = self.get_object()

        orders = SalesOrder.objects.filter(
            client_id=request.client_id, party=party, deleted_at__isnull=True
        ).exclude(stage="Cancelled")
        invoices = SalesInvoice.objects.filter(
            client_id=request.client_id, party=party, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft"])

        invoice_rows = invoices.aggregate(
            lifetime=money_sum("total"), paid=money_sum("amount_paid"), count=Count("id")
        )
        advance = PaymentIn.objects.filter(
            client_id=request.client_id, party=party, status="Active", deleted_at__isnull=True
        ).aggregate(
            amount=money_sum("amount"), allocated=money_sum("allocated_amount")
        )

        last_order = orders.order_by("-doc_date").values_list("doc_date", flat=True).first()
        last_invoice = (
            invoices.order_by("-doc_date").values_list("doc_date", flat=True).first()
        )

        return Response(
            {
                "partyId": str(party.id),
                "name": party.name,
                "balance": round2(party.balance),
                "creditLimit": round2(party.credit_limit) if party.credit_limit else None,
                "outstanding": round2(invoice_rows["lifetime"] - invoice_rows["paid"]),
                "lifetimeValue": round2(invoice_rows["lifetime"]),
                "openOrders": orders.exclude(stage__in=["Fully Dispatched", "Invoiced", "Closed"]).count(),
                "openInvoices": invoices.exclude(status="Paid").count(),
                "totalInvoices": invoice_rows["count"],
                "lastOrderDate": last_order,
                "lastInvoiceDate": last_invoice,
                "unallocatedAdvance": round2(
                    max(advance["amount"] - advance["allocated"], ZERO)
                ),
            }
        )

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        """``GET /parties/{id}/documents/`` -- every document referencing the
        party, newest first. Replaces ``RelatedDocumentsCard``'s scan of the
        whole context (api-integration.md §9.1.3)."""
        from apps.purchase.models import PurchaseBill, PurchaseOrder
        from apps.sales.models import (
            DeliveryChallan,
            Quotation,
            SalesInvoice,
            SalesOrder,
        )

        self.get_object()
        rows = []
        sources = [
            ("Quotation", Quotation, "quotation_number", "status"),
            ("SalesOrder", SalesOrder, "order_number", "stage"),
            ("DeliveryChallan", DeliveryChallan, "challan_number", "status"),
            ("SalesInvoice", SalesInvoice, "invoice_number", "status"),
            ("PurchaseOrder", PurchaseOrder, "po_number", "status"),
            ("PurchaseBill", PurchaseBill, "bill_number", "status"),
        ]
        for label, model, number_field, status_field in sources:
            for row in model.objects.filter(
                client_id=request.client_id, party_id=pk, deleted_at__isnull=True
            ).order_by("-doc_date")[:50]:
                rows.append(
                    {
                        "documentType": label,
                        "id": str(row.id),
                        "number": getattr(row, number_field),
                        "date": row.doc_date,
                        "status": getattr(row, status_field),
                        "total": round2(row.total),
                    }
                )
        rows.sort(key=lambda row: (row["date"] is None, row["date"]), reverse=True)
        return Response(envelope(rows))

    @action(detail=True, methods=["get", "post"], url_path="contacts")
    def contacts(self, request, pk=None):
        party = self.get_object()
        if request.method == "GET":
            rows = party.contacts.filter(deleted_at__isnull=True)
            return Response(envelope(PartyContactSerializer(rows, many=True).data))

        serializer = PartyContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contact = serializer.save(client_id=request.client_id, party=party)
        return Response(
            PartyContactSerializer(contact).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=True, methods=["patch", "delete"], url_path=r"contacts/(?P<contact_id>[^/.]+)"
    )
    def contact_detail(self, request, pk=None, contact_id=None):
        party = self.get_object()
        contact = party.contacts.filter(pk=contact_id, deleted_at__isnull=True).first()
        if contact is None:
            raise NotFound("That contact no longer exists.")

        if request.method == "DELETE":
            contact.soft_delete(request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PartyContactSerializer(contact, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="import")
    def bulk_import(self, request):
        """``POST /parties/import/`` -- returns per-row errors (api.md §12.2)."""
        serializer = ImportRowsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            _import_rows(
                request,
                serializer.validated_data["rows"],
                serializer.validated_data["dryRun"],
                PartySerializer,
                Party,
                required=["name"],
            )
        )

    @action(detail=False, methods=["get"], url_path="import-template")
    def import_template(self, request):
        return Response(
            {
                "headers": [
                    "name", "type", "phone", "email", "gstin", "gstTreatment",
                    "placeOfSupply", "creditLimit", "paymentTerms", "openingBalance",
                ],
                "sampleRow": [
                    "Acme Corp", "Customer", "9876543210", "ops@acme.com",
                    "24AABCA1234A1Z5", "Registered Business", "Gujarat", "500000",
                    "Net 30", "0",
                ],
            }
        )


# ---------------------------------------------------------------------------
# Items (api.md §4.2)
# ---------------------------------------------------------------------------
class ItemViewSet(TenantModelViewSet):
    queryset = Item.objects.select_related("category", "vendor", "default_location")
    serializer_class = ItemSerializer
    audit_entity_type = "Item"
    audit_label_field = "sku"
    search_fields = ["name", "sku", "description", "hsn_code"]
    ordering_fields = ["name", "sku", "cost_price", "selling_price", "created_at"]
    ordering = ["name"]
    status_field = "lifecycle_status"
    filter_map = {
        "itemKind": "item_kind",
        "item_kind": "item_kind",
        "categoryId": "category_id",
        "category_id": "category_id",
        "location": "default_location_id",
        "lifecycleStatus": "lifecycle_status",
        "trackingMode": "tracking_mode",
        "vendorId": "vendor_id",
    }
    permission_map = {"write": ["view_inventory"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        # `?lowStock=true` and `?status=` filter on derived values, so they are
        # applied after annotation rather than in SQL.
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = stock.annotate_items_with_stock(request.client_id, page)

        low_stock = request.query_params.get("lowStock") == "true"
        status_filter = request.query_params.getlist("stockStatus")
        if low_stock:
            items = [item for item in items if item.stock_status in ("Low Stock", "Critical")]
        if status_filter:
            items = [item for item in items if item.stock_status in status_filter]

        return self.get_paginated_response(self.get_serializer(items, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        item = self.get_object()
        stock.annotate_items_with_stock(request.client_id, [item])
        data = self.get_serializer(item).data
        data["bom"] = ItemPartSerializer(
            item.bom_lines.filter(deleted_at__isnull=True).select_related("part_item"),
            many=True,
        ).data
        return Response(data)

    def get_aggregates(self, queryset):
        """KPI tiles computed in the database, never by fetching rows (db.md §15)."""
        from apps.inventory.models import StockBalance

        rows = queryset.aggregate(total=Count("id"))
        balances = StockBalance.objects.filter(
            client_id=self.get_client_id(),
            item__in=queryset.values("id"),
            deleted_at__isnull=True,
        ).aggregate(
            onHand=Coalesce(
                Sum("on_hand"), Value(Decimal("0.0000")),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            ),
            value=money_sum("inward_value"),
        )
        return {
            "totalItems": rows["total"],
            "totalOnHand": balances["onHand"],
            "inventoryValue": balances["value"],
        }

    def perform_create(self, serializer):
        """api.md §4.2 -- the server resolves ``hsnCode`` from the category and
        returns it, rather than the frontend's hardcoded steel-family map."""
        from apps.core.views import hsn_for_category

        data = serializer.validated_data
        if not data.get("hsn_code"):
            category = data.get("category")
            resolved = None
            if category is not None:
                resolved = category.default_hsn_code or hsn_for_category(
                    self.get_client_id(), category.name
                )
            data["hsn_code"] = resolved or hsn_for_category(self.get_client_id(), data.get("name"))

        instance = super().perform_create(serializer)

        # Opening stock itself posts an ADJUSTMENT movement (api.md §4.2), so
        # even the first number has a cause behind it.
        opening = self.request.data.get("openingQty") or self.request.data.get("availableQty")
        if opening and D(opening) > ZERO and instance.holds_stock:
            location = instance.default_location_id or _any_location(self.get_client_id())
            if location:
                stock.post_movement(
                    client_id=self.get_client_id(),
                    item=instance.id,
                    location=location,
                    type="ADJUSTMENT",
                    quantity=D(opening),
                    unit_cost=instance.cost_price,
                    notes="Opening stock on item creation",
                    user=self.request.user,
                )
        return instance

    def check_delete_allowed(self, item):
        """api.md §4.2 -- archiving is blocked if an open document references it."""
        from apps.purchase.models import PurchaseOrderLine
        from apps.sales.models import SalesOrderLine

        open_sales = SalesOrderLine.objects.filter(
            item=item, deleted_at__isnull=True
        ).exclude(sales_order__stage__in=["Fully Dispatched", "Invoiced", "Closed", "Cancelled"]).count()
        open_purchase = PurchaseOrderLine.objects.filter(
            item=item, deleted_at__isnull=True
        ).exclude(purchase_order__status__in=["Received", "Cancelled"]).count()

        if open_sales or open_purchase:
            raise Conflict(
                f"{item.name} is on {open_sales + open_purchase} open document(s).",
                code="ITEM_IN_USE",
                payload={"openSalesLines": open_sales, "openPurchaseLines": open_purchase},
            )

    def perform_destroy(self, instance):
        """Masters are archived through ``lifecycle_status``, never removed
        (api.md §1.9)."""
        self.check_delete_allowed(instance)
        instance.lifecycle_status = "Archived"
        instance.save(update_fields=["lifecycle_status", "updated_at"])
        self.write_audit("archive", instance, description="Item archived")

    @action(detail=True, methods=["get"])
    def stock(self, request, pk=None):
        """``GET /inventory/items/{id}/stock/`` -- replaces ``calculateItemStock``."""
        item = self.get_object()
        payload = stock.calculate_item_stock(request.client_id, item)
        payload["byLocation"] = stock.stock_by_location(request.client_id, item.id)
        return Response(payload)

    @action(detail=True, methods=["get"])
    def movements(self, request, pk=None):
        """``GET /inventory/items/{id}/movements/`` -- paged ledger."""
        from apps.inventory.models import StockMovement
        from apps.inventory.serializers import StockMovementSerializer

        self.get_object()
        queryset = StockMovement.objects.filter(
            client_id=request.client_id, item_id=pk, deleted_at__isnull=True
        ).select_related("item", "location").order_by("-movement_date", "-created_at")
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(
            StockMovementSerializer(page, many=True).data
        )

    @action(detail=True, methods=["get"], url_path="qc-block")
    def qc_block(self, request, pk=None):
        """``GET /inventory/items/{id}/qc-block/`` -- replaces ``getItemQCBlock``."""
        self.get_object()
        return Response(stock.qc_block_for(request.client_id, pk))

    @action(detail=True, methods=["get", "post", "delete"], url_path="serials")
    def serials(self, request, pk=None):
        item = self.get_object()

        if request.method == "GET":
            queryset = item.serials.filter(deleted_at__isnull=True)
            status_filter = request.query_params.getlist("status")
            if status_filter:
                queryset = queryset.filter(status__in=status_filter)
            return Response(envelope(ItemSerialSerializer(queryset, many=True).data))

        serial_numbers = request.data.get("serials") or []
        if not isinstance(serial_numbers, list) or not serial_numbers:
            raise ValidationFailed(
                "Provide at least one serial number.",
                field_errors={"serials": ["Expected a non-empty list."]},
            )

        if request.method == "DELETE":
            removed = ItemSerial.objects.filter(
                client_id=request.client_id,
                item=item,
                serial_no__in=serial_numbers,
                status="available",
            ).update(deleted_at=self.request_now())
            return Response({"removed": removed})

        existing = set(
            item.serials.filter(serial_no__in=serial_numbers).values_list(
                "serial_no", flat=True
            )
        )
        created = ItemSerial.objects.bulk_create(
            [
                ItemSerial(
                    client_id=request.client_id,
                    item=item,
                    serial_no=str(serial).strip(),
                    location=item.default_location,
                    status="available",
                )
                for serial in serial_numbers
                if str(serial).strip() and str(serial).strip() not in existing
            ]
        )
        return Response(
            {"added": len(created), "skipped": len(existing)},
            status=status.HTTP_201_CREATED,
        )

    def request_now(self):
        from django.utils import timezone

        return timezone.now()

    @action(detail=True, methods=["get", "post"], url_path="parts")
    def parts(self, request, pk=None):
        """Machine BOM (``itemParts``)."""
        item = self.get_object()
        if request.method == "GET":
            rows = item.bom_lines.filter(deleted_at__isnull=True).select_related("part_item")
            return Response(envelope(ItemPartSerializer(rows, many=True).data))

        serializer = ItemPartSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        part_item = serializer.validated_data["part_item"]
        _assert_no_bom_cycle(item, part_item)
        line = serializer.save(client_id=request.client_id, parent_item=item)
        return Response(ItemPartSerializer(line).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["patch", "delete"], url_path=r"parts/(?P<part_id>[^/.]+)"
    )
    def part_detail(self, request, pk=None, part_id=None):
        item = self.get_object()
        line = item.bom_lines.filter(pk=part_id, deleted_at__isnull=True).first()
        if line is None:
            raise NotFound("That BOM line no longer exists.")

        if request.method == "DELETE":
            line.soft_delete(request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ItemPartSerializer(
            line, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="bom-explosion")
    def bom_explosion(self, request, pk=None):
        """``?qty=4`` -> the component lines the LineItemEditor auto-inserts.

        api.md §4.3 requires availability to be re-checked against the exploded
        components, not the parent -- which is why the response carries each
        component's derived stock.
        """
        item = self.get_object()
        qty = D(request.query_params.get("qty") or 1)
        rows = _explode_bom(request.client_id, item, qty)
        return Response(envelope(rows))

    @action(detail=True, methods=["get"])
    def barcode(self, request, pk=None):
        """Payload for ``BarcodeLabelModal``."""
        item = self.get_object()
        return Response(
            {
                "itemId": str(item.id),
                "sku": item.sku,
                "name": item.name,
                "barcodeValue": item.sku,
                "hsnCode": item.hsn_code,
                "uom": item.uom,
                "sellingPrice": round2(item.selling_price),
                "mrpLabel": f"MRP {round2(item.selling_price)}",
            }
        )

    @action(detail=False, methods=["post"], url_path="import")
    def bulk_import(self, request):
        serializer = ImportRowsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            _import_rows(
                request,
                serializer.validated_data["rows"],
                serializer.validated_data["dryRun"],
                ItemSerializer,
                Item,
                required=["sku", "name"],
            )
        )

    @action(detail=False, methods=["get"], url_path="import-template")
    def import_template(self, request):
        return Response(
            {
                "headers": [
                    "sku", "name", "description", "uom", "itemKind", "costPrice",
                    "sellingPrice", "reorderLevel", "hsnCode", "trackingMode",
                ],
                "sampleRow": [
                    "STL-2MM-CRCA", "CRCA Sheet 2mm", "Cold rolled sheet", "Kg",
                    "Standalone", "62.5", "78", "500", "7208.10", "Quantity",
                ],
            }
        )


def _any_location(client_id):
    return (
        Location.objects.filter(client_id=client_id, is_active=True, deleted_at__isnull=True)
        .values_list("id", flat=True)
        .first()
    )


def _assert_no_bom_cycle(parent, child, depth=0):
    """db.md §4.2 -- the self-reference constraint only catches the trivial case."""
    if depth > 5:
        raise ValidationFailed(
            "This BOM is nested more than five levels deep.",
            field_errors={"partItemId": ["Maximum BOM depth exceeded."]},
        )
    if str(child.id) == str(parent.id):
        raise ValidationFailed(
            "An item cannot be a part of itself.",
            field_errors={"partItemId": ["Creates a cycle."]},
        )
    for grandchild in ItemPart.objects.filter(
        parent_item=child, deleted_at__isnull=True
    ).select_related("part_item"):
        _assert_no_bom_cycle(parent, grandchild.part_item, depth + 1)


def _explode_bom(client_id, item, qty, depth=0, accumulator=None):
    """Recursive explosion with the max depth of 5 db.md §4.2 asks for."""
    accumulator = accumulator if accumulator is not None else []
    if depth > 5:
        return accumulator

    lines = ItemPart.objects.filter(
        parent_item=item, deleted_at__isnull=True
    ).select_related("part_item")

    for line in lines:
        required = round4(D(line.required_qty) * D(qty))
        component = line.part_item
        stock_row = stock.calculate_item_stock(client_id, component)
        accumulator.append(
            {
                "itemId": str(component.id),
                "sku": component.sku,
                "name": component.name,
                "uom": component.uom,
                "requiredQty": required,
                "available": stock_row["available"],
                "shortfall": max(required - stock_row["available"], ZERO),
                "rate": round4(component.selling_price),
                "isBomGenerated": True,
                "bomSourceItemId": str(item.id),
                "parentSku": item.sku,
                "depth": depth + 1,
            }
        )
        if component.item_kind == "Machine":
            _explode_bom(client_id, component, required, depth + 1, accumulator)
    return accumulator


def _import_rows(request, rows, dry_run, serializer_class, model, required=()):
    """Shared bulk-import body (api.md §12.2).

    Returns ``{ created, updated, skipped, errors: [{ row, field, message }] }``
    so ``ImportModal`` can render errors against its preview rows rather than
    toasting a single failure (api-integration.md §9.5).
    """
    created = updated = skipped = 0
    errors = []
    context = {"client_id": request.client_id, "request": request}

    with transaction.atomic():
        for index, row in enumerate(rows):
            missing = [field for field in required if not row.get(field)]
            if missing:
                for field in missing:
                    errors.append({"row": index, "field": field, "message": "Required."})
                skipped += 1
                continue

            lookup = {}
            if "sku" in row and hasattr(model, "sku"):
                lookup = {"sku": row["sku"]}
            elif "code" in row and hasattr(model, "code"):
                lookup = {"code": row["code"]}

            instance = None
            if lookup:
                instance = model.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True, **lookup
                ).first()

            serializer = serializer_class(
                instance, data=row, partial=instance is not None, context=context
            )
            if not serializer.is_valid():
                for field, messages in serializer.errors.items():
                    errors.append(
                        {
                            "row": index,
                            "field": field,
                            "message": messages[0] if messages else "Invalid.",
                        }
                    )
                skipped += 1
                continue

            if not dry_run:
                serializer.save(
                    client_id=request.client_id,
                    **({} if instance else {"created_by": request.user}),
                )
            if instance is None:
                created += 1
            else:
                updated += 1

        if dry_run:
            transaction.set_rollback(True)

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dryRun": dry_run,
    }


# ---------------------------------------------------------------------------
# Categories, units, locations (api.md §4.3)
# ---------------------------------------------------------------------------
class ItemCategoryViewSet(TenantModelViewSet):
    queryset = ItemCategory.objects.select_related("parent", "created_by", "updated_by", "image_file").prefetch_related("custom_fields")
    serializer_class = ItemCategorySerializer
    audit_entity_type = "ItemCategory"
    audit_label_field = "name"
    search_fields = ["name", "code", "description", "parent__name", "parent__code"]
    ordering = ["code", "name"]
    status_field = None
    filter_map = {
        "kind": "kind",
        "isActive": "is_active",
        "is_active": "is_active",
        "parentId": "parent_id",
    }

    def get_queryset(self):
        return super().get_queryset().annotate(
            item_count=Count("items", filter=Q(items__deleted_at__isnull=True))
        )

    def check_delete_allowed(self, category):
        count = category.items.filter(deleted_at__isnull=True).count()
        if count:
            raise Conflict(
                f"{count} item(s) still use this category.",
                code="CATEGORY_IN_USE",
                payload={"itemCount": count},
            )

    @action(detail=True, methods=["get", "post"], url_path="parts")
    def parts(self, request, pk=None):
        """Category default parts (``categoryParts``)."""
        category = self.get_object()
        if request.method == "GET":
            rows = category.default_parts.filter(deleted_at__isnull=True).select_related("item")
            return Response(envelope(CategoryPartSerializer(rows, many=True).data))

        serializer = CategoryPartSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        row = serializer.save(client_id=request.client_id, category=category)
        return Response(CategoryPartSerializer(row).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"parts/(?P<part_id>[^/.]+)")
    def part_detail(self, request, pk=None, part_id=None):
        category = self.get_object()
        row = category.default_parts.filter(pk=part_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That category part no longer exists.")
        row.soft_delete(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnitViewSet(TenantModelViewSet):
    queryset = Unit.objects.select_related("image_file")
    serializer_class = UnitSerializer
    audit_entity_type = "Unit"
    audit_label_field = "code"
    search_fields = ["code", "label", "uom_type", "description"]
    ordering = ["code"]
    status_field = None
    filter_map = {
        "isActive": "is_active",
        "is_active": "is_active",
        "uomType": "uom_type",
        "isBaseUnit": "is_base_unit",
    }

    def check_delete_allowed(self, unit):
        client_id = self.get_client_id()
        count = (
            FabricQuality.objects.filter(client_id=client_id, uom=unit, deleted_at__isnull=True).count()
            + FabricVariant.objects.filter(client_id=client_id, uom=unit, deleted_at__isnull=True).count()
        )
        usage = uom.item_unit_usage(client_id, unit)
        total = count + usage["items"] + usage["documentLines"]
        if total:
            # History keeps the unit by its code -- deleting it would orphan
            # those records. Inactivating hides it from new entries instead.
            raise Conflict(
                f"{unit.code} is used by {count} fabric master(s), {usage['items']} item(s) "
                f"and {usage['documentLines']} document line(s). Mark it Inactive instead.",
                code="UNIT_IN_USE",
                payload={"usageCount": total, "fabricMasters": count, **usage},
            )


class LocationViewSet(TenantModelViewSet):
    queryset = Location.objects.select_related("parent")
    serializer_class = LocationSerializer
    audit_entity_type = "Location"
    audit_label_field = "name"
    search_fields = ["name", "code", "manager", "description"]
    ordering = ["name"]
    status_field = None
    filter_map = {"type": "type", "isActive": "is_active", "is_active": "is_active", "parent": "parent"}

    def perform_destroy(self, instance):
        client_id = self.get_client_id()
        children_count = Location.objects.filter(
            client_id=client_id, parent=instance, deleted_at__isnull=True
        ).count()
        if children_count > 0:
            raise Conflict(
                f"Cannot delete location '{instance.name}' because it has {children_count} child location(s). Reassign or delete child locations first.",
                code="LOCATION_HAS_CHILDREN",
                payload={"childCount": children_count},
            )

        from apps.inventory.models import StockBalance, StockMovement, LotTaka
        stock_on_hand = StockBalance.objects.filter(
            client_id=client_id, location=instance, on_hand__gt=0
        ).aggregate(total=Sum("on_hand"))["total"] or 0
        if stock_on_hand > 0:
            raise Conflict(
                f"Cannot delete location '{instance.name}' because it currently holds {stock_on_hand} units of stock. Transfer stock out before deleting.",
                code="LOCATION_HAS_STOCK",
                payload={"onHand": float(stock_on_hand)},
            )

        has_movements = StockMovement.objects.filter(
            client_id=client_id, location=instance
        ).exists()
        has_takas = LotTaka.objects.filter(
            client_id=client_id, location=instance, deleted_at__isnull=True
        ).exists()
        if has_movements or has_takas:
            raise Conflict(
                f"Cannot delete location '{instance.name}' because it has historical inventory transactions or lot/takas. Deactivate the location instead to preserve stock ledger traceability.",
                code="LOCATION_HAS_HISTORY",
            )
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        client_id = self.get_client_id()
        locs = Location.objects.filter(client_id=client_id, deleted_at__isnull=True)
        active_locs = locs.filter(is_active=True)

        total_warehouses = active_locs.filter(type="Warehouse").count()
        total_locations = active_locs.count()
        total_racks = active_locs.filter(type="Rack").count()
        total_bins = active_locs.filter(type="Bin").count()

        from apps.inventory.models import StockBalance
        loc_ids_with_stock = set(
            StockBalance.objects.filter(
                client_id=client_id, on_hand__gt=0
            ).values_list("location_id", flat=True).distinct()
        )
        locations_with_stock = active_locs.filter(id__in=loc_ids_with_stock).count()
        empty_locations = max(0, total_locations - locations_with_stock)

        total_capacity = active_locs.aggregate(total=Sum("capacity"))["total"] or Decimal("0")
        total_used = StockBalance.objects.filter(
            client_id=client_id, on_hand__gt=0
        ).aggregate(total=Sum("on_hand"))["total"] or Decimal("0")

        capacity_utilization = round((float(total_used) / float(total_capacity)) * 100, 1) if total_capacity > 0 else 0.0

        return Response({
            "total_warehouses": total_warehouses,
            "total_locations": total_locations,
            "total_racks": total_racks,
            "total_bins": total_bins,
            "locations_with_stock": locations_with_stock,
            "empty_locations": empty_locations,
            "total_capacity": float(total_capacity),
            "total_used": float(total_used),
            "capacity_utilization": capacity_utilization,
        })

    @action(detail=False, methods=["get"])
    def hierarchy(self, request):
        from .serializers import get_location_descendant_ids
        from apps.inventory.models import StockBalance
        client_id = self.get_client_id()
        locs = list(Location.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name"))

        tree = []
        loc_map = {}
        for loc in locs:
            all_ids = [loc.id] + list(get_location_descendant_ids(loc))
            used = StockBalance.objects.filter(
                client_id=client_id, location_id__in=all_ids, on_hand__gt=0
            ).aggregate(total=Sum("on_hand"))["total"] or 0
            cap = float(loc.capacity or 0)
            util = round((float(used) / cap) * 100, 1) if cap > 0 else 0.0

            loc_data = {
                "id": str(loc.id),
                "code": loc.code,
                "name": loc.name,
                "type": loc.type,
                "parent_id": str(loc.parent_id) if loc.parent_id else None,
                "capacity": cap,
                "used_quantity": float(used),
                "utilization_pct": util,
                "is_active": loc.is_active,
                "children": [],
            }
            loc_map[str(loc.id)] = loc_data

        for loc_id, node in loc_map.items():
            pid = node["parent_id"]
            if pid and pid in loc_map:
                loc_map[pid]["children"].append(node)
            else:
                tree.append(node)

        return Response({"tree": tree, "total_nodes": len(locs)})

    @action(detail=True, methods=["get"])
    def details(self, request, pk=None):
        from .serializers import get_location_descendant_ids
        from apps.inventory.models import StockBalance, StockMovement, LotTaka
        loc = self.get_object()
        client_id = self.get_client_id()
        all_ids = [loc.id] + list(get_location_descendant_ids(loc))

        balances = StockBalance.objects.filter(
            client_id=client_id, location_id__in=all_ids, on_hand__gt=0
        ).select_related("item", "location")

        item_rows = []
        for b in balances:
            lot_count = LotTaka.objects.filter(
                client_id=client_id, item_id=b.item_id, location_id__in=all_ids, deleted_at__isnull=True, status="Active"
            ).count()
            first_taka = LotTaka.objects.filter(
                client_id=client_id, item_id=b.item_id, location_id__in=all_ids, deleted_at__isnull=True, status="Active"
            ).first()
            item_rows.append({
                "item_id": str(b.item_id),
                "item_code": b.item.sku or b.item.code or f"ITM-{b.item.id}",
                "item_name": b.item.name,
                "fabric_quality": getattr(b.item, "fabric_quality", "") or "",
                "design": getattr(b.item, "fabric_design", "") or "",
                "shade": getattr(b.item, "fabric_color", "") or "",
                "lot_number": first_taka.lot_number if first_taka else "-",
                "lot_taka_count": lot_count,
                "quantity": float(b.on_hand),
                "uom": b.item.uom or "m",
                "location_code": b.location.code,
            })

        takas = LotTaka.objects.filter(
            client_id=client_id, location_id__in=all_ids, deleted_at__isnull=True
        ).select_related("item", "location").order_by("-received_date", "-created_at")[:50]

        taka_rows = [{
            "id": str(t.id),
            "taka_id": t.taka_id or f"TAK-{t.taka_number}",
            "lot_number": t.lot_number,
            "taka_number": t.taka_number,
            "item_code": t.item.sku or t.item.code,
            "item_name": t.item.name,
            "quantity": float(t.available_quantity or t.received_quantity),
            "uom": t.uom or "m",
            "qc_status": t.qc_status,
            "barcode": t.barcode or t.taka_id,
            "tag_status": t.tag_status,
            "location_code": t.location.code if t.location else loc.code,
            "received_date": t.received_date.isoformat() if t.received_date else None,
        } for t in takas]

        movements = StockMovement.objects.filter(
            client_id=client_id, location_id__in=all_ids
        ).select_related("item", "created_by").order_by("-movement_date", "-created_at")[:30]

        tx_rows = [{
            "id": str(m.id),
            "movement_date": m.movement_date.isoformat() if m.movement_date else None,
            "type": m.type,
            "reference_type": m.reference_type,
            "reference_number": m.reference_number or "-",
            "item_code": m.item.sku or m.item.code,
            "item_name": m.item.name,
            "quantity": float(m.quantity),
            "user_name": (getattr(m.created_by, "name", None) or m.created_by.get_username()) if m.created_by else "System",
            "notes": m.notes,
        } for m in movements]

        used_qty = sum(r["quantity"] for r in item_rows)
        capacity = float(loc.capacity or 0)
        avail = max(0.0, capacity - used_qty) if capacity > 0 else 0.0
        util_pct = round((used_qty / capacity) * 100, 1) if capacity > 0 else 0.0

        serializer = self.get_serializer(loc)
        return Response({
            "location": serializer.data,
            "stock_by_item": item_rows,
            "lot_takas": taka_rows,
            "transactions": tx_rows,
            "utilization": {
                "capacity": capacity,
                "used": used_qty,
                "available": avail,
                "utilization_pct": util_pct,
            }
        })


class TextileMasterViewSet(TenantModelViewSet):
    """CRUD for the code + name textile masters."""

    audit_label_field = "name"
    search_fields = ["name", "code", "description"]
    ordering = ["name"]
    status_field = None
    filter_map = {"isActive": "is_active", "is_active": "is_active"}


class VariantPartViewSet(TextileMasterViewSet):
    """A master that fabric items point at: archiving it while a live fabric
    item still uses it would leave that item describing nothing."""

    variant_field = None

    def check_delete_allowed(self, part):
        count = FabricVariant.objects.filter(
            client_id=self.get_client_id(), deleted_at__isnull=True, **{self.variant_field: part}
        ).count()
        if count:
            raise Conflict(
                f"{part.name} is used by {count} fabric item(s).",
                code=f"{self.audit_entity_type.upper()}_IN_USE",
                payload={"variantCount": count},
            )


class FabricTypeViewSet(TextileMasterViewSet):
    queryset = FabricType.objects.select_related("image_file")
    serializer_class = FabricTypeSerializer
    audit_entity_type = "FabricType"
    search_fields = TextileMasterViewSet.search_fields + ["process_stage"]
    ordering = ["code"]
    filter_map = {
        **TextileMasterViewSet.filter_map,
        "processStage": "process_stage",
    }

    def check_delete_allowed(self, fabric_type):
        count = FabricQuality.objects.filter(
            client_id=self.get_client_id(), fabric_type=fabric_type, deleted_at__isnull=True
        ).count()
        if count:
            raise Conflict(
                f"{fabric_type.name} is used by {count} quality record(s).",
                code="FABRIC_TYPE_IN_USE",
                payload={"qualityCount": count},
            )


class FabricQualityViewSet(VariantPartViewSet):
    variant_field = "quality"
    queryset = FabricQuality.objects.select_related("fabric", "fabric_type", "uom")
    serializer_class = FabricQualitySerializer
    audit_entity_type = "FabricQuality"
    search_fields = ["name", "code", "description", "composition"]
    filter_map = {
        **TextileMasterViewSet.filter_map,
        "fabricId": "fabric_id",
        "fabricTypeId": "fabric_type_id",
        "uomId": "uom_id",
    }


class DesignViewSet(VariantPartViewSet):
    variant_field = "design"
    queryset = Design.objects.select_related("image_file")
    serializer_class = DesignSerializer
    audit_entity_type = "Design"
    search_fields = TextileMasterViewSet.search_fields + ["pattern_type", "season", "reference"]
    filter_map = {
        **TextileMasterViewSet.filter_map,
        "patternType": "pattern_type",
        "season": "season",
    }


class ShadeViewSet(VariantPartViewSet):
    variant_field = "shade"
    queryset = Shade.objects.select_related("image_file")
    serializer_class = ShadeSerializer
    audit_entity_type = "Shade"
    search_fields = TextileMasterViewSet.search_fields + ["colour_type", "pantone_reference"]
    filter_map = {
        **TextileMasterViewSet.filter_map,
        "colourType": "colour_type",
    }


class FabricVariantViewSet(TextileMasterViewSet):
    """Fabric item / variant master -- ``/inventory/fabric-variants/``."""

    queryset = FabricVariant.objects.select_related(
        "quality", "quality__uom", "design", "shade", "uom", "image_file"
    )
    serializer_class = FabricVariantSerializer
    search_fields = TextileMasterViewSet.search_fields + [
        "hsn_code", "quality__name", "quality__code", "design__name", "design__code",
        "shade__name", "shade__code", "fabric_type", "construction",
    ]
    filter_map = {
        **TextileMasterViewSet.filter_map,
        "qualityId": "quality_id",
        "designId": "design_id",
        "shadeId": "shade_id",
        "fabricType": "fabric_type",
        "construction": "construction",
    }


class FabricViewSet(TenantModelViewSet):
    """Permanent fabric catalogue -- ``GET /inventory/fabrics/``.

    The "Select fabric..." dropdown reads this list. Rows are seeded per
    tenant (migration 0007 + ``seed_fabrics``), never hardcoded in the client.
    """

    queryset = Fabric.objects.all()
    serializer_class = FabricSerializer
    audit_entity_type = "Fabric"
    audit_label_field = "name"
    search_fields = ["name", "code", "description"]
    ordering = ["name"]
    status_field = None
    filter_map = {"isActive": "is_active", "is_active": "is_active"}

    def check_delete_allowed(self, fabric):
        """Block archiving while an active Fabric-kind item references it."""
        count = Item.objects.filter(
            client_id=self.get_client_id(),
            item_kind="Fabric",
            fabric_quality__iexact=fabric.name,
            deleted_at__isnull=True,
        ).count()
        if count:
            raise Conflict(
                f"{fabric.name} is used by {count} fabric item(s).",
                code="FABRIC_IN_USE",
                payload={"itemCount": count},
            )

    @action(detail=False, methods=["post"], url_path="import")
    def bulk_import(self, request):
        serializer = ImportRowsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            _import_rows(
                request,
                serializer.validated_data["rows"],
                serializer.validated_data["dryRun"],
                FabricSerializer,
                Fabric,
                required=["name"],
            )
        )

    @action(detail=False, methods=["get"], url_path="import-template")
    def import_template(self, request):
        return Response(
            {
                "headers": ["name", "code", "description"],
                "sampleRow": ["Cotton", "COTTON", "Natural fibre"],
            }
        )


# ---------------------------------------------------------------------------
# Textile & Job Work Masters (Phase 2.1)
# ---------------------------------------------------------------------------
class AgencyViewSet(TenantModelViewSet):
    queryset = Agency.objects.all()
    serializer_class = AgencySerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_agencies"],
        "update": ["manage_agencies"],
        "partial_update": ["manage_agencies"],
        "destroy": ["manage_agencies"],
    }
    audit_entity_type = "Agency"
    search_fields = ["name", "code", "contact_person", "phone", "gstin"]
    filter_map = {"isActive": "is_active", "is_active": "is_active"}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            serializer.validated_data["code"] = allocate_number(self.request.client_id, "AGY")
        return super().perform_create(serializer)


class BrokerViewSet(TenantModelViewSet):
    queryset = Broker.objects.all()
    serializer_class = BrokerSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_brokers"],
        "update": ["manage_brokers"],
        "partial_update": ["manage_brokers"],
        "destroy": ["manage_brokers"],
    }
    audit_entity_type = "Broker"
    search_fields = ["name", "code", "contact_person", "phone", "pan", "gstin"]
    filter_map = {
        "isActive": "is_active",
        "is_active": "is_active",
        "commissionType": "commission_type",
    }

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            serializer.validated_data["code"] = allocate_number(self.request.client_id, "BRK")
        return super().perform_create(serializer)


class ProcessMasterViewSet(TenantModelViewSet):
    queryset = ProcessMaster.objects.all()
    serializer_class = ProcessMasterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_processes"],
        "update": ["manage_processes"],
        "partial_update": ["manage_processes"],
        "destroy": ["manage_processes"],
    }
    audit_entity_type = "ProcessMaster"
    search_fields = ["name", "code", "category"]
    filter_map = {"isActive": "is_active", "is_active": "is_active", "category": "category"}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            name = serializer.validated_data.get("name", "")
            serializer.validated_data["code"] = slug_code(name, "PRC")
        return super().perform_create(serializer)


class JobWorkRateMasterViewSet(TenantModelViewSet):
    queryset = JobWorkRateMaster.objects.select_related("processor", "process", "fabric_quality")
    serializer_class = JobWorkRateMasterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_jobwork_rates"],
        "update": ["manage_jobwork_rates"],
        "partial_update": ["manage_jobwork_rates"],
        "destroy": ["manage_jobwork_rates"],
    }
    audit_entity_type = "JobWorkRateMaster"
    filter_map = {
        "isActive": "is_active",
        "is_active": "is_active",
        "processorId": "processor_id",
        "processId": "process_id",
        "fabricQualityId": "fabric_quality_id",
    }


class DefectMasterViewSet(TenantModelViewSet):
    queryset = DefectMaster.objects.all()
    serializer_class = DefectMasterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_defects"],
        "update": ["manage_defects"],
        "partial_update": ["manage_defects"],
        "destroy": ["manage_defects"],
    }
    audit_entity_type = "DefectMaster"
    search_fields = ["name", "code", "category", "severity"]
    filter_map = {
        "isActive": "is_active",
        "is_active": "is_active",
        "category": "category",
        "severity": "severity",
    }

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            name = serializer.validated_data.get("name", "")
            serializer.validated_data["code"] = slug_code(name, "DEF")
        return super().perform_create(serializer)


class LossReasonMasterViewSet(TenantModelViewSet):
    queryset = LossReasonMaster.objects.all()
    serializer_class = LossReasonMasterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_loss_reasons"],
        "update": ["manage_loss_reasons"],
        "partial_update": ["manage_loss_reasons"],
        "destroy": ["manage_loss_reasons"],
    }
    audit_entity_type = "LossReasonMaster"
    search_fields = ["name", "code", "description"]
    filter_map = {"isActive": "is_active", "is_active": "is_active"}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            name = serializer.validated_data.get("name", "")
            serializer.validated_data["code"] = slug_code(name, "LOSS")
        return super().perform_create(serializer)


class ReturnReasonMasterViewSet(TenantModelViewSet):
    queryset = ReturnReasonMaster.objects.all()
    serializer_class = ReturnReasonMasterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_return_reasons"],
        "update": ["manage_return_reasons"],
        "partial_update": ["manage_return_reasons"],
        "destroy": ["manage_return_reasons"],
    }
    audit_entity_type = "ReturnReasonMaster"
    search_fields = ["name", "code", "description"]
    filter_map = {"isActive": "is_active", "is_active": "is_active"}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            name = serializer.validated_data.get("name", "")
            serializer.validated_data["code"] = slug_code(name, "RET")
        return super().perform_create(serializer)


class TransporterViewSet(TenantModelViewSet):
    queryset = Transporter.objects.all()
    serializer_class = TransporterSerializer
    required_permissions = ["view_masters"]
    permission_map = {
        "list": ["view_masters"],
        "retrieve": ["view_masters"],
        "create": ["manage_masters"],
        "update": ["manage_masters"],
        "partial_update": ["manage_masters"],
        "destroy": ["manage_masters"],
    }
    audit_entity_type = "Transporter"
    search_fields = ["name", "code", "phone", "gstin", "contact_person"]
    filter_map = {"isActive": "is_active", "is_active": "is_active"}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("code"):
            name = serializer.validated_data.get("name", "")
            serializer.validated_data["code"] = slug_code(name, "TRP")
        return super().perform_create(serializer)


