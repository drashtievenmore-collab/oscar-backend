"""
Shared serializer machinery for sales and purchase documents (db.md §3).

The twelve document types differ in their status machine and their upstream
FKs, not in how a line is written. This module holds the part that is genuinely
common: the frozen line snapshot, the BOM flags, the serial selection, and the
recompute-on-write rule of api.md §5.7.
"""
from rest_framework import serializers

from .serializers import BaseModelSerializer, TenantPrimaryKeyRelatedField

#: The frozen and computed line fields, in the order the UI renders them.
LINE_FIELDS = [
    "id", "line_no", "itemId", "sku", "itemName", "description", "hsn_code", "uom",
    "uom_factor", "qty", "rate", "amount", "discount", "discount_amount", "tax", "tax_amount",
    "line_total", "serials",
    "fabricQualityId", "fabricQualityName", "designId", "designName", "shadeId", "shadeName",
    "lotPreference", "takaCount", "landedCost", "marginPct", "belowCost", "belowMargin", "reservedTakas",
    "isBomGenerated", "bomSourceItemId", "parentLineId", "isBomPart",
    "isUserModified", "parent_sku",
]

#: Header fields every document exposes.
HEADER_FIELDS = [
    "id", "partyId", "partyName", "party_gstin", "billing_address",
    "shipping_address", "place_of_supply", "date", "notes", "terms",
    "salesperson", "salespersonId", "salespersonName",
    "broker", "brokerId", "brokerName",
    "transportResponsibility", "payment_terms", "paymentTerms", "credit_days", "creditDays",
    "subtotal", "total_discount", "taxable_value", "cgst", "sgst", "igst", "cess",
    "total_tax", "freight_charges", "other_charges", "round_off", "total",
    "amount_paid", "balance_due", "discount_override",
    "posted_at", "cancelled_at", "cancellation_reason",
    "lineItems", "created_at", "updated_at",
]

READ_ONLY_HEADER_FIELDS = [
    "subtotal", "total_discount", "taxable_value", "cgst", "sgst", "igst", "cess",
    "total_tax", "total", "amount_paid", "balance_due", "posted_at",
    "cancelled_at", "cancellation_reason", "created_at", "updated_at",
]


class DocumentLineSerializer(BaseModelSerializer):
    """One line of any sales or purchase document.

    Money fields are read-only outputs of the server's recompute: the client
    may send ``qty``, ``rate``, ``discount`` and ``tax``, and gets back
    ``amount``, ``taxAmount`` and ``lineTotal`` as the server computed them
    (api.md §5.7).
    """

    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    itemName = serializers.CharField(source="item_name", required=False, allow_null=True)
    # JSON names `discount` and `tax` map to the `_pct` columns (db.md §3.2).
    discount = serializers.DecimalField(
        source="discount_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False,
    )
    tax = serializers.DecimalField(
        source="tax_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False,
    )
    isBomGenerated = serializers.BooleanField(source="is_bom_generated", required=False)
    bomSourceItemId = TenantPrimaryKeyRelatedField(
        source="bom_source_item", model="masters.Item", required=False, allow_null=True
    )
    # Self-reference to a sibling line of the *same* document. Carried as an
    # opaque id rather than a relation: the concrete line model differs per
    # document type, and api-integration.md §10.2 already treats every id as an
    # opaque string. The parent line is resolved in the write path, where the
    # whole line set is present.
    parentLineId = serializers.UUIDField(
        source="parent_line_id", required=False, allow_null=True
    )
    isBomPart = serializers.SerializerMethodField()
    isUserModified = serializers.BooleanField(source="is_user_modified", required=False)
    serials = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )
    fabricQualityId = TenantPrimaryKeyRelatedField(
        source="fabric_quality", model="masters.FabricQuality", required=False, allow_null=True
    )
    fabricQualityName = serializers.CharField(source="fabric_quality.name", read_only=True)
    designId = TenantPrimaryKeyRelatedField(
        source="design", model="masters.Design", required=False, allow_null=True
    )
    designName = serializers.CharField(source="design.name", read_only=True)
    shadeId = TenantPrimaryKeyRelatedField(
        source="shade", model="masters.Shade", required=False, allow_null=True
    )
    shadeName = serializers.CharField(source="shade.name", read_only=True)
    lotPreference = serializers.CharField(source="lot_preference", required=False, allow_blank=True, allow_null=True)
    takaCount = serializers.IntegerField(source="taka_count", required=False, default=0)
    landedCost = serializers.DecimalField(source="landed_cost", max_digits=18, decimal_places=4, required=False, default=0)
    marginPct = serializers.DecimalField(source="margin_pct", max_digits=7, decimal_places=2, required=False, default=0)
    belowCost = serializers.BooleanField(source="below_cost", required=False, default=False)
    belowMargin = serializers.BooleanField(source="below_margin", required=False, default=False)
    reservedTakas = serializers.ListField(source="reserved_takas", required=False, default=list)

    class Meta:
        fields = LINE_FIELDS
        read_only_fields = ["amount", "discount_amount", "tax_amount", "line_total", "uom_factor"]

    def get_isBomPart(self, line):
        """db.md §3.2 -- derived as ``parent_line_id is not null``."""
        return line.parent_line_id is not None


class DocumentSerializer(BaseModelSerializer):
    """Header + nested lines, with the line write path handled once.

    Subclasses declare ``line_model``, ``line_serializer`` and
    ``line_fk_name``; everything else -- freezing the party snapshot, writing
    the lines, recomputing the totals and persisting serial selections -- is
    the same for every document.
    """

    partyId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    partyName = serializers.CharField(source="party_name", read_only=True)
    date = serializers.DateField(source="doc_date")
    salespersonId = TenantPrimaryKeyRelatedField(
        source="salesperson_fk", model="hrms.Employee", required=False, allow_null=True
    )
    salespersonName = serializers.CharField(source="salesperson_fk.name", read_only=True)
    brokerId = TenantPrimaryKeyRelatedField(
        source="broker_fk", model="masters.Broker", required=False, allow_null=True
    )
    brokerName = serializers.CharField(source="broker_fk.name", read_only=True)
    transportResponsibility = serializers.CharField(
        source="transport_responsibility", required=False, default="Company"
    )
    paymentTerms = serializers.CharField(source="payment_terms", required=False, allow_blank=True, allow_null=True)
    creditDays = serializers.IntegerField(source="credit_days", required=False, allow_null=True)
    balance_due = serializers.SerializerMethodField()
    lineItems = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    #: Overridden by each concrete document.
    line_model = None
    line_serializer = None
    line_fk_name = None
    line_table_name = None

    def get_balance_due(self, document):
        return document.balance_due

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("lineItems", None)
        lines = getattr(instance, "_prefetched_lines", None)
        if lines is None:
            lines = instance.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        serialized = self.line_serializer(lines, many=True, context=self.context).data
        self._attach_serials(instance, lines, serialized)
        data["lineItems"] = serialized
        return data

    def _attach_serials(self, instance, lines, serialized):
        if not self.line_table_name:
            return
        from apps.inventory.services import serials_for_lines

        line_ids = [line.id for line in lines]
        if not line_ids:
            return
        grouped = serials_for_lines(instance.client_id, self.line_table_name, line_ids)
        for line, payload in zip(lines, serialized):
            payload["serials"] = grouped.get(line.id, [])

    #: Header amounts that only balance once the lines exist. The db.md §3.1
    #: total check is a plain CHECK constraint, which Postgres evaluates per
    #: statement and cannot defer -- so a header must never be written with
    #: charges before its totals have been computed. These are held back and
    #: applied during the recompute.
    DEFERRED_AMOUNT_FIELDS = (
        "freight_charges",
        "other_charges",
        "round_off",
        "discount_override",
    )

    # -- write path ---------------------------------------------------------
    def create(self, validated_data):
        line_payloads = validated_data.pop("lineItems", [])
        deferred = {
            field: validated_data.pop(field)
            for field in self.DEFERRED_AMOUNT_FIELDS
            if field in validated_data
        }

        # Sync salesperson text and salesperson_fk
        sp_obj = validated_data.get("salesperson_fk")
        sp_text = validated_data.get("salesperson")
        if sp_obj and not sp_text:
            validated_data["salesperson"] = sp_obj.name
        elif sp_text and not sp_obj:
            from apps.hrms.models import Employee
            client_id = getattr(self.context.get("request"), "client_id", None)
            if client_id:
                matched_emp = Employee.objects.filter(
                    client_id=client_id, name__iexact=str(sp_text).strip(), deleted_at__isnull=True
                ).first()
                if matched_emp:
                    validated_data["salesperson_fk"] = matched_emp

        document = super().create(validated_data)
        document.freeze_party_snapshot()
        document.save()

        self._write_lines(document, line_payloads, replace=False)

        for field, value in deferred.items():
            setattr(document, field, value)
        self._recalculate(document)
        return document

    def update(self, instance, validated_data):
        line_payloads = validated_data.pop("lineItems", None)
        sp_obj = validated_data.get("salesperson_fk")
        sp_text = validated_data.get("salesperson")
        if sp_obj and not sp_text:
            validated_data["salesperson"] = sp_obj.name
        elif sp_text and not sp_obj:
            from apps.hrms.models import Employee
            matched_emp = Employee.objects.filter(
                client_id=instance.client_id, name__iexact=str(sp_text).strip(), deleted_at__isnull=True
            ).first()
            if matched_emp:
                validated_data["salesperson_fk"] = matched_emp
        document = super().update(instance, validated_data)
        if line_payloads is not None:
            self._write_lines(document, line_payloads, replace=True)
        self._recalculate(document)
        return document

    def _write_lines(self, document, payloads, replace):
        from apps.inventory.services import link_line_serials, resolve_serials

        if replace:
            # Drafts cascade; posted documents are never edited (db.md §3.2).
            document.line_items.all().delete()

        for index, payload in enumerate(payloads or [], start=1):
            line_serializer = self.line_serializer(
                data=payload, context=self.context
            )
            line_serializer.is_valid(raise_exception=True)
            data = dict(line_serializer.validated_data)
            serial_numbers = data.pop("serials", [])
            data.setdefault("line_no", index)

            line = self.line_model(
                client_id=document.client_id,
                **{self.line_fk_name: document},
                **data,
            )
            line.freeze_item_snapshot()
            line.save()

            if serial_numbers and self.line_table_name:
                resolved = resolve_serials(
                    document.client_id, line.item_id, serial_numbers, expected_status=None
                )
                link_line_serials(
                    document.client_id, self.line_table_name, line.id, resolved
                )

    def _recalculate(self, document):
        from apps.sales.services import assert_client_totals_match, recalculate_document

        recalculate_document(document)
        request = self.context.get("request")
        if request is not None and isinstance(request.data, dict):
            assert_client_totals_match(document, request.data.get("totals"))
