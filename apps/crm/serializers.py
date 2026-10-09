"""CRM serializers (api.md §9)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    Contract,
    CrmProject,
    Deal,
    DealActivity,
    DealDocument,
    DealLine,
    DealStage,
    Form,
    FormSubmission,
    Industry,
    Lead,
    LeadEmail,
    LeadFile,
    LeadNote,
    LeadProduct,
    LeadSource,
    LeadThread,
    LeadThreadMessage,
    LeadUser,
    LostReason,
    MasterTask,
    Source,
    Stage,
    StageTask,
    Task,
    TaskAllocation,
    TaskAllocationAudit,
    UserAllocation,
    UserLocation,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class StageSerializer(BaseModelSerializer):
    """Lead stages are data, not an enum -- ``icon``/``bg``/``fg`` are rendered
    directly, so they are persisted (api.md §9.3)."""

    order = serializers.IntegerField(source="sequence", required=False)
    status = serializers.SerializerMethodField()
    count = serializers.SerializerMethodField()

    class Meta:
        model = Stage
        fields = [
            "id", "name", "order", "sequence", "color", "icon", "bg", "fg",
            "is_won", "is_lost", "is_active", "status", "count",
            "created_at", "updated_at",
        ]

    def get_status(self, stage):
        return "Active" if stage.is_active else "Inactive"

    def get_count(self, stage):
        return getattr(stage, "lead_count", None)


class DealStageSerializer(BaseModelSerializer):
    order = serializers.IntegerField(source="sequence", required=False)

    class Meta:
        model = DealStage
        fields = [
            "id", "name", "order", "sequence", "pipeline", "icon", "bg", "fg",
            "is_won", "is_lost", "is_active",
        ]


class SourceSerializer(BaseModelSerializer):
    class Meta:
        model = Source
        fields = ["id", "name", "is_active"]


class IndustrySerializer(BaseModelSerializer):
    class Meta:
        model = Industry
        fields = ["id", "name"]


class LostReasonSerializer(BaseModelSerializer):
    class Meta:
        model = LostReason
        fields = ["id", "name"]


# ---------------------------------------------------------------------------
# Leads (api.md §9.1)
# ---------------------------------------------------------------------------
class LeadSerializer(BaseModelSerializer):
    """The lead row, including the eight counters computed server-side."""

    leadNumber = serializers.CharField(source="lead_number", read_only=True)
    #: JSON ``status`` maps to the stage *name* -- stages are configurable.
    status = serializers.CharField(source="stage.name", read_only=True)
    stageId = TenantPrimaryKeyRelatedField(source="stage", model="crm.Stage")
    source = serializers.CharField(source="source.name", read_only=True)
    sourceId = TenantPrimaryKeyRelatedField(
        source="source", model="crm.Source", required=False, allow_null=True
    )
    owner = serializers.CharField(source="owner.name", read_only=True)
    ownerId = TenantPrimaryKeyRelatedField(
        source="owner", model="accounts.User", required=False, allow_null=True
    )
    createdOn = serializers.DateField(source="created_on", read_only=True)
    avatarColor = serializers.CharField(
        source="avatar_color", required=False, allow_null=True
    )

    productsCount = serializers.SerializerMethodField()
    sourcesCount = serializers.SerializerMethodField()
    filesCount = serializers.SerializerMethodField()
    openTasksCount = serializers.SerializerMethodField()
    deliveryChallansCount = serializers.SerializerMethodField()
    salesInvoicesCount = serializers.SerializerMethodField()
    #: Automatic Lead -> Customer conversion (quotation approval).
    convertedAt = serializers.DateTimeField(source="converted_at", read_only=True)
    convertedByName = serializers.CharField(source="converted_by.name", read_only=True)
    conversionSource = serializers.CharField(source="conversion_source", read_only=True)
    conversionReference = serializers.CharField(source="conversion_reference", read_only=True)
    customerId = serializers.CharField(source="party.id", read_only=True)
    customerName = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = Lead
        fields = [
            "id", "leadNumber", "name", "company", "phone", "email", "status",
            "stageId", "owner", "ownerId", "createdOn", "source", "sourceId",
            "city", "state", "country", "amount", "job_title", "industry",
            "avatarColor", "latitude", "longitude", "is_pinned", "party",
            "customerId", "customerName",
            "convertedAt", "convertedByName", "conversionSource",
            "conversionReference",
            "lost_reason", "custom_values",
            "productsCount", "sourcesCount", "filesCount", "openTasksCount",
            "deliveryChallansCount",
            "salesInvoicesCount",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def _counter(self, lead, attribute):
        return getattr(lead, attribute, 0)

    def get_productsCount(self, lead):
        return self._counter(lead, "products_count")

    def get_sourcesCount(self, lead):
        return self._counter(lead, "sources_count")

    def get_filesCount(self, lead):
        return self._counter(lead, "files_count")

    def get_openTasksCount(self, lead):
        return self._counter(lead, "open_tasks_count")

    def get_deliveryChallansCount(self, lead):
        return self._counter(lead, "delivery_challans_count")

    def get_salesInvoicesCount(self, lead):
        return self._counter(lead, "sales_invoices_count")


class LeadUserSerializer(BaseModelSerializer):
    userId = TenantPrimaryKeyRelatedField(source="user", model="accounts.User")
    name = serializers.CharField(source="user.name", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = LeadUser
        fields = ["id", "userId", "name", "email", "role", "added_at"]


class LeadProductSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    sku = serializers.CharField(source="item.sku", read_only=True)

    class Meta:
        model = LeadProduct
        fields = [
            "id", "itemId", "sku", "product_name", "fabric_code", "fabric_type",
            "fabric_design", "fabric_color", "fabric_width", "fabric_gsm",
            "qty", "uom", "expected_rate", "status", "notes", "created_at",
        ]


class LeadSourceSerializer(BaseModelSerializer):
    sourceId = TenantPrimaryKeyRelatedField(
        source="source", model="crm.Source", required=False, allow_null=True
    )
    name = serializers.CharField(source="source.name", read_only=True)
    createdByName = serializers.CharField(source="created_by.name", read_only=True)

    class Meta:
        model = LeadSource
        fields = ["id", "sourceId", "name", "campaign", "medium", "attributed_at", "created_at", "createdByName"]


class LeadNoteSerializer(BaseModelSerializer):
    authorName = serializers.CharField(source="author.name", read_only=True)

    class Meta:
        model = LeadNote
        fields = ["id", "body", "author", "authorName", "created_at"]
        read_only_fields = ["author"]


class LeadThreadMessageSerializer(BaseModelSerializer):
    authorName = serializers.CharField(source="author.name", read_only=True)

    class Meta:
        model = LeadThreadMessage
        fields = ["id", "body", "author", "authorName", "sent_at"]
        read_only_fields = ["author"]


class LeadThreadSerializer(BaseModelSerializer):
    messages = LeadThreadMessageSerializer(many=True, read_only=True)

    class Meta:
        model = LeadThread
        fields = ["id", "subject", "messages", "created_at"]


class LeadEmailSerializer(BaseModelSerializer):
    createdByName = serializers.CharField(source="created_by.name", read_only=True)

    class Meta:
        model = LeadEmail
        fields = [
            "id", "subject", "body", "to_addresses", "sent_at",
            "provider_message_id", "created_at", "createdByName",
        ]


class LeadFileSerializer(BaseModelSerializer):
    fileId = TenantPrimaryKeyRelatedField(source="file", model="core.File")
    fileName = serializers.CharField(source="file.file_name", read_only=True)
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = LeadFile
        fields = ["id", "fileId", "fileName", "fileSize", "label", "url", "created_at"]

    def get_url(self, row):
        from apps.core.files import public_url

        return public_url(row.file, self.context.get("request"))


# ---------------------------------------------------------------------------
# Tasks (api.md §9.3)
# ---------------------------------------------------------------------------
class MasterTaskSerializer(BaseModelSerializer):
    stages = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Stage.objects.all(), required=False
    )
    stageNames = serializers.SerializerMethodField()
    order = serializers.IntegerField(source="sort_order", required=False)
    dueIn = serializers.IntegerField(source="duration_days", required=False)
    status = serializers.SerializerMethodField()

    class Meta:
        model = MasterTask
        fields = [
            "id", "order", "sort_order", "title", "description", "role",
            "department", "dueIn", "duration_days", "priority", "stages",
            "stageNames", "is_active", "status", "created_at",
        ]

    def get_stageNames(self, task):
        return [stage.name for stage in task.stages.all()]

    def get_status(self, task):
        return "Active" if task.is_active else "Inactive"


class StageTaskSerializer(BaseModelSerializer):
    stageId = TenantPrimaryKeyRelatedField(source="stage", model="crm.Stage")
    stageName = serializers.CharField(source="stage.name", read_only=True)
    role = serializers.CharField(
        source="assignee_role", required=False, allow_null=True, allow_blank=True
    )
    order = serializers.IntegerField(source="sort_order", required=False)
    dueIn = serializers.IntegerField(source="offset_days", required=False)
    autoCreate = serializers.BooleanField(source="auto_create", required=False)

    class Meta:
        model = StageTask
        fields = [
            "id", "stageId", "stageName", "name", "title", "description", "role",
            "assignee_role", "department", "order", "sort_order", "dueIn",
            "offset_days", "priority", "required", "autoCreate", "repeats",
            "master_task", "created_at",
        ]
        extra_kwargs = {"title": {"required": False}}

    name = serializers.CharField(source="title", required=False)

    def validate(self, attrs):
        if not attrs.get("title") and self.instance is None:
            raise serializers.ValidationError({"name": ["Required."]})
        return attrs


class TaskSerializer(BaseModelSerializer):
    leadId = TenantPrimaryKeyRelatedField(
        source="lead", model="crm.Lead", required=False, allow_null=True
    )
    dealId = TenantPrimaryKeyRelatedField(
        source="deal", model="crm.Deal", required=False, allow_null=True
    )
    leadName = serializers.CharField(source="lead.name", read_only=True)
    assigneeId = TenantPrimaryKeyRelatedField(
        source="assignee", model="accounts.User", required=False, allow_null=True
    )
    assigneeName = serializers.CharField(source="assignee.name", read_only=True)
    dueDate = serializers.DateField(source="due_date", required=False, allow_null=True)

    class Meta:
        model = Task
        fields = [
            "id", "task_number", "leadId", "leadName", "dealId", "title",
            "description", "assigneeId", "assigneeName", "assignee_role",
            "department", "dueDate", "priority", "status", "source", "outcome",
            "next_action", "completion_note", "completed_at", "follow_up_task",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "task_number", "source", "outcome", "next_action", "completion_note",
            "completed_at", "follow_up_task", "created_at", "updated_at",
        ]


class CompleteTaskSerializer(BaseSerializer):
    outcome = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    nextAction = serializers.ChoiceField(
        choices=[
            "follow-up", "schedule-demo", "send-quotation",
            "move-next-stage", "finish",
        ],
        required=False,
        allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    completedBy = serializers.CharField(required=False, allow_null=True)


class TaskAllocationAuditSerializer(BaseModelSerializer):
    at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = TaskAllocationAudit
        fields = ["id", "action", "text", "at"]


class TaskAllocationSerializer(BaseModelSerializer):
    """A separate entity from lead tasks -- internal work assignment (api.md §9.3)."""

    assignee = serializers.CharField(source="assignee.name", read_only=True)
    assigneeId = TenantPrimaryKeyRelatedField(
        source="assignee", model="accounts.User", required=False, allow_null=True
    )
    assignedBy = serializers.CharField(source="assigned_by.name", read_only=True)
    fileName = serializers.CharField(
        source="file_name", required=False, allow_null=True, allow_blank=True
    )
    audit = TaskAllocationAuditSerializer(
        source="audit_entries", many=True, read_only=True
    )

    class Meta:
        model = TaskAllocation
        fields = [
            "id", "title", "description", "department", "assignee", "assigneeId",
            "assignedBy", "priority", "deadline", "status", "fileName", "audit",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Deals, contracts, projects (api.md §9.4 - §9.6)
# ---------------------------------------------------------------------------
class DealSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    customerName = serializers.CharField(source="party.name", read_only=True)
    ownerId = TenantPrimaryKeyRelatedField(
        source="owner", model="accounts.User", required=False, allow_null=True
    )
    ownerName = serializers.CharField(source="owner.name", read_only=True)
    leadId = TenantPrimaryKeyRelatedField(
        source="lead", model="crm.Lead", required=False, allow_null=True
    )

    expectedCloseDate = serializers.DateField(
        source="expected_close_date", required=False, allow_null=True
    )

    class Meta:
        model = Deal
        fields = [
            "id", "deal_number", "title", "leadId", "customerId", "customerName",
            "ownerId", "ownerName", "stage", "value", "probability",
            "expected_close_date", "expectedCloseDate", "closed_at", "lost_reason", "quotation",
            "crm_project", "created_at", "updated_at",
        ]
        read_only_fields = ["deal_number", "closed_at", "created_at", "updated_at"]


class DealActivitySerializer(BaseModelSerializer):
    actorName = serializers.CharField(source="actor.name", read_only=True)

    class Meta:
        model = DealActivity
        fields = ["id", "type", "description", "actor", "actorName", "created_at"]
        read_only_fields = ["actor"]


class DealLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    sku = serializers.CharField(source="item.sku", read_only=True)

    class Meta:
        model = DealLine
        fields = [
            "id", "itemId", "sku", "name", "description", "qty", "rate",
            "unit", "created_at",
        ]


class DealDocumentSerializer(BaseModelSerializer):
    fileId = TenantPrimaryKeyRelatedField(source="file", model="core.File")
    fileName = serializers.CharField(source="file.file_name", read_only=True)
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = DealDocument
        fields = ["id", "fileId", "fileName", "fileSize", "label", "url", "created_at"]

    def get_url(self, row):
        from apps.core.files import public_url

        return public_url(row.file, self.context.get("request"))


class ContractSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    #: ``Expiring Soon`` / ``Expired`` derived from ``end_date`` (api.md §9.5).
    displayStatus = serializers.SerializerMethodField()

    class Meta:
        model = Contract
        fields = [
            "id", "contract_number", "title", "customerId", "customerName",
            "deal", "template_key", "contract_type", "value", "start_date",
            "end_date", "status", "displayStatus", "body", "description", "signed_file",
            "signed_at", "expiring_soon_days", "created_at", "updated_at",
        ]
        read_only_fields = ["contract_number", "created_at", "updated_at"]

    def get_displayStatus(self, contract):
        from django.utils import timezone

        if contract.status in ("Draft", "Cancelled", "Closed"):
            return contract.status
        today = timezone.localdate()
        if contract.end_date < today:
            return "Expired"
        if (contract.end_date - today).days <= (contract.expiring_soon_days or 30):
            return "Expiring Soon"
        return contract.status

    def validate(self, attrs):
        """``assertContractDates`` moved server-side (api.md §9.5)."""
        start = attrs.get("start_date") or getattr(self.instance, "start_date", None)
        end = attrs.get("end_date") or getattr(self.instance, "end_date", None)
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"endDate": ["Must be after the start date."]}
            )
        return attrs


class CrmProjectSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    customerName = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = CrmProject
        fields = [
            "id", "name", "code", "customerId", "customerName", "deal", "owner",
            "start_date", "end_date", "status", "value", "progress",
            "description", "customer_text", "owner_text", "team", "project_type",
            "created_at", "updated_at",
        ]


class UserAllocationSerializer(BaseModelSerializer):
    userId = TenantPrimaryKeyRelatedField(source="user", model="accounts.User")
    userName = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = UserAllocation
        fields = [
            "id", "userId", "userName", "territory", "city", "state",
            "industry", "weight", "is_active",
        ]


class UserLocationSerializer(BaseModelSerializer):
    userId = serializers.CharField(source="user_id")
    recordedAt = serializers.DateTimeField(source="recorded_at")

    class Meta:
        model = UserLocation
        fields = ["id", "userId", "latitude", "longitude", "accuracy", "recordedAt"]


class FormSerializer(BaseModelSerializer):
    class Meta:
        model = Form
        fields = [
            "id", "name", "slug", "schema", "kind", "is_published",
            "published_at", "created_at", "updated_at",
        ]
        read_only_fields = ["published_at", "created_at", "updated_at"]


class FormSubmissionSerializer(BaseModelSerializer):
    class Meta:
        model = FormSubmission
        fields = ["id", "form", "payload", "lead", "submitted_at"]
