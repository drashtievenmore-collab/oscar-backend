import logging

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import record_audit
from apps.core.money import D
from apps.core.models import AlertIncident, AlertSetting, ApprovalRequest, ApprovalRule
from .approval_serializers import ApprovalRequestSerializer, ApprovalRuleSerializer
from .approval_services import decide_approval_request
from .permissions import HasModulePermission

logger = logging.getLogger(__name__)


class ApprovalRuleViewSet(viewsets.ModelViewSet):
    # Anyone may read the rules (the inbox shows them); only rule admins change them.
    permission_classes = [HasModulePermission]
    permission_map = {"write": ["manage_approval_rules"]}
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

    # Requests are raised by the system and decided through the actions below;
    # they are never edited or deleted directly.
    http_method_names = ["get", "post", "head", "options"]

    def perform_create(self, serializer):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        # Who may decide, and whether a request was raised by a business rule,
        # are set by the server only (core.approval_services.raise_approval).
        new_values = dict(serializer.validated_data.get("new_values") or {})
        for key in ("system", "approverRoleCodes", "approverRoleCode", "level"):
            new_values.pop(key, None)
        serializer.save(
            client_id=client_id, requested_by=self.request.user, status="Pending", new_values=new_values
        )

    def _decide(self, request, decision):
        req = self.get_object()  # tenant-scoped: another tenant's id is a 404
        comments = request.data.get("comments", "")
        req = decide_approval_request(
            req.id, request.user, decision, comments=comments, client_id=req.client_id
        )
        return Response(ApprovalRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, "Approved")

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, "Rejected")

    @action(detail=True, methods=["post"], url_path="return-for-correction")
    def return_correction(self, request, pk=None):
        return self._decide(request, "Returned")

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


class DiscountPolicyView(APIView):
    """``GET/PATCH /approvals/discount-policy/`` -- the tenant's discount authority.

    Anyone may read it (the quotation composer shows the limits and what the
    current user may give); changing it needs ``manage_approval_rules``.
    Accepts camelCase fields, and the older single-threshold shape
    ``{max_discount_pct, approver_role_code}`` from the settings screen.
    """

    permission_classes = [HasModulePermission]
    permission_map = {"PATCH": ["manage_approval_rules"], "PUT": ["manage_approval_rules"]}

    PCT_FIELDS = {
        "autoAllowPct": "auto_allow_pct",
        "salespersonMaxPct": "salesperson_max_pct",
        "managerMaxPct": "manager_max_pct",
        "minMarginPct": "min_margin_pct",
        "max_discount_pct": "salesperson_max_pct",
    }

    def get(self, request):
        from apps.sales import discount_control

        policy = discount_control.get_policy(request.client_id)
        return Response(discount_control.serialize_policy(policy, request.user))

    def patch(self, request):
        from apps.core.exceptions import ValidationFailed
        from apps.sales import discount_control

        policy = discount_control.get_policy(request.client_id)
        data = request.data if isinstance(request.data, dict) else {}
        errors = {}
        for key, field in self.PCT_FIELDS.items():
            if key not in data:
                continue
            try:
                value = D(str(data[key]))
            except Exception:
                errors[key] = ["Must be a number."]
                continue
            if value < 0 or value > 100:
                errors[key] = ["Must be between 0 and 100."]
                continue
            setattr(policy, field, value)

        for key, field in (
            ("managerRoleCode", "manager_role_code"),
            ("approver_role_code", "manager_role_code"),
            ("ownerRoleCode", "owner_role_code"),
        ):
            if data.get(key):
                setattr(policy, field, discount_control.normalise_role_code(data[key]))

        if "partyOverrides" in data:
            rows = data["partyOverrides"] or []
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not row.get("partyId") for row in rows
            ):
                errors["partyOverrides"] = ["A list of {partyId, maxPct}."]
            else:
                policy.party_overrides = [
                    {"partyId": str(row["partyId"]), "maxPct": str(row.get("maxPct") or "0")}
                    for row in rows
                ]

        if not errors:
            limits = (policy.auto_allow_pct, policy.salesperson_max_pct, policy.manager_max_pct)
            if not (D(limits[0]) <= D(limits[1]) <= D(limits[2])):
                errors["managerMaxPct"] = [
                    "Limits must rise: auto-allow ≤ salesperson ≤ manager."
                ]
        if errors:
            raise ValidationFailed("Some fields need attention.", field_errors=errors)

        before = discount_control.serialize_policy(discount_control.get_policy(request.client_id))
        policy.updated_by = request.user
        policy.save()
        after = discount_control.serialize_policy(policy, request.user)
        record_audit(
            client=policy.client_id, actor=request.user, action="update",
            entity_type="DiscountPolicy", entity_id=policy.id, entity_label="Discount policy",
            before=before, after=after,
        )
        return Response(after)

    put = patch


class AlertIncidentViewSet(viewsets.ModelViewSet):
    # Any tenant user may read and acknowledge alerts. Resolving or deleting
    # one closes it for everybody, so that needs resolve_alerts. Incidents are
    # raised by the alert engine and are never edited directly.
    permission_classes = [HasModulePermission]
    permission_map = {
        "create": ["manage_alerts"],
        "run": ["manage_alerts"],
        "resolve": ["resolve_alerts"],
        "destroy": ["resolve_alerts"],
    }
    http_method_names = ["get", "post", "delete", "head", "options"]
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

    @action(detail=False, methods=["post"])
    def run(self, request):
        """Run every alert check for the caller's tenant now (manage_alerts only)."""
        from datetime import date

        from apps.accounts.models import Client
        from .alert_services import ensure_alert_settings, run_all_alert_checks

        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        client = Client.objects.get(pk=client_id)

        as_of = None
        raw_as_of = request.data.get("asOf") if isinstance(request.data, dict) else None
        if raw_as_of:
            try:
                as_of = date.fromisoformat(str(raw_as_of))
            except ValueError:
                return Response(
                    {"detail": "asOf must be a date in YYYY-MM-DD format."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        ensure_alert_settings(client)
        errors = []
        incidents = run_all_alert_checks(client.id, as_of=as_of, errors=errors)
        return Response({
            "incidentCount": len(incidents),
            "incidents": AlertIncidentSerializer(incidents, many=True).data,
            "errors": errors,
        })

    @action(detail=False, methods=["get"])
    def badge(self, request):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        count = AlertIncident.objects.filter(client_id=client_id, status="Active").count()
        return Response({"count": count, "activeCount": count, "pendingCount": count})


class AlertSettingViewSet(viewsets.ModelViewSet):
    # Anyone may read the settings (the Alert Center shows them); only alert
    # admins change thresholds or switch a rule off.
    permission_classes = [HasModulePermission]
    permission_map = {"write": ["manage_alerts"]}
    serializer_class = AlertSettingSerializer

    def get_queryset(self):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        return AlertSetting.objects.filter(client_id=client_id).order_by("alert_key")

    def _reevaluate(self, instance, client_id):
        if instance.alert_key != "combined_grey_stock_low":
            return
        try:
            from apps.inventory.services import evaluate_combined_grey_stock_alert
            evaluate_combined_grey_stock_alert(client_id)
        except Exception:
            # The setting is saved either way; the next alert run re-evaluates.
            logger.exception("Re-evaluating combined grey stock alert failed for client %s", client_id)

    def perform_create(self, serializer):
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        instance = serializer.save(client_id=client_id)
        self._reevaluate(instance, client_id)

    def perform_update(self, serializer):
        instance = serializer.save()
        client_id = getattr(self.request, "client_id", None) or getattr(self.request.user, "client_id", None)
        self._reevaluate(instance, client_id)

