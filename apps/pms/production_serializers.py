"""Production serializers.

Declared field names follow the model (snake_case); the wire renders
camelCase automatically via ``CamelCaseMixin``. Relations use
``TenantPrimaryKeyRelatedField`` so cross-tenant ids fail as "does not exist".
"""
from rest_framework import serializers

from apps.core.serializers import BaseModelSerializer, BaseSerializer, TenantPrimaryKeyRelatedField

from . import production_services as services
from .models import (
    DailyProductionEntry,
    IncentiveCalculation,
    IncentiveScheme,
    ProductionInstruction,
)


class ProductionInstructionSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee"
    )
    supervisorId = TenantPrimaryKeyRelatedField(
        source="supervisor", model="hrms.Employee", required=False, allow_null=True
    )
    agencyId = TenantPrimaryKeyRelatedField(
        source="agency", model="masters.Agency", required=False, allow_null=True
    )
    agencyCode = serializers.ReadOnlyField(source="agency.code")
    greyLotId = TenantPrimaryKeyRelatedField(
        source="grey_lot", model="inventory.LotTaka", required=False, allow_null=True
    )
    greyLotNo = serializers.ReadOnlyField(source="grey_lot.lot_number")
    finalPoId = serializers.PrimaryKeyRelatedField(source="final_po", read_only=True)
    finalPoNumber = serializers.ReadOnlyField(source="final_po.po_number")
    employeeCode = serializers.ReadOnlyField(source="employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="employee.name")
    supervisorName = serializers.ReadOnlyField(source="supervisor.name")
    expectedStartDate = serializers.DateField(
        source="expected_start_date", required=False, allow_null=True
    )
    expectedCompletionDate = serializers.DateField(
        source="expected_completion_date", required=False, allow_null=True
    )
    cumulative = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    totalProduced = serializers.SerializerMethodField()
    acceptedQty = serializers.SerializerMethodField()
    shortageQty = serializers.SerializerMethodField()
    rejectedQty = serializers.SerializerMethodField()
    reprocessQty = serializers.SerializerMethodField()
    finalAmount = serializers.SerializerMethodField()

    class Meta:
        model = ProductionInstruction
        fields = [
            "id", "instruction_number", "agency_name", "agencyId", "agencyCode", "order_reference", "order_meter",
            "taka_count", "total_weight", "width", "gsm", "tentative_rate", "agreed_rate", "rate_uom",
            "special_terms", "expected_start_date", "expectedStartDate", "expected_completion_date", "expectedCompletionDate",
            "pi_date", "fabric", "process_type", "agreed_job_rate", "rejected_qty",
            "verification_remarks", "completed_at", "greyLotId", "greyLotNo", "finalPoId", "finalPoNumber",
            "employeeId", "employeeCode", "employeeName", "supervisorId", "supervisorName",
            "status", "verified_by", "verified_at", "notes",
            "cumulative", "balance", "totalProduced", "acceptedQty", "rejectedQty", "reprocessQty",
            "shortageQty", "finalAmount", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "instruction_number", "verified_by", "verified_at",
            "completed_at", "rejected_qty", "verification_remarks",
            "employeeCode", "employeeName", "cumulative", "balance",
            "totalProduced", "acceptedQty", "rejectedQty", "reprocessQty", "shortageQty", "finalAmount",
            "agencyCode", "greyLotNo", "finalPoNumber",
            # Status only moves through approve/start/complete/verify/generate-po.
            "status",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        from apps.core.money import D, ZERO

        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        supervisor = attrs.get("supervisor", getattr(self.instance, "supervisor", None))
        services.validate_employee_for_production(employee, field="employeeId")
        if supervisor is not None:
            services.validate_employee_for_production(supervisor, field="supervisorId")
        if "agreed_job_rate" in attrs and D(attrs["agreed_job_rate"]) < ZERO:
            from rest_framework import serializers as drf_serializers

            raise drf_serializers.ValidationError(
                {"agreed_job_rate": ["Must be zero or more."]}
            )
        if "agreed_rate" in attrs and D(attrs["agreed_rate"]) < ZERO:
            from rest_framework import serializers as drf_serializers

            raise drf_serializers.ValidationError(
                {"agreed_rate": ["Must be zero or more."]}
            )
        return attrs

    def _completion(self, instance):
        if not hasattr(self, "_completion_cache"):
            self._completion_cache = {}
        key = str(instance.pk)
        if key not in self._completion_cache:
            self._completion_cache[key] = services.instruction_completion(instance)
        return self._completion_cache[key]

    def get_cumulative(self, instance):
        return services.instruction_progress(instance)["cumulative"]

    def get_balance(self, instance):
        return services.instruction_progress(instance)["balance"]

    def get_totalProduced(self, instance):
        return self._completion(instance)["totalProduced"]

    def get_acceptedQty(self, instance):
        return self._completion(instance)["acceptedQty"]

    def get_rejectedQty(self, instance):
        return self._completion(instance)["rejectedQty"]

    def get_reprocessQty(self, instance):
        return self._completion(instance).get("reprocessQty", 0)

    def get_shortageQty(self, instance):
        return self._completion(instance)["shortageQty"]

    def get_finalAmount(self, instance):
        return self._completion(instance)["finalAmount"]


class DailyProductionEntrySerializer(BaseModelSerializer):
    instructionId = TenantPrimaryKeyRelatedField(
        source="instruction", model="pms.ProductionInstruction"
    )
    enteredByEmployeeId = TenantPrimaryKeyRelatedField(
        source="entered_by_employee", model="hrms.Employee", required=False, allow_null=True
    )
    photoFileId = TenantPrimaryKeyRelatedField(
        source="photo_file", model="core.File", required=False, allow_null=True
    )
    employeeCode = serializers.ReadOnlyField(source="entered_by_employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="entered_by_employee.name")

    class Meta:
        model = DailyProductionEntry
        fields = [
            "id", "instructionId", "entry_date", "opening_pending", "meters",
            "produced_takas", "produced_weight", "good_meter", "reject_meter", "reprocess_meter",
            "cumulative_meter", "balance_meter", "photoFileId",
            "enteredByEmployeeId", "employeeCode", "employeeName",
            "entered_by_user", "created_at", "remarks", "status",
        ]
        read_only_fields = [
            "id", "entered_by_user", "created_at", "opening_pending",
            "cumulative_meter", "balance_meter",
        ]

    def validate(self, attrs):
        employee = attrs.get(
            "entered_by_employee", getattr(self.instance, "entered_by_employee", None)
        )
        if not employee and hasattr(self, "context") and "request" in self.context:
            user = self.context["request"].user
            if user and getattr(user, "is_authenticated", False):
                from apps.hrms.models import Employee
                employee = getattr(user, "employee", None)
                if not employee:
                    employee = Employee.objects.filter(client=user.client, email__iexact=user.email, deleted_at__isnull=True).first()
                if not employee and "instruction" in attrs:
                    employee = getattr(attrs["instruction"], "employee", None) or getattr(attrs["instruction"], "supervisor", None)
                if not employee:
                    employee = Employee.objects.filter(client=user.client, status="Active", deleted_at__isnull=True).first()
                if employee:
                    attrs["entered_by_employee"] = employee

        if employee:
            services.validate_employee_for_production(
                employee, field="enteredByEmployeeId"
            )
        else:
            raise serializers.ValidationError({"enteredByEmployeeId": ["An active employee is required to record daily production."]})
        return attrs


class IncentiveSchemeSerializer(BaseModelSerializer):
    class Meta:
        model = IncentiveScheme
        fields = [
            "id", "name", "description", "period_type", "basis", "slab_type",
            "rate_pct", "slabs", "hold_unpaid_invoices", "exclude_gst",
            "applies_from", "applies_to", "is_active", "created_at", "updated_at",
        ]


class IncentiveCalculationSerializer(BaseModelSerializer):
    schemeId = TenantPrimaryKeyRelatedField(
        source="scheme", model="pms.IncentiveScheme"
    )
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee"
    )
    employeeCode = serializers.ReadOnlyField(source="employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="employee.name")

    class Meta:
        model = IncentiveCalculation
        fields = [
            "id", "calc_number", "schemeId", "employeeId", "employeeCode",
            "employeeName", "period_label", "period_start", "period_end",
            "basis", "net_sales", "returns_total", "incentive_amount",
            "hold_amount", "paid_amount", "status", "approved_by",
            "approved_at", "paid_at", "detail", "journal_entry",
            "reversed_at", "reversal_reason",
        ]
        read_only_fields = [
            "id", "calc_number", "net_sales", "returns_total", "incentive_amount",
            "status", "detail", "journal_entry", "reversed_at", "reversal_reason",
        ]


class CalculateIncentiveSerializer(BaseSerializer):
    employeeId = TenantPrimaryKeyRelatedField(model="hrms.Employee")
    periodLabel = serializers.CharField()
    periodStart = serializers.DateField()
    periodEnd = serializers.DateField()


class VerifyInstructionSerializer(BaseSerializer):
    rejectedQty = serializers.DecimalField(
        max_digits=18, decimal_places=4, required=False, default=0
    )
    physicallyVerifiedQty = serializers.DecimalField(
        max_digits=18, decimal_places=4, required=False, allow_null=True
    )
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    action = serializers.CharField(required=False, allow_blank=True, default="approve_complete")
    verificationDate = serializers.DateField(required=False, allow_null=True)
    verifiedByEmployeeId = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attachments = serializers.ListField(required=False, default=list)
