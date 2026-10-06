"""Purchase serializers (api.md §6)."""
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.core.document_serializers import (
    HEADER_FIELDS,
    READ_ONLY_HEADER_FIELDS,
    DocumentLineSerializer,
    DocumentSerializer,
)
from apps.core.money import ZERO, D, round2
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    PercentField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.sales.serializers import line_serializer_for

from .models import (
    Expense,
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
    VendorBill,
    VendorBillLine,
)

PurchaseOrderLineSerializer = line_serializer_for(
    PurchaseOrderLine,
    "purchase_order_lines",
    extra_fields=["received_qty", "billed_qty"],
    extra_read_only=["received_qty", "billed_qty"],
)


class PurchaseBillLineSerializer(DocumentLineSerializer):
    """Carries the weight-receiving and landed-cost fields (api.md §6.3, §6.4)."""

    receivedQty = QuantityField(source="received_qty", required=False)
    receivedWeight = QuantityField(source="received_weight", required=False, allow_null=True)
    theoreticalWeight = QuantityField(
        source="theoretical_weight", required=False, allow_null=True, read_only=True
    )
    variationPct = serializers.DecimalField(
        source="variation_pct", max_digits=9, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )
    tolerancePct = serializers.DecimalField(
        source="tolerance_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False, allow_null=True,
    )
    isWeightItem = serializers.BooleanField(source="is_weight_item", read_only=True)
    batchNumber = serializers.CharField(
        source="batch_number", required=False, allow_null=True, allow_blank=True
    )
    landedUnitCost = serializers.DecimalField(
        source="landed_unit_cost", max_digits=18, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseBillLine
        fields = DocumentLineSerializer.Meta.fields + [
            "receivedQty", "receivedWeight", "theoreticalWeight", "variationPct",
            "tolerancePct", "isWeightItem", "batchNumber", "returned_qty",
            "base_unit_cost", "apportioned_cost", "landedUnitCost",
        ]
        read_only_fields = DocumentLineSerializer.Meta.read_only_fields + [
            "returned_qty", "base_unit_cost", "apportioned_cost",
        ]


# ---------------------------------------------------------------------------
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrderSerializer(DocumentSerializer):
    line_model = PurchaseOrderLine
    line_serializer = PurchaseOrderLineSerializer
    line_fk_name = "purchase_order"
    line_table_name = "purchase_order_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    #: The UI shows the derived billed status, not the stored one (api.md §6.2).
    billedStatus = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = HEADER_FIELDS + [
            "po_number", "status", "expected_date", "location",
            "reference_number", "auto_generated", "vendorId", "vendorName",
            "billedStatus",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["po_number", "auto_generated"]


    def get_billedStatus(self, order):
        from .services import po_billed_status

        # Only computed on the detail view -- running it per row would make the
        # list N+1 (db.md §15).
        if self.context.get("include_billed_status"):
            return po_billed_status(order)
        return None


class PurchaseBillSerializer(DocumentSerializer):
    line_model = PurchaseBillLine
    line_serializer = PurchaseBillLineSerializer
    line_fk_name = "purchase_bill"
    line_table_name = "purchase_bill_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    vendorBillNumber = serializers.CharField(
        source="vendor_bill_number", required=False, allow_null=True, allow_blank=True
    )
    goodsReceived = serializers.BooleanField(source="goods_received", read_only=True)
    qcStatus = serializers.CharField(source="qc_status", read_only=True)

    class Meta:
        model = PurchaseBill
        fields = HEADER_FIELDS + [
            "bill_number", "vendorBillNumber", "status", "due_date",
            "purchase_order", "location", "goodsReceived", "received_date",
            "qcStatus", "qc_note", "vendorId", "vendorName",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "bill_number", "received_date", "qc_note",
        ]



class ReceiveGoodsSerializer(BaseSerializer):
    """``POST /purchase/bills/{id}/receive-goods/`` (api.md §6.4).

    A line may be addressed by ``lineId``, ``lineIndex`` or ``sku`` -- all three
    appear in the frontend's call sites.
    """

    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    qcStatus = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"],
        required=False,
        allow_null=True,
    )
    locationId = serializers.CharField(required=False, allow_null=True)


class QcSerializer(BaseSerializer):
    status = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"]
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class GoodsReceiptLineSerializer(BaseModelSerializer):
    itemId = serializers.CharField(source="item_id", read_only=True)
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = GoodsReceiptLine
        fields = [
            "id", "itemId", "sku", "itemName", "ordered_qty", "received_qty",
            "weighed_qty", "rejected_qty", "batch_number", "unit_cost",
            "variation_pct",
        ]


class GoodsReceiptSerializer(BaseModelSerializer):
    lines = GoodsReceiptLineSerializer(many=True, read_only=True)
    vendorName = serializers.CharField(source="party.name", read_only=True)
    billNumber = serializers.CharField(
        source="purchase_bill.bill_number", read_only=True
    )

    class Meta:
        model = GoodsReceipt
        fields = [
            "id", "grn_number", "purchase_order", "purchase_bill", "billNumber",
            "party", "vendorName", "receipt_date", "location", "qc_status",
            "qc_note", "qc_at", "lines", "created_at",
        ]


# ---------------------------------------------------------------------------
# Payments out, returns, expenses
# ---------------------------------------------------------------------------
class PaymentOutSerializer(BaseModelSerializer):
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    unallocatedAmount = serializers.SerializerMethodField()

    class Meta:
        model = PaymentOut
        fields = [
            "id", "payment_number", "vendorId", "vendorName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "notes",
            "allocated_amount", "unallocatedAmount", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class PurchaseReturnLineSerializer(DocumentLineSerializer):
    purchaseBillLineId = serializers.PrimaryKeyRelatedField(
        source="purchase_bill_line", queryset=PurchaseBillLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseReturnLine
        fields = DocumentLineSerializer.Meta.fields + ["purchaseBillLineId", "returnedQty"]


class PurchaseReturnSerializer(DocumentSerializer):
    line_model = PurchaseReturnLine
    line_serializer = PurchaseReturnLineSerializer
    line_fk_name = "purchase_return"
    line_table_name = "purchase_return_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    purchaseBillId = TenantPrimaryKeyRelatedField(
        source="purchase_bill", queryset=PurchaseBill.objects.all()
    )

    class Meta:
        model = PurchaseReturn
        fields = HEADER_FIELDS + [
            "return_number", "debit_note_number", "status", "purchaseBillId",
            "reason", "location", "vendorId", "vendorName",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "debit_note_number",
        ]


class ExpenseSerializer(BaseModelSerializer):
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="accounting.ExpenseCategory", required=False, allow_null=True
    )
    categoryName = serializers.CharField(source="category.name", read_only=True)
    vendorId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    receiptFileId = TenantPrimaryKeyRelatedField(
        source="receipt_file", model="core.File", required=False, allow_null=True
    )
    date = serializers.DateField(source="expense_date")

    class Meta:
        model = Expense
        fields = [
            "id", "expense_number", "categoryId", "categoryName", "vendorId",
            "vendorName", "date", "amount", "tax_amount", "total", "payment_mode",
            "bankAccountId", "receiptFileId", "account", "reference_number",
            "notes", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["expense_number", "total", "created_at", "updated_at"]



class BillOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    paid = MoneyField()
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()


# ---------------------------------------------------------------------------
# Vendor bills -- manual entry, matched later to a real PurchaseBill
# ---------------------------------------------------------------------------
class VendorBillLineSerializer(BaseModelSerializer):
    lineNo = serializers.IntegerField(source="line_no", required=False)
    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    itemName = serializers.CharField(
        source="item_name", required=False, allow_null=True, allow_blank=True
    )
    qty = QuantityField()
    rate = QuantityField(required=False)
    lineTotal = MoneyField(source="line_total", read_only=True)

    class Meta:
        model = VendorBillLine
        fields = [
            "id", "lineNo", "itemId", "sku", "itemName", "uom",
            "qty", "rate", "amount", "lineTotal",
        ]
        read_only_fields = ["amount"]

    def validate(self, attrs):
        qty = attrs.get("qty")
        if qty is not None and D(qty) <= ZERO:
            raise serializers.ValidationError(
                {"qty": "Quantity must be greater than zero."}
            )
        rate = attrs.get("rate")
        if rate is not None and D(rate) < ZERO:
            raise serializers.ValidationError(
                {"rate": "Rate cannot be negative."}
            )
        return attrs


class VendorBillSerializer(BaseModelSerializer):
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    vendorBillNumber = serializers.CharField(source="vendor_bill_number")
    billDate = serializers.DateField(source="bill_date")
    purchaseOrderId = TenantPrimaryKeyRelatedField(
        source="purchase_order", model="purchase.PurchaseOrder",
        required=False, allow_null=True,
    )
    grnId = TenantPrimaryKeyRelatedField(
        source="goods_receipt", model="purchase.GoodsReceipt",
        required=False, allow_null=True,
    )
    attachmentFileId = TenantPrimaryKeyRelatedField(
        source="attachment", model="core.File", required=False, allow_null=True
    )
    billFileName = serializers.CharField(source="attachment.file_name", read_only=True)
    purchaseBillId = serializers.CharField(source="purchase_bill_id", read_only=True)
    purchaseBillNumber = serializers.CharField(
        source="purchase_bill.bill_number", read_only=True
    )
    gstPct = PercentField(source="gst_pct", required=False)
    subtotal = MoneyField(read_only=True)
    gstAmount = MoneyField(source="gst_amount", read_only=True)
    total = MoneyField(read_only=True)
    matchStatus = serializers.CharField(source="match_status", read_only=True)
    remarks = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    approvalRemarks = serializers.CharField(
        source="approval_remarks", read_only=True
    )
    approvedAt = serializers.DateTimeField(source="approved_at", read_only=True)
    approvedByName = serializers.SerializerMethodField()
    approvalHistory = serializers.JSONField(
        source="approval_history", read_only=True
    )
    matchResult = serializers.JSONField(source="match_result", read_only=True)
    lineItems = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    class Meta:
        model = VendorBill
        fields = [
            "id", "vendorBillNumber", "billDate", "vendorId", "vendorName",
            "purchaseOrderId", "grnId", "attachmentFileId", "billFileName",
            "purchaseBillId", "purchaseBillNumber", "gstPct", "subtotal",
            "gstAmount", "total", "remarks", "status", "matchStatus",
            "approvalRemarks", "approvedAt", "approvedByName",
            "approvalHistory", "matchResult",
            "lineItems", "created_at", "updated_at",
        ]
        read_only_fields = [
            "subtotal", "gstAmount", "total", "matchStatus",
            "approvalRemarks", "approvedAt", "approvedByName",
            "approvalHistory", "matchResult",
            "created_at", "updated_at",
        ]

    def get_approvedByName(self, bill):
        user = getattr(bill, "approved_by", None)
        return getattr(user, "name", None) if user is not None else None

    def validate(self, attrs):
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or self.context.get("client_id")
        number = attrs.get(
            "vendor_bill_number",
            getattr(self.instance, "vendor_bill_number", None),
        )
        if number is not None and client_id is not None:
            duplicates = VendorBill.objects.filter(
                client_id=client_id,
                vendor_bill_number=number,
                deleted_at__isnull=True,
            )
            if self.instance is not None:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError(
                    {"vendorBillNumber": "A vendor bill with this number already exists."}
                )
        gst_pct = attrs.get("gst_pct", getattr(self.instance, "gst_pct", None))
        if gst_pct is not None and D(gst_pct) < ZERO:
            raise serializers.ValidationError(
                {"gstPct": "GST % cannot be negative."}
            )
        lines = attrs.get("lineItems", None)
        if (self.instance is None and not lines) or lines == []:
            raise serializers.ValidationError(
                {"lineItems": "Add at least one line."}
            )
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        lines = instance.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        data["lineItems"] = VendorBillLineSerializer(
            lines, many=True, context=self.context
        ).data
        return data

    def create(self, validated_data):
        line_payloads = validated_data.pop("lineItems", [])
        bill = super().create(validated_data)
        bill.freeze_party_snapshot()
        bill.save(update_fields=["party_name", "updated_at"])
        self._write_lines(bill, line_payloads)
        self._recalculate(bill)
        return bill

    def update(self, instance, validated_data):
        line_payloads = validated_data.pop("lineItems", None)
        bill = super().update(instance, validated_data)
        if "party" in validated_data:
            bill.freeze_party_snapshot()
            bill.save(update_fields=["party_name", "updated_at"])
        if line_payloads is not None:
            self._replace_lines(bill, line_payloads)
        self._recalculate(bill)
        return bill

    def _write_lines(self, bill, payloads):
        for index, payload in enumerate(payloads or [], start=1):
            line_serializer = VendorBillLineSerializer(
                data=payload, context=self.context
            )
            line_serializer.is_valid(raise_exception=True)
            data = dict(line_serializer.validated_data)
            data.setdefault("line_no", index)
            line = VendorBillLine(
                client_id=bill.client_id, vendor_bill=bill, **data
            )
            line.freeze_item_snapshot()
            line.save()

    def _replace_lines(self, bill, payloads):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            user = None
        for old in bill.line_items.filter(deleted_at__isnull=True):
            old.deleted_at = timezone.now()
            old.deleted_by = user
            old.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
        self._write_lines(bill, payloads)

    def _recalculate(self, bill):
        lines = list(
            bill.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        )
        subtotal = ZERO
        for line in lines:
            line.amount = round2(D(line.qty) * D(line.rate))
            line.line_total = line.amount
            subtotal += line.amount
        if lines:
            VendorBillLine.objects.bulk_update(
                lines, ["amount", "line_total", "updated_at"]
            )
        bill.subtotal = round2(subtotal)
        bill.gst_amount = round2(
            bill.subtotal * D(bill.gst_pct) / Decimal("100")
        )
        bill.total = round2(bill.subtotal + bill.gst_amount)
        bill.save(
            update_fields=["subtotal", "gst_amount", "total", "updated_at"]
        )
