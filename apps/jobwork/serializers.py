"""Serializers for the jobwork module."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    JWOCharge,
    JWOInward,
    JWOMaterial,
    JWOOutward,
    JWOReprocess,
    JobWorkOrder,
    PlanCharge,
    ProcessPlan,
    VendorProcessInstruction,
    VPIProgressEntry,
)


class PlanChargeSerializer(BaseModelSerializer):
    class Meta:
        model = PlanCharge
        fields = ["id", "type", "description", "rate", "qty", "amount", "status"]
        read_only_fields = ["id"]


class ProcessPlanSerializer(BaseModelSerializer):
    planNo = serializers.CharField(source="plan_no", required=False)
    processCategory = serializers.CharField(
        source="process_category", required=False, allow_null=True, allow_blank=True
    )
    vendor = serializers.CharField(source="vendor_name", required=False, allow_blank=True)
    contactPerson = serializers.CharField(
        source="contact_person", required=False, allow_null=True, allow_blank=True
    )
    fabricItem = serializers.CharField(source="fabric_item", required=False, allow_blank=True)
    greyLotNo = serializers.CharField(
        source="grey_lot_no", required=False, allow_null=True, allow_blank=True
    )
    fabricQuality = serializers.CharField(
        source="fabric_quality", required=False, allow_blank=True
    )
    takaRollNo = serializers.CharField(
        source="taka_roll_no", required=False, allow_null=True, allow_blank=True
    )
    expectedQty = QuantityField(source="expected_qty", required=False)
    expectedLoss = QuantityField(source="expected_loss", required=False)
    expectedReturnQty = QuantityField(source="expected_return_qty", required=False)
    weight = QuantityField(source="weight_kg", required=False, allow_null=True)
    finishedWeight = QuantityField(source="finished_weight_kg", required=False, allow_null=True)
    noOfRolls = serializers.IntegerField(source="no_of_rolls", required=False, allow_null=True)
    startDate = serializers.DateField(source="start_date", required=False, allow_null=True)
    expectedCompletionDate = serializers.DateField(
        source="expected_completion_date", required=False, allow_null=True
    )
    targetDate = serializers.DateField(
        source="target_date", required=False, allow_null=True
    )
    assignedEmployee = serializers.CharField(
        source="assigned_employee", required=False, allow_null=True, allow_blank=True
    )
    charges = PlanChargeSerializer(many=True, required=False)

    class Meta:
        model = ProcessPlan
        fields = [
            "id", "planNo", "date", "process", "processCategory", "vendor",
            "contactPerson", "phone", "email", "address",
            "fabricItem", "greyLotNo", "fabricQuality", "takaRollNo", "shade",
            "expectedQty", "expectedLoss", "expectedReturnQty",
            "weight", "finishedWeight", "noOfRolls",
            "startDate", "expectedCompletionDate", "targetDate",
            "assignedEmployee", "approver",
            "status", "remarks", "charges",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "planNo", "created_at", "updated_at"]


class JWOMaterialSerializer(BaseModelSerializer):
    fabricItem = serializers.CharField(source="fabric_item", required=False, allow_blank=True)
    fabricQuality = serializers.CharField(source="fabric_quality", required=False, allow_blank=True)
    lotNo = serializers.CharField(source="lot_no", required=False, allow_blank=True, allow_null=True)
    qty = QuantityField(required=False)
    rate = MoneyField(required=False)
    amount = MoneyField(required=False)

    class Meta:
        model = JWOMaterial
        fields = [
            "id", "fabricItem", "fabricQuality", "shade", "lotNo",
            "qty", "rate", "amount",
        ]
        read_only_fields = ["id"]


class JWOOutwardSerializer(BaseModelSerializer):
    lrNo = serializers.CharField(source="lr_no", required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = JWOOutward
        fields = ["id", "no", "date", "qty", "lrNo", "transporter", "status"]
        read_only_fields = ["id"]


class JWOInwardSerializer(BaseModelSerializer):
    challanNo = serializers.CharField(
        source="challan_no", required=False, allow_blank=True, allow_null=True
    )

    class Meta:
        model = JWOInward
        fields = ["id", "no", "date", "qty", "accepted", "rejected", "challanNo", "status"]
        read_only_fields = ["id"]


class JWOChargeSerializer(BaseModelSerializer):
    class Meta:
        model = JWOCharge
        fields = ["id", "type", "description", "rate", "qty", "amount", "status"]
        read_only_fields = ["id"]


class JWOReprocessSerializer(BaseModelSerializer):
    inwardNo = serializers.CharField(
        source="inward_no", required=False, allow_blank=True
    )
    expectedReturn = serializers.DateField(
        source="expected_return", required=False, allow_null=True
    )

    class Meta:
        model = JWOReprocess
        fields = [
            "id", "no", "inwardNo", "date", "qty", "reason",
            "expectedReturn", "status", "remarks",
        ]
        read_only_fields = ["id"]


class JobWorkOrderSerializer(BaseModelSerializer):
    jwoNo = serializers.CharField(source="jwo_no", required=False)
    processPlanId = serializers.UUIDField(
        source="process_plan_id", required=False, allow_null=True
    )
    vendor = serializers.CharField(source="vendor_name", required=False, allow_blank=True)
    orderDate = serializers.DateField(source="order_date", required=False)
    plannedQty = QuantityField(source="planned_qty", required=False)
    rate = MoneyField(required=False)
    totalAmount = MoneyField(source="total_amount", required=False)
    expectedCompletion = serializers.DateField(
        source="expected_completion", required=False, allow_null=True
    )
    materials = JWOMaterialSerializer(many=True, required=False)
    outwards = JWOOutwardSerializer(many=True, required=False)
    inwards = JWOInwardSerializer(many=True, required=False)
    charges = JWOChargeSerializer(many=True, required=False)
    reprocesses = JWOReprocessSerializer(many=True, required=False)

    class Meta:
        model = JobWorkOrder
        fields = [
            "id", "jwoNo", "processPlanId", "process", "vendor", "orderDate",
            "plannedQty", "rate", "totalAmount", "expectedCompletion",
            "status", "remarks", "materials", "outwards", "inwards", "charges",
            "reprocesses",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "jwoNo", "created_at", "updated_at"]


class VendorProcessInstructionSerializer(BaseModelSerializer):
    piNumber = serializers.CharField(source="pi_number", required=False)
    poId = serializers.CharField(source="po_id", required=False, allow_null=True, allow_blank=True)
    poNumber = serializers.CharField(source="po_number", required=False, allow_null=True, allow_blank=True)
    vendorId = serializers.CharField(source="vendor_id", required=False, allow_null=True, allow_blank=True)
    fabricSku = serializers.CharField(source="fabric_sku", required=False, allow_null=True, allow_blank=True)
    processType = serializers.CharField(source="process_type", required=False, allow_blank=True)
    assignedEmployee = serializers.CharField(source="assigned_employee", required=False, allow_null=True, allow_blank=True)
    assignedQty = QuantityField(source="assigned_qty", required=False)
    producedQty = QuantityField(source="produced_qty", required=False)
    startDate = serializers.DateField(source="start_date", required=False, allow_null=True)
    expectedCompletionDate = serializers.DateField(source="expected_completion_date", required=False, allow_null=True)

    class Meta:
        model = VendorProcessInstruction
        fields = [
            "id", "piNumber", "poId", "poNumber", "vendorId", "vendor",
            "fabric", "fabricSku", "processType", "assignedEmployee",
            "assignedQty", "producedQty", "date", "startDate",
            "expectedCompletionDate", "status", "remarks",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "piNumber", "created_at", "updated_at"]


class VPIProgressEntrySerializer(BaseModelSerializer):
    instructionId = serializers.UUIDField(source="instruction_id", required=False)
    producedQty = QuantityField(source="produced_qty", required=False)
    enteredBy = serializers.CharField(source="entered_by", required=False, allow_null=True, allow_blank=True)
    photoFileId = TenantPrimaryKeyRelatedField(
        source="photo_file", model="core.File", required=False, allow_null=True
    )
    photoUrl = serializers.SerializerMethodField()

    class Meta:
        model = VPIProgressEntry
        fields = [
            "id", "instructionId", "date", "producedQty", "enteredBy", "remarks",
            "photoFileId", "photoUrl", "created_at",
        ]
        read_only_fields = ["id", "photoUrl", "created_at"]

    def get_photoUrl(self, row):
        if not row.photo_file_id:
            return None
        from apps.core.files import public_url

        return public_url(row.photo_file, self.context.get("request"))
