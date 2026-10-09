"""PMS serializers (api.md §10).

The frontend keeps stages, tasks, documents, approvals and delays nested inside
each project object. The tables are flat (db.md §10), so the *detail*
serializer re-assembles that nesting -- one round trip per child table with
``= any($ids)``, not N+1 per stage (db.md §15).
"""
from decimal import Decimal

from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    TenantPrimaryKeyRelatedField,
)

from . import services
from .models import (
    Approval,
    Delay,
    Department,
    Document,
    Project,
    ProjectStage,
    ProofShare,
    StageConfig,
    Task,
)


# ---------------------------------------------------------------------------
# Configuration (api.md §10.1)
# ---------------------------------------------------------------------------
class DepartmentSerializer(BaseModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "color", "capacity", "is_active", "created_at", "updated_at"]


class StageConfigSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="pms.Department", required=False, allow_null=True
    )
    defaultDuration = serializers.DecimalField(
        source="default_duration", max_digits=9, decimal_places=2,
        coerce_to_string=False, required=False,
    )
    durationUnit = serializers.CharField(source="duration_unit", required=False)
    assignedRole = serializers.CharField(
        source="assigned_role", required=False, allow_null=True, allow_blank=True
    )
    requiredApproval = serializers.BooleanField(source="required_approval", required=False)
    requiredDocument = serializers.BooleanField(source="required_document", required=False)
    isActive = serializers.BooleanField(source="is_active", required=False)

    class Meta:
        model = StageConfig
        fields = [
            "id", "name", "description", "sequence", "department", "departmentId",
            "defaultDuration", "durationUnit", "assignedRole", "requiredApproval",
            "requiredDocument", "isActive", "created_at", "updated_at",
        ]


class PmsSettingsSerializer(BaseSerializer):
    """The ``/pms/settings/`` payload (api.md §10.1)."""

    atRiskThresholdPct = serializers.IntegerField(required=False, min_value=1, max_value=100)
    requireClientApprovalOnDesign = serializers.BooleanField(required=False)
    requireQaCertificate = serializers.BooleanField(required=False)
    notifications = serializers.DictField(required=False)
    defaultDepartmentCapacity = serializers.IntegerField(required=False, min_value=1)
    departmentCapacity = serializers.DictField(required=False)
    statusColors = serializers.DictField(required=False)
    delayCategories = serializers.ListField(child=serializers.CharField(), required=False)


# ---------------------------------------------------------------------------
# Tasks, documents, approvals, delays
# ---------------------------------------------------------------------------
class TaskSerializer(BaseModelSerializer):
    taskName = serializers.CharField(source="task_name")
    assignedUser = serializers.SerializerMethodField()
    assignedUserId = TenantPrimaryKeyRelatedField(
        source="assigned_user", model="accounts.User", required=False, allow_null=True
    )
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="pms.Department", required=False, allow_null=True
    )
    projectId = serializers.CharField(source="project_id", read_only=True)
    stageId = serializers.CharField(source="stage_id", read_only=True)
    completionPct = serializers.IntegerField(
        source="completion_pct", required=False, min_value=0, max_value=100
    )
    startDate = serializers.DateField(source="start_date", required=False, allow_null=True)
    dueDate = serializers.DateField(source="due_date", required=False, allow_null=True)

    class Meta:
        model = Task
        fields = [
            "id", "projectId", "stageId", "taskName", "description",
            "assignedUser", "assignedUserId", "department", "departmentId",
            "startDate", "dueDate", "completionPct", "priority", "status",
            "created_at", "updated_at",
        ]

    def get_assignedUser(self, task):
        if not task.assigned_user_id:
            return None
        return {"id": str(task.assigned_user_id), "name": task.assigned_user.name}


class DocumentSerializer(BaseModelSerializer):
    uploadedBy = serializers.SerializerMethodField()
    previewUrl = serializers.SerializerMethodField()
    fileName = serializers.CharField(source="file_name", read_only=True)
    fileSize = serializers.IntegerField(source="file_size", read_only=True)
    fileId = TenantPrimaryKeyRelatedField(source="file", model="core.File")
    approvalStatus = serializers.CharField(source="approval_status", read_only=True)
    docKey = serializers.CharField(source="doc_key", required=False)
    revisionReason = serializers.CharField(source="revision_reason", read_only=True)
    uploadedAt = serializers.DateTimeField(source="uploaded_at", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id", "docKey", "version", "fileId", "fileName", "fileSize",
            "previewUrl", "uploadedBy", "uploaded_at", "uploadedAt", "comments",
            "revisionReason", "approvalStatus", "is_current", "is_proof",
        ]

    def get_uploadedBy(self, document):
        if not document.uploaded_by_id:
            return None
        return {"id": str(document.uploaded_by_id), "name": document.uploaded_by.name}

    def get_previewUrl(self, document):
        """A signed URL, so the drawing opens on any device -- which the
        IndexedDB store it replaces could never do (api.md §10.5)."""
        from apps.core.files import public_url

        return public_url(document.file, self.context.get("request"))


class ApprovalSerializer(BaseModelSerializer):
    documentId = serializers.CharField(source="document_id", read_only=True)
    approverType = serializers.CharField(source="approver_type")
    approverName = serializers.CharField(
        source="approver_name", required=False, allow_null=True
    )
    requestedBy = serializers.CharField(source="requested_by.name", read_only=True)
    requestedAt = serializers.DateTimeField(source="requested_at", read_only=True)
    decisionAt = serializers.DateTimeField(source="decision_at", read_only=True)

    class Meta:
        model = Approval
        fields = [
            "id", "documentId", "approverType", "approverName", "requestedBy",
            "requestedAt", "status", "decisionAt", "comments", "revision_reason",
        ]


class DelaySerializer(BaseModelSerializer):
    """The API shape is ``stage.delayDetails`` -- a singleton on the stage."""

    isDelayed = serializers.SerializerMethodField()
    responsibleUser = serializers.SerializerMethodField()
    responsibleUserId = TenantPrimaryKeyRelatedField(
        source="responsible_user", model="accounts.User", required=False, allow_null=True
    )
    expectedRecoveryDate = serializers.DateField(
        source="expected_recovery_date", required=False, allow_null=True
    )
    resolutionNotes = serializers.CharField(
        source="resolution_notes", required=False, allow_null=True, allow_blank=True
    )
    loggedAt = serializers.DateTimeField(source="created_at", read_only=True)
    resolvedAt = serializers.DateTimeField(source="resolved_at", read_only=True)

    class Meta:
        model = Delay
        fields = [
            "id", "isDelayed", "reason", "category", "responsibleUser",
            "responsibleUserId", "expectedRecoveryDate", "recovery_plan",
            "resolutionNotes", "loggedAt", "resolvedAt", "delay_days",
        ]

    def get_isDelayed(self, delay):
        return delay.resolved_at is None

    def get_responsibleUser(self, delay):
        if not delay.responsible_user_id:
            return None
        return {"id": str(delay.responsible_user_id), "name": delay.responsible_user.name}


class ProjectStageSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="pms.Department", required=False, allow_null=True
    )
    assignedTeam = serializers.CharField(
        source="assigned_team", required=False, allow_null=True, allow_blank=True
    )
    assignedUser = serializers.SerializerMethodField()
    assignedUserId = TenantPrimaryKeyRelatedField(
        source="assigned_user", model="accounts.User", required=False, allow_null=True
    )
    plannedDuration = serializers.DecimalField(
        source="planned_duration", max_digits=9, decimal_places=2,
        coerce_to_string=False, required=False, allow_null=True,
    )
    durationUnit = serializers.CharField(
        source="duration_unit", required=False, allow_null=True
    )
    startDateTime = serializers.DateTimeField(
        source="start_datetime", required=False, allow_null=True
    )
    expectedCompletionDateTime = serializers.DateTimeField(
        source="expected_completion_datetime", required=False, allow_null=True
    )
    actualStartDateTime = serializers.DateTimeField(
        source="actual_start_datetime", read_only=True
    )
    actualCompletionDateTime = serializers.DateTimeField(
        source="actual_completion_datetime", read_only=True
    )
    completionPct = serializers.IntegerField(source="completion_pct", read_only=True)
    percentage = serializers.DecimalField(
        source="weight_pct", max_digits=5, decimal_places=2,
        coerce_to_string=False, required=False,
        min_value=Decimal("0"), max_value=Decimal("100"),
    )
    # Aliases the UI emits for the same weight; all write ``weight_pct``.
    weightPct = serializers.DecimalField(
        max_digits=5, decimal_places=2, coerce_to_string=False,
        required=False, write_only=True,
        min_value=Decimal("0"), max_value=Decimal("100"),
    )
    weight = serializers.DecimalField(
        max_digits=5, decimal_places=2, coerce_to_string=False,
        required=False, write_only=True,
        min_value=Decimal("0"), max_value=Decimal("100"),
    )
    requiredApproval = serializers.BooleanField(source="required_approval", required=False)
    requiredDocument = serializers.BooleanField(source="required_document", required=False)

    # Nested children, populated by the detail serializer.
    tasks = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    approvals = serializers.SerializerMethodField()
    delayDetails = serializers.SerializerMethodField()
    isOverdue = serializers.SerializerMethodField()
    isAtRisk = serializers.SerializerMethodField()

    class Meta:
        model = ProjectStage
        fields = [
            "id", "name", "sequence", "department", "departmentId", "assignedTeam",
            "assignedUser", "assignedUserId", "plannedDuration", "durationUnit",
            "startDateTime", "expectedCompletionDateTime", "actualStartDateTime",
            "actualCompletionDateTime", "completionPct", "percentage", "weightPct",
            "weight", "requiredApproval",
            "requiredDocument", "status", "tasks", "documents", "approvals",
            "delayDetails", "isOverdue", "isAtRisk", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        # Fold the weight aliases into ``weight_pct`` (``percentage`` already
        # lands there via its source). Explicit ``percentage`` wins.
        weight = attrs.pop("weight", None)
        weight_pct_alias = attrs.pop("weightPct", None)
        if "weight_pct" not in attrs:
            if weight_pct_alias is not None:
                attrs["weight_pct"] = weight_pct_alias
            elif weight is not None:
                attrs["weight_pct"] = weight
        return attrs

    def get_assignedUser(self, stage):
        if not stage.assigned_user_id:
            return None
        return {"id": str(stage.assigned_user_id), "name": stage.assigned_user.name}

    def _bucket(self, stage, key):
        return (self.context.get(key) or {}).get(stage.id, [])

    def get_tasks(self, stage):
        return TaskSerializer(
            self._bucket(stage, "tasks_by_stage"), many=True, context=self.context
        ).data

    def get_documents(self, stage):
        return DocumentSerializer(
            self._bucket(stage, "documents_by_stage"), many=True, context=self.context
        ).data

    def get_approvals(self, stage):
        return ApprovalSerializer(
            self._bucket(stage, "approvals_by_stage"), many=True, context=self.context
        ).data

    def get_delayDetails(self, stage):
        rows = self._bucket(stage, "delays_by_stage")
        open_delay = next((row for row in rows if row.resolved_at is None), None)
        if open_delay is None:
            return {"isDelayed": False}
        return DelaySerializer(open_delay, context=self.context).data

    def get_isOverdue(self, stage):
        return services.is_stage_overdue(stage)

    def get_isAtRisk(self, stage):
        threshold = self.context.get("at_risk_threshold")
        if threshold is None:
            threshold = services.get_settings(stage.client_id).at_risk_threshold_pct
        return services.is_stage_at_risk(stage, threshold)


class ProjectListSerializer(BaseModelSerializer):
    """The list row -- no nested children, so the list stays one query."""

    crmOrderId = serializers.SerializerMethodField()
    crmCustomerId = serializers.CharField(source="party_id", read_only=True)
    customerName = serializers.CharField(source="customer_name", read_only=True)
    productDetails = serializers.SerializerMethodField()
    projectManager = serializers.SerializerMethodField()
    currentStageId = serializers.CharField(source="current_stage_id", read_only=True)
    currentDepartment = serializers.CharField(
        source="current_department.name", read_only=True
    )
    overallCompletionPct = serializers.IntegerField(
        source="overall_completion_pct", read_only=True
    )
    startDate = serializers.DateTimeField(source="start_date", required=False, allow_null=True)
    expectedCompletionDate = serializers.DateTimeField(
        source="expected_completion_date", required=False, allow_null=True
    )
    actualCompletionDate = serializers.DateTimeField(
        source="actual_completion_date", read_only=True
    )
    isOverdue = serializers.SerializerMethodField()
    createdBy = serializers.SerializerMethodField()
    createdById = serializers.CharField(source="created_by_id", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "code", "crmOrderId", "crmCustomerId", "customerName",
            "productDetails", "projectManager", "currentStageId",
            "currentDepartment", "priority", "overallCompletionPct",
            "startDate", "expectedCompletionDate", "actualCompletionDate",
            "status", "isOverdue", "createdBy", "createdById",
            "created_at", "updated_at",
        ]

    def get_createdBy(self, project):
        if not project.created_by_id:
            return None
        return {
            "id": str(project.created_by_id),
            "name": getattr(project.created_by, "name", "") or getattr(project.created_by, "username", "") or str(project.created_by),
            "email": getattr(project.created_by, "email", "") or "",
        }

    def get_crmOrderId(self, project):
        if not project.sales_order_id:
            return None
        return project.sales_order.order_number or str(project.sales_order_id)

    def get_productDetails(self, project):
        return {
            "productName": project.product_name,
            "orderValue": project.order_value,
            "quantity": project.quantity,
            "specifications": project.specifications,
        }

    def get_projectManager(self, project):
        if not project.project_manager_id:
            return None
        manager = project.project_manager
        return {
            "id": str(manager.id),
            "name": manager.name,
            "avatar": manager.avatar_url,
            "email": manager.email,
        }

    def get_isOverdue(self, project):
        from django.utils import timezone

        return bool(
            project.expected_completion_date
            and project.expected_completion_date < timezone.now()
            and project.actual_completion_date is None
            and project.status != "Completed"
        )


class ProjectDetailSerializer(ProjectListSerializer):
    """Full project including stages -- the shape ``pmsStore`` keeps locally."""

    stages = serializers.SerializerMethodField()
    activityLog = serializers.SerializerMethodField()

    class Meta(ProjectListSerializer.Meta):
        fields = ProjectListSerializer.Meta.fields + ["stages", "activityLog"]

    def get_stages(self, project):
        stages = self.context.get("stages") or []
        return ProjectStageSerializer(stages, many=True, context=self.context).data

    def get_activityLog(self, project):
        from apps.core.models import AuditLog

        rows = AuditLog.objects.filter(
            client_id=project.client_id,
            entity_type__in=["PmsProject", "PmsStage", "PmsTask", "PmsDocument", "PmsApproval"],
        ).filter(
            models_q(project)
        )[:100]
        return [
            {
                "id": str(row.id),
                "action": row.action,
                "entityType": row.entity_type,
                "entityId": str(row.entity_id) if row.entity_id else None,
                "description": row.description,
                "actor": row.actor_name,
                "timestamp": row.created_at,
                "from": row.from_value,
                "to": row.to_value,
                "comments": row.comments,
            }
            for row in rows
        ]


def models_q(project):
    """Activity for a project spans several entity types (api.md §10.9)."""
    from django.db.models import Q

    stage_ids = list(project.stages.values_list("id", flat=True))
    task_ids = list(project.tasks.values_list("id", flat=True))
    return Q(entity_id=project.id) | Q(entity_id__in=stage_ids + task_ids)


# ---------------------------------------------------------------------------
# Action payloads
# ---------------------------------------------------------------------------
class AssignStageSerializer(BaseSerializer):
    departmentId = serializers.CharField(required=False, allow_null=True)
    # pmsStore.assignStage sends the department *name*; resolve it server-side.
    department = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    assignedTeam = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    assignedUserId = serializers.CharField(required=False, allow_null=True)
    plannedDuration = serializers.DecimalField(
        max_digits=9, decimal_places=2, coerce_to_string=False, required=False
    )
    durationUnit = serializers.ChoiceField(
        choices=["Hours", "Days"], required=False, default="Days"
    )
    startDateTime = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        from django.apps import apps as django_apps

        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or self.context.get("client_id")
        Department = django_apps.get_model("pms", "Department")
        User = django_apps.get_model("accounts", "User")

        if attrs.get("department") and not attrs.get("departmentId"):
            match = Department.objects.filter(
                client_id=client_id, name=attrs["department"], deleted_at__isnull=True
            ).first()
            if match is None:
                raise serializers.ValidationError(
                    {"department": "Unknown department for this tenant."}
                )
            attrs["departmentId"] = str(match.id)

        if attrs.get("departmentId"):
            try:
                exists = Department.objects.filter(
                    pk=attrs["departmentId"], client_id=client_id,
                    deleted_at__isnull=True,
                ).exists()
            except Exception:
                exists = False
            if not exists:
                raise serializers.ValidationError(
                    {"departmentId": "Unknown department for this tenant."}
                )

        if attrs.get("assignedUserId"):
            try:
                exists = User.objects.filter(
                    pk=attrs["assignedUserId"], client_id=client_id,
                    deleted_at__isnull=True,
                ).exists()
            except Exception:
                exists = False
            if not exists:
                raise serializers.ValidationError(
                    {"assignedUserId": "Unknown user for this tenant."}
                )
        return attrs


class ProgressSerializer(BaseSerializer):
    pct = serializers.IntegerField(required=False, min_value=0, max_value=100)
    # The PMS store sends ``{ completionPct }`` (pmsSync.setStageProgress);
    # accept every alias the UI emits so progress is never dropped as unknown.
    completionPct = serializers.IntegerField(required=False, min_value=0, max_value=100)
    completion_pct = serializers.IntegerField(required=False, min_value=0, max_value=100)
    percentage = serializers.IntegerField(required=False, min_value=0, max_value=100)

    def validate(self, attrs):
        pct = (
            attrs.get("pct", None)
            if attrs.get("pct") is not None
            else attrs.get("completionPct", None)
            if attrs.get("completionPct") is not None
            else attrs.get("completion_pct", None)
            if attrs.get("completion_pct") is not None
            else attrs.get("percentage", None)
        )
        if pct is None:
            raise serializers.ValidationError({"pct": "A completion percentage is required."})
        attrs["pct"] = pct
        return attrs


class StageStatusSerializer(BaseSerializer):
    status = serializers.ChoiceField(
        choices=[
            "Not Started", "Assigned", "In Progress", "At Risk", "Delayed",
            "Submitted", "Under Review", "Approved", "Need Improvement",
            "Completed", "Blocked",
        ]
    )


class HandoffSerializer(BaseSerializer):
    force = serializers.BooleanField(required=False, default=False)
    comments = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class RequestApprovalSerializer(BaseSerializer):
    approverType = serializers.ChoiceField(choices=["PM", "Client"])
    approverName = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    approverUserId = serializers.CharField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get("approverUserId"):
            from django.apps import apps as django_apps

            request = self.context.get("request")
            client_id = getattr(request, "client_id", None) or self.context.get("client_id")
            User = django_apps.get_model("accounts", "User")
            try:
                exists = User.objects.filter(
                    pk=attrs["approverUserId"], client_id=client_id,
                    deleted_at__isnull=True,
                ).exists()
            except Exception:
                exists = False
            if not exists:
                raise serializers.ValidationError(
                    {"approverUserId": "Unknown user for this tenant."}
                )
        return attrs


class DecideSerializer(BaseSerializer):
    decision = serializers.ChoiceField(choices=["Approved", "Need Improvement"])
    comments = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    revisionReason = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    decidedBy = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class LogDelaySerializer(BaseSerializer):
    """Both a category and a recovery date are required (api.md §10.7)."""

    reason = serializers.CharField()
    category = serializers.CharField()
    responsibleUserId = serializers.CharField(required=False, allow_null=True)
    expectedRecoveryDate = serializers.DateField()
    recoveryPlan = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ShareProofSerializer(BaseSerializer):
    recipientName = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    recipientEmail = serializers.EmailField(required=False, allow_null=True)
    expiryDays = serializers.IntegerField(required=False, default=14, min_value=1, max_value=365)


class ProofShareSerializer(BaseModelSerializer):
    documentId = serializers.CharField(source="document_id", read_only=True)
    projectId = serializers.CharField(source="project_id", read_only=True)
    recipientName = serializers.CharField(source="recipient_name", read_only=True)
    recipientEmail = serializers.CharField(source="recipient_email", read_only=True)
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    decidedBy = serializers.CharField(source="decided_by", read_only=True)
    decisionComments = serializers.CharField(source="decision_comments", read_only=True)
    revisionReason = serializers.CharField(source="revision_reason", read_only=True)
    openedAt = serializers.DateTimeField(source="opened_at", read_only=True)
    expiresAt = serializers.DateTimeField(source="expires_at", read_only=True)

    class Meta:
        model = ProofShare
        fields = [
            "id", "documentId", "projectId", "recipientName", "recipientEmail",
            "status", "decision", "decidedAt", "decidedBy", "decisionComments",
            "revisionReason", "openedAt", "expiresAt", "revoked_at",
            "revoked_reason", "created_at",
        ]


class ApplyTemplateSerializer(BaseSerializer):
    configIds = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=False
    )
    # pmsStore sends ``stageConfigIds``; accept it as the same list.
    stageConfigIds = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=False
    )
    stageWeights = serializers.DictField(required=False, default=dict)

    def validate(self, attrs):
        ids = attrs.get("configIds") or attrs.get("stageConfigIds")
        if not ids:
            raise serializers.ValidationError(
                {"configIds": "At least one stage template is required."}
            )
        attrs["configIds"] = ids
        return attrs


class CompleteProjectSerializer(BaseSerializer):
    force = serializers.BooleanField(required=False, default=False)


class FromOrderSerializer(BaseSerializer):
    salesOrderId = serializers.CharField(required=False, allow_null=True)
    orderId = serializers.CharField(required=False, allow_null=True)
    orderNumber = serializers.CharField(required=False, allow_null=True)
    projectManagerId = serializers.CharField(required=False, allow_null=True)
    priority = serializers.ChoiceField(
        choices=["Low", "Medium", "High", "Urgent"], required=False, default="Medium"
    )
    startDate = serializers.DateTimeField(required=False, allow_null=True)
    specifications = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    stageConfigIds = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    stageWeights = serializers.DictField(required=False, default=dict)
    stages = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )

    def validate(self, attrs):
        if not attrs.get("salesOrderId"):
            order_id = attrs.get("orderId") or attrs.get("orderNumber")
            if not order_id:
                raise serializers.ValidationError({"salesOrderId": "A sales order id is required."})
            attrs["salesOrderId"] = order_id

        if attrs.get("projectManagerId"):
            from django.apps import apps as django_apps

            request = self.context.get("request")
            client_id = getattr(request, "client_id", None) or self.context.get("client_id")
            User = django_apps.get_model("accounts", "User")
            try:
                exists = User.objects.filter(
                    pk=attrs["projectManagerId"], client_id=client_id,
                    deleted_at__isnull=True,
                ).exists()
            except Exception:
                exists = False
            if not exists:
                raise serializers.ValidationError(
                    {"projectManagerId": "Unknown user for this tenant."}
                )
        return attrs


class StagePercentagesSerializer(BaseSerializer):
    stages = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate_stages(self, value):
        if not value:
            raise serializers.ValidationError("At least one stage is required.")
        total = 0.0
        for item in value:
            pct = item.get("percentage")
            if pct is None:
                pct = item.get("weightPct") or item.get("weight") or 0
            try:
                pct_num = float(pct)
            except (ValueError, TypeError):
                raise serializers.ValidationError("Stage percentage must be a valid number.")
            if pct_num < 0 or pct_num > 100:
                raise serializers.ValidationError("Stage percentage must be between 0 and 100.")
            total += pct_num
        if round(total, 2) != 100.0:
            raise serializers.ValidationError(
                f"Total stage percentage must equal 100% (currently {round(total, 2)}%)."
            )
        return value
