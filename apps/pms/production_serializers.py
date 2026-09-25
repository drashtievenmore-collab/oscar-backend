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
    employeeCode = serializers.ReadOnlyField(source="employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="employee.name")
    cumulative = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()

    class Meta:
        model = ProductionInstruction
        fields = [
            "id", "instruction_number", "agency_name", "order_reference", "order_meter",
            "employeeId", "employeeCode", "employeeName", "supervisorId",
            "status", "verified_by", "verified_at", "notes",
            "cumulative", "balance", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "instruction_number", "status", "verified_by", "verified_at",
            "employeeCode", "employeeName", "cumulative", "balance",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        supervisor = attrs.get("supervisor", getattr(self.instance, "supervisor", None))
        services.validate_employee_for_production(employee, field="employeeId")
        if supervisor is not None:
            services.validate_employee_for_production(supervisor, field="supervisorId")
        return attrs

    def get_cumulative(self, instance):
        return services.instruction_progress(instance)["cumulative"]

    def get_balance(self, instance):
        return services.instruction_progress(instance)["balance"]


class DailyProductionEntrySerializer(BaseModelSerializer):
    instructionId = TenantPrimaryKeyRelatedField(
        source="instruction", model="pms.ProductionInstruction"
    )
    enteredByEmployeeId = TenantPrimaryKeyRelatedField(
        source="entered_by_employee", model="hrms.Employee"
    )
    employeeCode = serializers.ReadOnlyField(source="entered_by_employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="entered_by_employee.name")

    class Meta:
        model = DailyProductionEntry
        fields = [
            "id", "instructionId", "entry_date", "meters",
            "enteredByEmployeeId", "employeeCode", "employeeName",
            "entered_by_user", "created_at", "remarks",
        ]
        read_only_fields = ["id", "entered_by_user", "created_at"]

    def validate(self, attrs):
        employee = attrs.get(
            "entered_by_employee", getattr(self.instance, "entered_by_employee", None)
        )
        services.validate_employee_for_production(
            employee, field="enteredByEmployeeId"
        )
        return attrs


class IncentiveSchemeSerializer(BaseModelSerializer):
    class Meta:
        model = IncentiveScheme
        fields = [
            "id", "name", "description", "rate_pct", "applies_from",
            "applies_to", "is_active", "created_at", "updated_at",
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
            "net_sales", "returns_total", "incentive_amount", "status",
            "detail", "journal_entry", "reversed_at", "reversal_reason",
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
