"""Sales serializers (api.md §5)."""
from rest_framework import serializers

from apps.core.document_serializers import (
    HEADER_FIELDS,
    READ_ONLY_HEADER_FIELDS,
    DocumentLineSerializer,
    DocumentSerializer,
)
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from . import services
from .models import (
    DeliveryChallan,
    DeliveryChallanLine,
    PaymentAllocation,
    PaymentIn,
    ProformaInvoice,
    ProformaInvoiceLine,
    Quotation,
    QuotationActivity,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
    WarrantyCard,
    WarrantyCardItem,
)


def line_serializer_for(line_model, table_name, extra_fields=(), extra_read_only=()):
    """Build the line serializer for one document type.

    The parameter is not named ``model``: a class body does not close over the
    enclosing function's locals, so ``model = model`` inside ``class Meta``
    would resolve to the name being defined, not the argument.
    """

    class _LineSerializer(DocumentLineSerializer):
        line_table_name = table_name

        class Meta(DocumentLineSerializer.Meta):
            model = line_model
            fields = DocumentLineSerializer.Meta.fields + list(extra_fields)
            read_only_fields = DocumentLineSerializer.Meta.read_only_fields + list(
                extra_read_only
            )

    _LineSerializer.__name__ = f"{line_model.__name__}Serializer"
    return _LineSerializer


QuotationLineSerializer = line_serializer_for(QuotationLine, "quotation_lines")
SalesOrderLineSerializer = line_serializer_for(
    SalesOrderLine,
    "sales_order_lines",
    extra_fields=["dispatched_qty", "invoiced_qty"],
    extra_read_only=["dispatched_qty", "invoiced_qty"],
)
ProformaInvoiceLineSerializer = line_serializer_for(
    ProformaInvoiceLine, "proforma_invoice_lines"
)
DeliveryChallanLineSerializer = line_serializer_for(
    DeliveryChallanLine, "delivery_challan_lines"
)
SalesInvoiceLineSerializer = line_serializer_for(
    SalesInvoiceLine, "sales_invoice_lines",
    extra_fields=["returned_qty"], extra_read_only=["returned_qty"],
)


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class QuotationSerializer(DocumentSerializer):
    line_model = QuotationLine
    line_serializer = QuotationLineSerializer
    line_fk_name = "quotation"
    line_table_name = "quotation_lines"

    class Meta:
        model = Quotation
        fields = HEADER_FIELDS + [
            "quotation_number", "status", "valid_until", "subject",
            "crm_deal", "crm_lead",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["quotation_number"]


class QuotationActivitySerializer(BaseModelSerializer):
    class Meta:
        model = QuotationActivity
        fields = ["id", "event", "actor_label", "comment", "ip", "created_at"]


class ShareRequestSerializer(BaseSerializer):
    expiryDays = serializers.IntegerField(required=False, default=14, min_value=1, max_value=365)
    recipients = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    channel = serializers.ChoiceField(
        choices=["email", "whatsapp", "link"], required=False, default="link"
    )


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrderSerializer(DocumentSerializer):
    line_model = SalesOrderLine
    line_serializer = SalesOrderLineSerializer
    line_fk_name = "sales_order"
    line_table_name = "sales_order_lines"

    class Meta:
        model = SalesOrder
        fields = HEADER_FIELDS + [
            "order_number", "stage", "payment_status", "delivery_date",
            "quotation", "pms_project", "reference_number",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["order_number", "payment_status"]


class ConvertLinesSerializer(BaseSerializer):
    """Partial conversion (api.md §5.4): ``{ lines: [{ lineId, qty, serials[] }] }``."""

    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    date = serializers.DateField(required=False)
    locationId = serializers.CharField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoiceSerializer(DocumentSerializer):
    line_model = ProformaInvoiceLine
    line_serializer = ProformaInvoiceLineSerializer
    line_fk_name = "proforma_invoice"
    line_table_name = "proforma_invoice_lines"

    class Meta:
        model = ProformaInvoice
        fields = HEADER_FIELDS + [
            "proforma_number", "status", "valid_until", "sales_order",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["proforma_number"]


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallanSerializer(DocumentSerializer):
    line_model = DeliveryChallanLine
    line_serializer = DeliveryChallanLineSerializer
    line_fk_name = "delivery_challan"
    line_table_name = "delivery_challan_lines"

    class Meta:
        model = DeliveryChallan
        fields = HEADER_FIELDS + [
            "challan_number", "status", "sales_order", "quotation",
            "dispatch_date", "vehicle_number", "transporter", "lr_number",
            "delivery_location", "location", "delivered_at",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["challan_number", "delivered_at"]


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoiceSerializer(DocumentSerializer):
    line_model = SalesInvoiceLine
    line_serializer = SalesInvoiceLineSerializer
    line_fk_name = "sales_invoice"
    line_table_name = "sales_invoice_lines"

    #: ``Overdue`` is layered on at read time, never stored (db.md §12).
    displayStatus = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = HEADER_FIELDS + [
            "invoice_number", "status", "displayStatus", "due_date",
            "sales_order", "delivery_challan", "proforma_invoice", "location",
            "irn", "eway_bill_number",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["invoice_number", "status"]

    def get_displayStatus(self, invoice):
        return services.display_status(invoice)


class InvoiceOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    paid = MoneyField()
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()


# ---------------------------------------------------------------------------
# Payments in (api.md §5.8)
# ---------------------------------------------------------------------------
class PaymentAllocationSerializer(BaseModelSerializer):
    documentId = serializers.CharField(source="document_id", read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "document_type", "documentId", "amount", "allocated_at"]


class PaymentInSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    allocations = serializers.SerializerMethodField()
    unallocatedAmount = serializers.SerializerMethodField()
    #: Write-only: the target invoice(s) this payment settles.
    invoiceId = serializers.CharField(required=False, allow_null=True, write_only=True)
    allocationsInput = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    class Meta:
        model = PaymentIn
        fields = [
            "id", "payment_number", "customerId", "customerName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "notes",
            "allocated_amount", "unallocatedAmount", "allocations", "status",
            "invoiceId", "allocationsInput", "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def get_allocations(self, payment):
        rows = PaymentAllocation.objects.filter(
            client_id=payment.client_id,
            payment_side="in",
            payment_id=payment.id,
            deleted_at__isnull=True,
        )
        return PaymentAllocationSerializer(rows, many=True).data

    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class AllocateSerializer(BaseSerializer):
    allocations = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# ---------------------------------------------------------------------------
# Sales returns (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturnLineSerializer(DocumentLineSerializer):
    salesInvoiceLineId = serializers.PrimaryKeyRelatedField(
        source="sales_invoice_line", queryset=SalesInvoiceLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")

    class Meta(DocumentLineSerializer.Meta):
        model = SalesReturnLine
        fields = DocumentLineSerializer.Meta.fields + ["salesInvoiceLineId", "returnedQty"]


class SalesReturnSerializer(DocumentSerializer):
    line_model = SalesReturnLine
    line_serializer = SalesReturnLineSerializer
    line_fk_name = "sales_return"
    line_table_name = "sales_return_lines"

    salesInvoiceId = TenantPrimaryKeyRelatedField(
        source="sales_invoice", queryset=SalesInvoice.objects.all()
    )

    class Meta:
        model = SalesReturn
        fields = HEADER_FIELDS + [
            "return_number", "credit_note_number", "status", "salesInvoiceId",
            "reason", "location",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "credit_note_number",
        ]


# ---------------------------------------------------------------------------
# Warranty cards (api.md §5.10)
# ---------------------------------------------------------------------------
class WarrantyCardItemSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
    serials = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )

    class Meta:
        model = WarrantyCardItem
        fields = ["id", "itemId", "sku", "item_name", "qty", "serials"]



class WarrantyCardSerializer(BaseModelSerializer):
    """Two independent status fields -- do not collapse them (api.md §5.10)."""

    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    challanNumber = serializers.CharField(
        source="delivery_challan.challan_number", read_only=True
    )
    invoiceNumber = serializers.CharField(
        source="sales_invoice.invoice_number", read_only=True
    )
    #: DERIVED on every read (api.md §5.10) -- never stored.
    coverageStatus = serializers.SerializerMethodField()
    items = WarrantyCardItemSerializer(many=True, required=False)

    class Meta:
        model = WarrantyCard
        fields = [
            "id", "card_number", "customerId", "customerName", "contact_person",
            "billing_address", "shipping_address", "gstin",
            "delivery_challan", "challanNumber", "sales_invoice", "invoiceNumber",
            "sales_order", "delivery_date", "delivery_location",
            "warranty_period", "warranty_unit", "warranty_start_event",
            "start_date", "expiry_date", "expiring_soon_days",
            "document_status", "coverageStatus",
            "suspended_reason", "cancelled_reason", "void_reason",
            "terms", "notes", "items", "created_at", "updated_at",
        ]
        read_only_fields = ["card_number", "created_at", "updated_at"]


    def get_coverageStatus(self, card):
        return services.coverage_status(card)

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        card = super().create(validated_data)
        self._write_items(card, items)
        return card

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        card = super().update(instance, validated_data)
        if items is not None:
            card.items.all().delete()
            self._write_items(card, items)
        return card

    def _write_items(self, card, rows):
        from apps.inventory.services import resolve_serials
        from .models import WarrantyCardSerial

        for row in rows:
            serial_numbers = row.pop("serials", [])
            item = row.get("item")
            line = WarrantyCardItem.objects.create(
                client_id=card.client_id,
                warranty_card=card,
                sku=item.sku if item else row.get("sku"),
                item_name=item.name if item else row.get("item_name"),
                **row,
            )
            if serial_numbers and item is not None:
                resolved = resolve_serials(
                    card.client_id, item.id, serial_numbers, expected_status=None
                )
                WarrantyCardSerial.objects.bulk_create(
                    [
                        WarrantyCardSerial(
                            client_id=card.client_id, warranty_card=card, serial=serial
                        )
                        for serial in resolved
                    ],
                    ignore_conflicts=True,
                )


class ReasonSerializer(BaseSerializer):
    reason = serializers.CharField(required=False, allow_blank=True)
