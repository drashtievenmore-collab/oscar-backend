from rest_framework import serializers
from apps.core.models import ApprovalRequest, ApprovalRule


class ApprovalRuleSerializer(serializers.ModelSerializer):
    approverRoleName = serializers.CharField(source="approver_role.name", read_only=True)

    class Meta:
        model = ApprovalRule
        fields = [
            "id",
            "document_type",
            "name",
            "description",
            "condition_type",
            "threshold_value",
            "approver_role",
            "approverRoleName",
            "approver_role_code",
            "min_level",
            "is_active",
            "created_at",
        ]


class ApprovalRequestSerializer(serializers.ModelSerializer):
    requestedByName = serializers.CharField(source="requested_by.name", read_only=True)
    decidedByName = serializers.CharField(source="decided_by.name", read_only=True)
    ruleName = serializers.CharField(source="rule.name", read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = [
            "id",
            "rule",
            "ruleName",
            "document_type",
            "document_id",
            "document_number",
            "requested_by",
            "requestedByName",
            "requested_at",
            "reason",
            "old_values",
            "new_values",
            "variance_amount",
            "variance_pct",
            "status",
            "urgency",
            "decided_by",
            "decidedByName",
            "decided_at",
            "comments",
        ]
        # Decisions go through approve / reject / return-for-correction only.
        read_only_fields = [
            "requested_by", "requested_at", "status", "decided_by", "decided_at", "comments",
        ]
