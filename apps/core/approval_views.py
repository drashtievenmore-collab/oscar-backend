from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import AlertIncident, AlertSetting, ApprovalRequest, ApprovalRule
from .approval_serializers import ApprovalRequestSerializer, ApprovalRuleSerializer
from .approval_services import decide_approval_request


class ApprovalRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ApprovalRuleSerializer

    def get_queryset(self):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        return ApprovalRule.objects.filter(client_id=client_id).order_by("document_type", "min_level")

    def perform_create(self, serializer):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        serializer.save(client_id=client_id)


class ApprovalRequestViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ApprovalRequestSerializer

    def get_queryset(self):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        qs = ApprovalRequest.objects.filter(client_id=client_id).select_related("rule", "requested_by", "decided_by")

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        doc_type = self.request.query_params.get("document_type")
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        urgency = self.request.query_params.get("urgency")
        if urgency:
            qs = qs.filter(urgency=urgency)

        my_approvals = self.request.query_params.get("my_approvals")
        if my_approvals == "true" and not self.request.user.is_superuser:
            role = getattr(self.request.user, "role", None)
            if role:
                qs = qs.filter(rule__approver_role=role) | qs.filter(rule__approver_role_code=role.code)
            else:
                qs = qs.none()

        return qs.order_by("-requested_at")

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        comments = request.data.get("comments", "")
        req = decide_approval_request(pk, request.user, "Approved", comments=comments)
        return Response(ApprovalRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        comments = request.data.get("comments", "")
        req = decide_approval_request(pk, request.user, "Rejected", comments=comments)
        return Response(ApprovalRequestSerializer(req).data)

    @action(detail=False, methods=["get"])
    def badge(self, request):
        """Return count of pending requests for the current user."""
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        qs = ApprovalRequest.objects.filter(client_id=client_id, status="Pending")
        if not request.user.is_superuser:
            role = getattr(request.user, "role", None)
            if role:
                qs = qs.filter(rule__approver_role=role) | qs.filter(rule__approver_role_code=role.code)
            else:
                qs = qs.none()
        cnt = qs.count()
        return Response({"count": cnt, "pendingCount": cnt, "activeCount": cnt})


# ---------------------------------------------------------------------------
# Alert Views
# ---------------------------------------------------------------------------
from rest_framework import serializers

class AlertIncidentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertIncident
        fields = [
            "id",
            "alert_key",
            "severity",
            "title",
            "message",
            "entity_type",
            "entity_id",
            "entity_number",
            "status",
            "acknowledged_by",
            "acknowledged_at",
            "resolved_at",
            "metadata",
            "created_at",
        ]


class AlertSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertSetting
        fields = [
            "id",
            "alert_key",
            "name",
            "description",
            "threshold_days",
            "threshold_pct",
            "threshold_amount",
            "is_enabled",
            "channels",
        ]


class AlertIncidentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AlertIncidentSerializer

    def get_queryset(self):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        qs = AlertIncident.objects.filter(client_id=client_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        severity = self.request.query_params.get("severity")
        if severity:
            qs = qs.filter(severity=severity)
        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        from django.utils import timezone
        incident = self.get_object()
        incident.status = "Acknowledged"
        incident.acknowledged_by = request.user
        incident.acknowledged_at = timezone.now()
        incident.save()
        return Response(AlertIncidentSerializer(incident).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        from django.utils import timezone
        incident = self.get_object()
        incident.status = "Resolved"
        incident.resolved_at = timezone.now()
        incident.save()
        return Response(AlertIncidentSerializer(incident).data)

    @action(detail=False, methods=["get"])
    def badge(self, request):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        count = AlertIncident.objects.filter(client_id=client_id, status="Active").count()
        return Response({"count": count, "activeCount": count, "pendingCount": count})


class AlertSettingViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AlertSettingSerializer

    def get_queryset(self):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        return AlertSetting.objects.filter(client_id=client_id).order_by("alert_key")

    def perform_create(self, serializer):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        serializer.save(client_id=client_id)
