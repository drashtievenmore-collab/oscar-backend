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
    JWODocument,
    JWOInward,
    JWOMaterial,
    JWOOutward,
    JWOReprocess,
    JobWorkOrder,
    PlanCharge,
    ProcessPlan,
)


class LiveRowsListSerializer(serializers.ListSerializer):
    """Nested child rows, minus soft-deleted ones.

    Child rows are soft-deleted through the per-row endpoints, so a plain
    ``manager.all()`` would still show them. Filtering in Python keeps any
    ``prefetch_related`` cache in play.
    """

    def to_representation(self, data):
        if hasattr(data, "all"):
            data = [row for row in data.all() if getattr(row, "deleted_at", None) is None]
        return super().to_representation(data)


class _RowMeta:
    list_serializer_class = LiveRowsListSerializer


class PlanChargeSerializer(BaseModelSerializer):
    class Meta(_RowMeta):
        model = PlanCharge
        fields = ["id", "type", "description", "rate", "qty", "amount", "status"]
        read_only_fields = ["id"]


class ProcessPlanSerializer(BaseModelSerializer):
    planNo = serializers.CharField(source="plan_no", required=False)
    projectId = serializers.UUIDField(source="project_id", required=False, allow_null=True)
    projectCode = serializers.ReadOnlyField(source="project.code")
    projectDestination = serializers.CharField(source="project_destination", required=False)
    priority = serializers.CharField(required=False)
    targetShadeFkId = TenantPrimaryKeyRelatedField(
        source="target_shade_fk", model="masters.Shade", required=False, allow_null=True
    )
    selectedTakas = serializers.JSONField(source="selected_takas", required=False)
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
    #: Legacy alias of expectedCompletionDate (the target_date column was
    #: folded into expected_completion_date).
    targetDate = serializers.DateField(
        source="expected_completion_date", read_only=True
    )
    assignedEmployee = serializers.CharField(
        source="assigned_employee", required=False, allow_null=True, allow_blank=True
    )
    #: Accepted on create only; after that charges go through
    #: /process-plans/{id}/charges/.
    charges = PlanChargeSerializer(many=True, required=False)

    class Meta:
        model = ProcessPlan
        fields = [
            "id", "planNo", "projectId", "projectCode", "projectDestination", "priority",
            "targetShadeFkId", "selectedTakas",
            "date", "process", "processCategory", "vendor",
            "contactPerson", "phone", "email", "address",
            "fabricItem", "greyLotNo", "fabricQuality", "takaRollNo", "shade",
            "expectedQty", "expectedLoss", "expectedReturnQty",
            "weight", "finishedWeight", "noOfRolls",
            "startDate", "expectedCompletionDate", "targetDate",
            "assignedEmployee", "approver",
            "status", "remarks", "charges",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "planNo", "projectCode", "created_at", "updated_at"]


class JWOMaterialSerializer(BaseModelSerializer):
    fabricItem = serializers.CharField(source="fabric_item", required=False, allow_blank=True)
    fabricQuality = serializers.CharField(source="fabric_quality", required=False, allow_blank=True)
    lotNo = serializers.CharField(source="lot_no", required=False, allow_blank=True, allow_null=True)
    qty = QuantityField(required=False)
    rate = MoneyField(required=False)
    amount = MoneyField(required=False)

    class Meta(_RowMeta):
        model = JWOMaterial
        fields = [
            "id", "fabricItem", "fabricQuality", "shade", "lotNo",
            "qty", "rate", "amount",
        ]
        read_only_fields = ["id"]


class JWOOutwardTakaSerializer(BaseModelSerializer):
    lotTakaId = TenantPrimaryKeyRelatedField(source="lot_taka", model="inventory.LotTaka")
    takaNo = serializers.ReadOnlyField(source="lot_taka.taka_number")
    lotNo = serializers.ReadOnlyField(source="lot_taka.lot_number")

    class Meta(_RowMeta):
        from .models import JWOOutwardTaka
        model = JWOOutwardTaka
        fields = ["id", "lotTakaId", "takaNo", "lotNo", "meters", "weight_kg"]
        read_only_fields = ["id", "takaNo", "lotNo"]


class JWOInwardTakaSerializer(BaseModelSerializer):
    sourceTakaId = TenantPrimaryKeyRelatedField(source="source_taka", model="inventory.LotTaka", required=False, allow_null=True)
    receivedTakaId = TenantPrimaryKeyRelatedField(source="received_taka", model="inventory.LotTaka", required=False, allow_null=True)

    class Meta(_RowMeta):
        from .models import JWOInwardTaka
        model = JWOInwardTaka
        fields = ["id", "sourceTakaId", "receivedTakaId", "taka_number", "meters", "weight_kg", "width", "shade"]
        read_only_fields = ["id"]


class JWOLossEntrySerializer(BaseModelSerializer):
    lossReasonId = TenantPrimaryKeyRelatedField(source="loss_reason", model="masters.LossReasonMaster")
    lossReasonName = serializers.ReadOnlyField(source="loss_reason.name")
    approvedByName = serializers.SerializerMethodField()

    class Meta(_RowMeta):
        from .models import JWOLossEntry
        model = JWOLossEntry
        fields = [
            "id", "lossReasonId", "lossReasonName", "qty", "taka_count",
            "weight_kg", "status", "approvedByName", "approved_at", "remarks", "created_at",
        ]
        read_only_fields = ["id", "lossReasonName", "approvedByName", "approved_at", "created_at"]

    def get_approvedByName(self, obj):
        return getattr(obj.approved_by, "name", None) or getattr(obj.approved_by, "email", None) if obj.approved_by else None


class JWOOutwardSerializer(BaseModelSerializer):
    challanNo = serializers.CharField(
        source="challan_no", required=False, allow_blank=True, allow_null=True
    )
    lrNo = serializers.CharField(source="lr_no", required=False, allow_blank=True, allow_null=True)
    vehicleNo = serializers.CharField(
        source="vehicle_no", required=False, allow_blank=True, allow_null=True
    )
    expectedReturnDate = serializers.DateField(
        source="expected_return_date", required=False, allow_null=True
    )
    authorizedById = TenantPrimaryKeyRelatedField(
        source="authorized_by", model="hrms.Employee", required=False, allow_null=True
    )
    authorizedByName = serializers.ReadOnlyField(source="authorized_by.name")
    takas = JWOOutwardTakaSerializer(many=True, required=False)

    class Meta(_RowMeta):
        model = JWOOutward
        fields = [
            "id", "no", "date", "qty", "challanNo", "lrNo", "transporter", "warehouse",
            "vehicleNo", "driver", "expectedReturnDate", "authorizedById", "authorizedByName",
            "status", "remarks", "takas",
        ]
        #: The server allocates challan numbers (JWOUT series).
        read_only_fields = ["id", "no", "authorizedByName"]


class JWOInwardSerializer(BaseModelSerializer):
    challanNo = serializers.CharField(
        source="challan_no", required=False, allow_blank=True, allow_null=True
    )
    takas = JWOInwardTakaSerializer(many=True, required=False)

    class Meta(_RowMeta):
        model = JWOInward
        fields = ["id", "no", "date", "qty", "accepted", "rejected", "challanNo", "status", "takas"]
        #: The server allocates receipt numbers (JWIN series).
        read_only_fields = ["id", "no"]


class JWOChargeSerializer(BaseModelSerializer):
    class Meta(_RowMeta):
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

    class Meta(_RowMeta):
        model = JWOReprocess
        fields = [
            "id", "no", "inwardNo", "date", "qty", "reason",
            "expectedReturn", "status", "remarks",
        ]
        #: The server allocates reprocess numbers (JWRP series).
        read_only_fields = ["id", "no"]


class JWODocumentSerializer(BaseModelSerializer):
    fileName = serializers.CharField(source="file_name", required=False, allow_blank=True)
    fileId = TenantPrimaryKeyRelatedField(
        source="file", model="core.File", required=False, allow_null=True
    )
    url = serializers.SerializerMethodField()
    uploadedBy = serializers.SerializerMethodField()

    class Meta(_RowMeta):
        model = JWODocument
        fields = [
            "id", "type", "no", "date", "fileName", "fileId", "url",
            "uploadedBy", "remarks", "created_at",
        ]
        read_only_fields = ["id", "url", "uploadedBy", "created_at"]

    def get_url(self, row):
        if not row.file_id:
            return None
        from apps.core.files import public_url

        return public_url(row.file, self.context.get("request"))

    def get_uploadedBy(self, row):
        user = getattr(row, "created_by", None)
        if user is None:
            return None
        return getattr(user, "name", None) or getattr(user, "email", None)


class JobWorkOrderSerializer(BaseModelSerializer):
    jwoNo = serializers.CharField(source="jwo_no", required=False)
    projectId = serializers.UUIDField(
        source="project_id", required=False, allow_null=True
    )
    projectCode = serializers.ReadOnlyField(source="project.code")
    projectDestination = serializers.CharField(
        source="project_destination", required=False
    )
    processPlanId = serializers.UUIDField(
        source="process_plan_id", required=False, allow_null=True
    )
    vendorId = TenantPrimaryKeyRelatedField(
        source="vendor", model="masters.Party", required=False, allow_null=True
    )
    processMasterId = TenantPrimaryKeyRelatedField(
        source="process_master", model="masters.ProcessMaster", required=False, allow_null=True
    )
    assignedEmployeeId = TenantPrimaryKeyRelatedField(
        source="assigned_employee", model="hrms.Employee", required=False, allow_null=True
    )
    approverEmployeeId = TenantPrimaryKeyRelatedField(
        source="approver_employee", model="hrms.Employee", required=False, allow_null=True
    )
    rateMasterId = TenantPrimaryKeyRelatedField(
        source="rate_master", model="masters.JobWorkRateMaster", required=False, allow_null=True
    )
    expectedLossPct = serializers.DecimalField(
        source="expected_loss_pct", max_digits=7, decimal_places=4, required=False
    )
    vendor = serializers.CharField(source="vendor_name", required=False, allow_blank=True)
    orderDate = serializers.DateField(source="order_date", required=False)
    #: Header totals are derived from the material rows on the server.
    plannedQty = QuantityField(source="planned_qty", read_only=True)
    rate = MoneyField(required=False)
    totalAmount = MoneyField(source="total_amount", read_only=True)
    expectedCompletion = serializers.DateField(
        source="expected_completion", required=False, allow_null=True
    )
    #: Child rows are accepted on create only. After that each one is
    #: created / edited / deleted through /jobwork/orders/{id}/<kind>/.
    materials = JWOMaterialSerializer(many=True, required=False)
    outwards = JWOOutwardSerializer(many=True, required=False)
    inwards = JWOInwardSerializer(many=True, required=False)
    charges = JWOChargeSerializer(many=True, required=False)
    reprocesses = JWOReprocessSerializer(many=True, required=False)
    lossEntries = JWOLossEntrySerializer(source="loss_entries", many=True, read_only=True)
    documents = JWODocumentSerializer(many=True, read_only=True)
    signatures = serializers.JSONField(required=False, default=dict)

    class Meta:
        model = JobWorkOrder
        fields = [
            "id", "jwoNo", "projectId", "projectCode", "projectDestination",
            "processPlanId", "process", "processMasterId", "vendor", "vendorId",
            "assignedEmployeeId", "approverEmployeeId", "rateMasterId", "expectedLossPct",
            "orderDate", "plannedQty", "rate", "totalAmount", "expectedCompletion",
            "status", "remarks", "materials", "outwards", "inwards", "charges",
            "reprocesses", "lossEntries", "documents", "signatures",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "jwoNo", "projectCode", "created_at", "updated_at"]
