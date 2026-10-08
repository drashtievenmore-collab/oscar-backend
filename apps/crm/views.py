"""CRM endpoints (api.md §9)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import ZERO, round2
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import BulkDeleteMixin, ReadOnlyTenantViewSet, TenantModelViewSet

from . import services
from .models import (
    Contract,
    CrmProject,
    Deal,
    DealActivity,
    DealDocument,
    DealLine,
    DealStage,
    Form,
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
from .serializers import (
    CompleteTaskSerializer,
    ContractSerializer,
    CrmProjectSerializer,
    DealActivitySerializer,
    DealDocumentSerializer,
    DealLineSerializer,
    DealSerializer,
    DealStageSerializer,
    FormSerializer,
    IndustrySerializer,
    LeadEmailSerializer,
    LeadFileSerializer,
    LeadNoteSerializer,
    LeadProductSerializer,
    LeadSerializer,
    LeadSourceSerializer,
    LeadThreadMessageSerializer,
    LeadThreadSerializer,
    LeadUserSerializer,
    LostReasonSerializer,
    MasterTaskSerializer,
    SourceSerializer,
    StageSerializer,
    StageTaskSerializer,
    TaskAllocationSerializer,
    TaskSerializer,
    UserAllocationSerializer,
    UserLocationSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


# ---------------------------------------------------------------------------
# Leads (api.md §9.1, §9.2)
# ---------------------------------------------------------------------------
class LeadViewSet(BulkDeleteMixin, TenantModelViewSet):
    queryset = Lead.objects.select_related("stage", "source", "owner", "party")
    serializer_class = LeadSerializer
    audit_entity_type = "CrmLead"
    audit_label_field = "lead_number"
    search_fields = ["name", "company", "phone", "email", "lead_number", "city"]
    ordering_fields = ["name", "created_on", "amount", "created_at"]
    ordering = ["-is_pinned", "-created_on"]
    status_field = "stage__name"
    default_date_field = "created_on"
    allowed_date_fields = ("created_on", "created_at", "updated_at")
    filter_map = {
        "source": "source__name",
        "sourceId": "source_id",
        "owner": "owner__name",
        "ownerId": "owner_id",
        "city": "city",
        "stageId": "stage_id",
    }
    permission_map = {
        "list": ["view_lead"],
        "retrieve": ["view_lead"],
        "create": ["create_lead"],
        "update": ["edit_lead"],
        "partial_update": ["edit_lead"],
        "destroy": ["delete_lead"],
        "bulk_delete": ["delete_lead"],
    }

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        tab = self.request.query_params.get("tab")
        if tab == "pinned":
            queryset = queryset.filter(is_pinned=True)
        elif tab == "mine":
            queryset = queryset.filter(owner=self.request.user)
        elif tab == "unassigned":
            queryset = queryset.filter(owner__isnull=True)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        # Paginate first, then count only the page's leads (db.md §9.1, §15).
        services.annotate_lead_counters(request.client_id, page)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        lead = self.get_object()
        services.annotate_lead_counters(request.client_id, [lead])
        self._concurrency_instance = lead
        return Response(self.get_serializer(lead).data)

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            pipelineValue=money_sum("amount"),
            pinned=Count("id", filter=Q(is_pinned=True)),
            unassigned=Count("id", filter=Q(owner__isnull=True)),
        )

    def perform_create(self, serializer):
        serializer.validated_data["lead_number"] = allocate_number(
            self.request.user.client, "LEAD"
        )
        if not serializer.validated_data.get("stage"):
            first = Stage.objects.filter(
                client_id=self.get_client_id(), is_active=True, deleted_at__isnull=True
            ).order_by("sequence").first()
            if first is None:
                raise ValidationFailed(
                    "No lead stages are configured for this workspace.",
                    field_errors={"stageId": ["Configure CRM stages first."]},
                )
            serializer.validated_data["stage"] = first

        lead = super().perform_create(serializer)
        services.run_stage_automation(lead, lead.stage, user=self.request.user)
        return lead

    def update(self, request, *args, **kwargs):
        """api.md §9.3 -- returns ``{ lead, createdTasks: [] }`` so the UI can
        toast what the stage automation generated."""
        partial = kwargs.pop("partial", False)
        lead = self.get_object()
        previous_stage_id = lead.stage_id

        serializer = self.get_serializer(lead, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        lead.refresh_from_db()
        created_tasks = []
        if str(lead.stage_id) != str(previous_stage_id):
            from apps.core.permissions import require_permission

            require_permission(request.user, "move_lead")
            created_tasks = services.run_stage_automation(
                lead, lead.stage, user=request.user
            )

        services.annotate_lead_counters(request.client_id, [lead])
        return Response(
            {
                "lead": self.get_serializer(lead).data,
                "createdTasks": TaskSerializer(created_tasks, many=True).data,
            }
        )

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete_action(self, request):
        return self.bulk_delete(request)

    @action(detail=True, methods=["post"])
    def pin(self, request, pk=None):
        lead = self.get_object()
        lead.is_pinned = True
        lead.save(update_fields=["is_pinned", "updated_at"])
        return Response({"isPinned": True})

    @action(detail=True, methods=["post"])
    def unpin(self, request, pk=None):
        lead = self.get_object()
        lead.is_pinned = False
        lead.save(update_fields=["is_pinned", "updated_at"])
        return Response({"isPinned": False})

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """``GET /crm/leads/stats/`` -- tab counts and KPI tiles."""
        queryset = self.get_queryset()
        by_stage = (
            queryset.values("stage__id", "stage__name")
            .annotate(count=Count("id"), value=money_sum("amount"))
            .order_by("stage__sequence")
        )
        return Response(
            {
                "total": queryset.count(),
                "pinned": queryset.filter(is_pinned=True).count(),
                "mine": queryset.filter(owner=request.user).count(),
                "unassigned": queryset.filter(owner__isnull=True).count(),
                "pipelineValue": round2(
                    queryset.aggregate(value=money_sum("amount"))["value"]
                ),
                "byStage": [
                    {
                        "stageId": str(row["stage__id"]),
                        "stage": row["stage__name"],
                        "count": row["count"],
                        "value": round2(row["value"]),
                    }
                    for row in by_stage
                ],
            }
        )

    @action(detail=False, methods=["get"])
    def map(self, request):
        """``GET /crm/leads/map/`` -- geo points for the map view."""
        rows = self.filter_queryset(self.get_queryset()).filter(
            latitude__isnull=False, longitude__isnull=False
        ).values("id", "name", "company", "city", "latitude", "longitude", "stage__name")
        return Response(
            envelope(
                [
                    {
                        "id": str(row["id"]),
                        "name": row["name"],
                        "company": row["company"],
                        "city": row["city"],
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "status": row["stage__name"],
                    }
                    for row in rows
                ]
            )
        )

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def convert(self, request, pk=None):
        """Lead -> Deal (+ optional customer) (api.md §9.1).

        The customer half reuses the quotation-approval conversion service so
        manual and automatic conversion link/dedupe identically.
        """
        lead = self.get_object()
        if lead.converted_deal_id:
            raise Conflict(
                "This lead has already been converted.", code=Codes.ALREADY_DONE
            )

        party = lead.party
        created_customer = False
        if party is None and request.data.get("createCustomer", True):
            result = services.convert_lead_to_customer(
                lead,
                user=request.user,
                source="Manual Conversion",
                reference=None,
            )
            lead = result["lead"]
            party = result["party"]
            created_customer = result["created"]

        deal = Deal.objects.create(
            client_id=request.client_id,
            deal_number=allocate_number(request.user.client, "DEAL"),
            title=request.data.get("title") or f"{lead.company or lead.name} opportunity",
            lead=lead,
            party=party,
            owner=lead.owner or request.user,
            stage="Open",
            value=lead.amount or ZERO,
            created_by=request.user,
        )
        lead.converted_deal = deal
        lead.save(update_fields=["party", "converted_deal", "updated_at"])
        self.write_audit("convert", lead, description=f"Converted to deal {deal.deal_number}")

        return Response(
            {
                "lead": LeadSerializer(lead).data,
                "deal": DealSerializer(deal).data,
                "customer": {"id": str(party.id), "name": party.name} if party else None,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """A ``union all`` over the sub-resources plus the audit log (db.md §9.2).

        Deliberately a query: a physical timeline table would have to be written
        from nine places and would be wrong the first time one forgot.
        """
        from apps.core.models import AuditLog

        lead = self.get_object()
        rows = []

        for note in lead.notes.filter(deleted_at__isnull=True):
            rows.append({"type": "note", "id": str(note.id), "at": note.created_at,
                         "title": "Note added", "body": note.body,
                         "actor": note.author.name if note.author_id else None})
        for email in lead.emails.filter(deleted_at__isnull=True):
            rows.append({"type": "email", "id": str(email.id),
                         "at": email.sent_at or email.created_at,
                         "title": email.subject, "body": email.body, "actor": None})
        for task in lead.tasks.filter(deleted_at__isnull=True):
            rows.append({"type": "task", "id": str(task.id), "at": task.created_at,
                         "title": task.title, "body": task.description,
                         "actor": task.assignee.name if task.assignee_id else None})
        for entry in AuditLog.objects.filter(
            client_id=request.client_id, entity_type="CrmLead", entity_id=lead.id
        ).exclude(action="update", description="").exclude(action="update", description__isnull=True)[:100]:
            rows.append({"type": "activity", "id": str(entry.id), "at": entry.created_at,
                         "title": entry.description or entry.action,
                         "body": None, "actor": entry.actor_name,
                         "from": entry.from_value, "to": entry.to_value})

        rows.sort(key=lambda row: row["at"] or timezone.now(), reverse=True)
        return Response(envelope(rows))

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        """Related estimates / quotations / invoices (api.md §9.2)."""
        from apps.sales.models import Estimate, Quotation

        lead = self.get_object()
        rows = [
            {
                "documentType": "Estimate",
                "id": str(row.id),
                "number": row.estimate_number,
                "date": row.doc_date,
                "status": row.status,
                "total": round2(row.total),
            }
            for row in Estimate.objects.filter(crm_lead=lead, deleted_at__isnull=True)
        ] + [
            {
                "documentType": "Quotation",
                "id": str(row.id),
                "number": row.quotation_number,
                "date": row.doc_date,
                "status": row.status,
                "total": round2(row.total),
            }
            for row in Quotation.objects.filter(crm_lead=lead, deleted_at__isnull=True)
        ]
        rows.sort(key=lambda row: row["date"], reverse=True)
        return Response(envelope(rows))

    # -- sub-resources (api.md §9.2) ---------------------------------------
    def _sub_resource(self, request, related_name, serializer_class, **defaults):
        lead = self.get_object()
        manager = getattr(lead, related_name)

        if request.method == "GET":
            rows = manager.filter(deleted_at__isnull=True)
            return Response(
                envelope(
                    serializer_class(rows, many=True, context=self.get_serializer_context()).data
                )
            )

        serializer = serializer_class(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        row = serializer.save(client_id=request.client_id, lead=lead, **defaults)
        return Response(
            serializer_class(row, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "post"], url_path="users")
    def lead_users(self, request, pk=None):
        return self._sub_resource(request, "lead_users", LeadUserSerializer)

    @action(detail=True, methods=["delete"], url_path=r"users/(?P<user_id>[^/.]+)")
    def lead_user_detail(self, request, pk=None, user_id=None):
        """Unassign one user from a lead (the Users & Requirements tab)."""
        lead = self.get_object()
        row = lead.lead_users.filter(pk=user_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That assignment no longer exists.")
        row.soft_delete(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"], url_path="products")
    def products(self, request, pk=None):
        return self._sub_resource(request, "products", LeadProductSerializer)

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"products/(?P<product_id>[^/.]+)",
    )
    def product_detail(self, request, pk=None, product_id=None):
        """Edit / remove one fabric requirement line (the Users &
        Requirements tab)."""
        lead = self.get_object()
        row = lead.products.filter(pk=product_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That requirement no longer exists.")
        if request.method == "DELETE":
            row.soft_delete(request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = LeadProductSerializer(
            row,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            LeadProductSerializer(row, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["get", "post"], url_path="sources")
    def sources(self, request, pk=None):
        if request.method == "POST":
            raw = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
            data = dict(raw)
            # The drawer posts a channel label (`source`/`name`/`campaign`);
            # resolve it to a real `crm.Source` so GET returns `name`.
            source_name = (
                data.get("sourceName") or data.get("source_name")
                or data.get("source") or data.get("name")
                or data.get("campaign")
            )
            if not data.get("sourceId") and not data.get("source_id") and source_name:
                label = str(source_name).strip()
                if label:
                    # Exact match first so iexact get_or_create never duplicates case variants.
                    source_obj = Source.objects.filter(
                        client_id=request.client_id, name__iexact=label, deleted_at__isnull=True
                    ).first()
                    if source_obj is None:
                        source_obj = Source.objects.create(
                            client_id=request.client_id, name=label,
                            created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                        )
                    data["sourceId"] = str(source_obj.id)
            lead = self.get_object()
            serializer = LeadSourceSerializer(
                data=data, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)
            row = serializer.save(
                client_id=request.client_id, lead=lead,
                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            )
            return Response(
                LeadSourceSerializer(row, context=self.get_serializer_context()).data,
                status=status.HTTP_201_CREATED,
            )
        return self._sub_resource(request, "source_entries", LeadSourceSerializer)

    @action(detail=True, methods=["get", "post"], url_path="notes")
    def notes(self, request, pk=None):
        return self._sub_resource(request, "notes", LeadNoteSerializer, author=request.user)

    @action(detail=True, methods=["get", "post"], url_path="emails")
    def emails(self, request, pk=None):
        if request.method == "POST":
            raw = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
            data = dict(raw)
            # A drawer "Send Email" is sent, not a draft.
            if not data.get("sent_at") and not data.get("sentAt"):
                data["sent_at"] = timezone.now().isoformat()
            lead = self.get_object()
            serializer = LeadEmailSerializer(
                data=data, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)
            row = serializer.save(
                client_id=request.client_id, lead=lead,
                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            )
            return Response(
                LeadEmailSerializer(row, context=self.get_serializer_context()).data,
                status=status.HTTP_201_CREATED,
            )
        return self._sub_resource(request, "emails", LeadEmailSerializer)

    def _delete_sub_resource(self, request, pk, row_id, related_name):
        """Soft-delete one lead sub-resource row (sources, emails, …)."""
        lead = self.get_object()
        row = getattr(lead, related_name).filter(pk=row_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That record no longer exists.")
        row.soft_delete(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["patch", "delete"], url_path=r"sources/(?P<row_id>[^/.]+)")
    def source_detail(self, request, pk=None, row_id=None):
        lead = self.get_object()
        row = lead.source_entries.filter(pk=row_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That record no longer exists.")
        if request.method == "DELETE":
            return self._delete_sub_resource(request, pk, row_id, "source_entries")
        raw = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
        data = dict(raw)
        # Same label -> Source resolution as POST so an edit can change the
        # channel ("Website", "Referral", …) without knowing the Source PK.
        source_name = (
            data.get("sourceName") or data.get("source_name")
            or data.get("source") or data.get("name")
            or (data.get("campaign") if not data.get("sourceId") and not data.get("source_id") else None)
        )
        if not data.get("sourceId") and not data.get("source_id") and source_name:
            label = str(source_name).strip()
            if label:
                source_obj = Source.objects.filter(
                    client_id=request.client_id, name__iexact=label, deleted_at__isnull=True
                ).first()
                if source_obj is None:
                    source_obj = Source.objects.create(
                        client_id=request.client_id, name=label,
                        created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                    )
                data["sourceId"] = str(source_obj.id)
        serializer = LeadSourceSerializer(
            row, data=data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            LeadSourceSerializer(row, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["delete"], url_path=r"emails/(?P<row_id>[^/.]+)")
    def email_detail(self, request, pk=None, row_id=None):
        return self._delete_sub_resource(request, pk, row_id, "emails")

    @action(detail=True, methods=["delete"], url_path=r"notes/(?P<row_id>[^/.]+)")
    def note_detail(self, request, pk=None, row_id=None):
        return self._delete_sub_resource(request, pk, row_id, "notes")

    @action(detail=True, methods=["get", "post"], url_path="files")
    def files(self, request, pk=None):
        return self._sub_resource(request, "files", LeadFileSerializer)

    @action(detail=True, methods=["delete"], url_path=r"files/(?P<file_id>[^/.]+)")
    def file_detail(self, request, pk=None, file_id=None):
        lead = self.get_object()
        row = lead.files.filter(pk=file_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That attachment no longer exists.")
        row.soft_delete(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"], url_path="threads")
    def threads(self, request, pk=None):
        return self._sub_resource(request, "threads", LeadThreadSerializer)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"threads/(?P<thread_id>[^/.]+)/messages",
    )
    def thread_messages(self, request, pk=None, thread_id=None):
        lead = self.get_object()
        thread = lead.threads.filter(pk=thread_id, deleted_at__isnull=True).first()
        if thread is None:
            raise NotFound("That thread no longer exists.")
        serializer = LeadThreadMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = serializer.save(
            client_id=request.client_id, thread=thread, author=request.user
        )
        return Response(
            LeadThreadMessageSerializer(message).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["post"], url_path="import")
    def bulk_import(self, request):
        from apps.masters.views import _import_rows

        rows = request.data.get("rows") or []
        dry_run = bool(request.data.get("dryRun"))
        return Response(
            _import_rows(request, rows, dry_run, LeadSerializer, Lead, required=["name"])
        )

    @action(detail=False, methods=["get"])
    def export(self, request):
        rows = self.filter_queryset(self.get_queryset())
        services.annotate_lead_counters(request.client_id, rows[:1000])
        return Response(
            envelope(LeadSerializer(rows[:1000], many=True).data)
        )


# ---------------------------------------------------------------------------
# Stage and task configuration (api.md §9.3)
# ---------------------------------------------------------------------------
class StageViewSet(TenantModelViewSet):
    queryset = Stage.objects.all()
    serializer_class = StageSerializer
    audit_entity_type = "CrmStage"
    audit_label_field = "name"
    status_field = None
    ordering = ["sequence"]
    permission_map = {"read": ["view_lead"], "write": ["manage_pipeline"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            lead_count=Count("leads", filter=Q(leads__deleted_at__isnull=True))
        )

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def reorder(self, request):
        """``{ order: [id] }`` -- drag-reorder (api.md §9.3)."""
        order = request.data.get("order") or []
        if not isinstance(order, list) or not order:
            raise ValidationFailed(
                "Provide the new stage order.",
                field_errors={"order": ["Expected a non-empty list of ids."]},
            )
        stages = {
            str(stage.id): stage
            for stage in Stage.objects.filter(
                client_id=request.client_id, pk__in=order, deleted_at__isnull=True
            )
        }
        for index, stage_id in enumerate(order, start=1):
            stage = stages.get(str(stage_id))
            if stage is not None:
                stage.sequence = index
                stage.save(update_fields=["sequence", "updated_at"])
        return Response({"reordered": len(stages)})


class DealStageViewSet(TenantModelViewSet):
    queryset = DealStage.objects.all()
    serializer_class = DealStageSerializer
    audit_entity_type = "CrmDealStage"
    audit_label_field = "name"
    status_field = None
    ordering = ["sequence"]
    permission_map = {"read": ["view_lead"], "write": ["manage_pipeline"]}


class MasterTaskViewSet(TenantModelViewSet):
    queryset = MasterTask.objects.prefetch_related("stages")
    serializer_class = MasterTaskSerializer
    audit_entity_type = "CrmMasterTask"
    audit_label_field = "title"
    status_field = None
    ordering = ["sort_order", "title"]
    permission_map = {"read": ["view_task"], "write": ["manage_pipeline"]}


class StageTaskViewSet(TenantModelViewSet):
    queryset = StageTask.objects.select_related("stage")
    serializer_class = StageTaskSerializer
    audit_entity_type = "CrmStageTask"
    audit_label_field = "title"
    status_field = None
    ordering = ["sort_order"]
    filter_map = {"stageId": "stage_id"}
    permission_map = {"read": ["view_task"], "write": ["manage_pipeline"]}


class TaskViewSet(TenantModelViewSet):
    queryset = Task.objects.select_related("lead", "deal", "assignee")
    serializer_class = TaskSerializer
    audit_entity_type = "CrmTask"
    audit_label_field = "title"
    status_field = "status"
    default_date_field = "due_date"
    search_fields = ["title", "description", "lead__name"]
    ordering = ["due_date", "-created_at"]
    filter_map = {
        "leadId": "lead_id",
        "dealId": "deal_id",
        "assigneeId": "assignee_id",
        "priority": "priority",
        "source": "source",
    }
    permission_map = {
        "list": ["view_task"],
        "retrieve": ["view_task"],
        "create": ["create_task"],
        "update": ["edit_task"],
        "partial_update": ["edit_task"],
        "destroy": ["delete_task"],
    }

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        due = self.request.query_params.get("dueDate")
        if due:
            queryset = queryset.filter(due_date=due)
        if self.request.query_params.get("mine") == "true":
            queryset = queryset.filter(assignee=self.request.user)
        return queryset

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            open=Count("id", filter=Q(status="Open")),
            inProgress=Count("id", filter=Q(status="In Progress")),
            completed=Count("id", filter=Q(status="Completed")),
            overdue=Count(
                "id",
                filter=Q(due_date__lt=timezone.localdate()) & ~Q(status="Completed"),
            ),
        )

    def perform_create(self, serializer):
        serializer.validated_data["task_number"] = allocate_number(
            self.request.user.client, "TSK"
        )
        serializer.validated_data.setdefault("source", "Manual")
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """``{ outcome, nextAction, note, completedBy }`` (api.md §9.3)."""
        serializer = CompleteTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = services.complete_task(
            self.get_object(),
            outcome=data.get("outcome"),
            next_action=data.get("nextAction"),
            note=data.get("note"),
            user=request.user,
        )
        return Response(
            {
                "task": TaskSerializer(result["task"]).data,
                "followUpTask": (
                    TaskSerializer(result["followUpTask"]).data
                    if result["followUpTask"]
                    else None
                ),
                "createdTasks": TaskSerializer(result["createdTasks"], many=True).data,
                "stageChanged": result["stageChanged"],
            }
        )


class TaskAllocationViewSet(TenantModelViewSet):
    queryset = TaskAllocation.objects.select_related("assignee", "assigned_by").prefetch_related(
        "audit_entries"
    )
    serializer_class = TaskAllocationSerializer
    audit_entity_type = "CrmTaskAllocation"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "description", "department"]
    ordering = ["-created_at"]
    filter_map = {"assigneeId": "assignee_id", "department": "department", "priority": "priority"}
    permission_map = {"read": ["view_task"], "write": ["manage_task_allocation"]}

    def _append_audit(self, allocation, action_name, text):
        """Every assignment and status change appends a human-readable line;
        the server generates the string from the structured record (api.md §9.3)."""
        TaskAllocationAudit.objects.create(
            client_id=allocation.client_id,
            allocation=allocation,
            action=action_name,
            text=text,
            actor=self.request.user,
        )

    def perform_create(self, serializer):
        serializer.validated_data.setdefault("assigned_by", self.request.user)
        allocation = super().perform_create(serializer)
        assignee = allocation.assignee.name if allocation.assignee_id else "nobody"
        self._append_audit(
            allocation, "assigned", f"{self.request.user.name} assigned this to {assignee}"
        )
        return allocation

    def perform_update(self, serializer):
        previous_status = serializer.instance.status
        previous_assignee = serializer.instance.assignee_id
        allocation = super().perform_update(serializer)

        if allocation.status != previous_status:
            self._append_audit(
                allocation,
                "status",
                f"{self.request.user.name} moved this to {allocation.status}",
            )
        if allocation.assignee_id != previous_assignee:
            name = allocation.assignee.name if allocation.assignee_id else "nobody"
            self._append_audit(
                allocation, "reassigned", f"{self.request.user.name} reassigned this to {name}"
            )
        return allocation

    @action(detail=False, methods=["post"])
    def assign(self, request):
        """``POST /crm/task-allocations/assign/`` -- the AssignTaskModal."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TeamRosterView(APIView):
    """``GET /crm/team-roster/`` -- replaces the hardcoded ``CRM_TEAM_MEMBERS``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_task"]

    def get(self, request):
        return Response({"roster": services.team_roster(request.client_id)})


# ---------------------------------------------------------------------------
# Deals, contracts, projects (api.md §9.4 - §9.6)
# ---------------------------------------------------------------------------
class DealViewSet(TenantModelViewSet):
    queryset = Deal.objects.select_related("party", "owner", "lead")
    serializer_class = DealSerializer
    audit_entity_type = "CrmDeal"
    audit_label_field = "deal_number"
    status_field = "stage"
    search_fields = ["title", "deal_number", "party__name"]
    ordering = ["-created_at"]
    filter_map = {"ownerId": "owner_id", "customerId": "party_id", "stage": "stage"}
    default_date_field = "expected_close_date"
    permission_map = {"read": ["view_lead"], "write": ["manage_deals"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            pipelineValue=money_sum("value"),
            won=Count("id", filter=Q(stage="Won")),
            lost=Count("id", filter=Q(stage="Lost")),
            openValue=money_sum("value", filter=~Q(stage__in=["Won", "Lost"])),
        )

    def perform_create(self, serializer):
        serializer.validated_data["deal_number"] = allocate_number(
            self.request.user.client, "DEAL"
        )
        deal = super().perform_create(serializer)
        DealActivity.objects.create(
            deal=deal, type="created", description="Deal created", actor=self.request.user
        )
        return deal

    def perform_update(self, serializer):
        previous = serializer.instance.stage
        deal = super().perform_update(serializer)
        if deal.stage != previous:
            if deal.stage in ("Won", "Lost") and deal.closed_at is None:
                deal.closed_at = timezone.now()
                deal.save(update_fields=["closed_at", "updated_at"])
            DealActivity.objects.create(
                deal=deal,
                type="stage_change",
                description=f"Stage moved from {previous} to {deal.stage}",
                actor=self.request.user,
            )
        return deal

    @action(detail=True, methods=["get", "post"])
    def activities(self, request, pk=None):
        deal = self.get_object()
        if request.method == "GET":
            return Response(
                envelope(DealActivitySerializer(deal.activities.all(), many=True).data)
            )
        serializer = DealActivitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = serializer.save(deal=deal, actor=request.user)
        return Response(DealActivitySerializer(row).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"], url_path="lines")
    def deal_lines(self, request, pk=None):
        """Priced deliverable lines (the deal workspace Products tab)."""
        deal = self.get_object()
        if request.method == "GET":
            rows = deal.lines.filter(deleted_at__isnull=True)
            return Response(envelope(DealLineSerializer(rows, many=True).data))
        serializer = DealLineSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        row = serializer.save(client_id=request.client_id, deal=deal)
        return Response(
            DealLineSerializer(row, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"lines/(?P<line_id>[^/.]+)",
    )
    def deal_line_detail(self, request, pk=None, line_id=None):
        deal = self.get_object()
        row = deal.lines.filter(pk=line_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That deal line no longer exists.")
        if request.method == "DELETE":
            row.soft_delete(request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = DealLineSerializer(
            row, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            DealLineSerializer(row, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["get", "post"], url_path="documents")
    def deal_documents(self, request, pk=None):
        """Files pinned to a deal (bytes live in ``core.File``)."""
        deal = self.get_object()
        if request.method == "GET":
            rows = deal.documents.filter(deleted_at__isnull=True)
            return Response(envelope(DealDocumentSerializer(rows, many=True).data))
        serializer = DealDocumentSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        row = serializer.save(client_id=request.client_id, deal=deal)
        return Response(
            DealDocumentSerializer(row, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"documents/(?P<document_id>[^/.]+)",
    )
    def deal_document_detail(self, request, pk=None, document_id=None):
        deal = self.get_object()
        row = deal.documents.filter(pk=document_id, deleted_at__isnull=True).first()
        if row is None:
            raise NotFound("That document no longer exists.")
        row.soft_delete(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"])
    def tasks(self, request, pk=None):
        deal = self.get_object()
        if request.method == "GET":
            rows = deal.tasks.filter(deleted_at__isnull=True)
            return Response(envelope(TaskSerializer(rows, many=True).data))

        serializer = TaskSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        task = serializer.save(
            client_id=request.client_id,
            deal=deal,
            task_number=allocate_number(request.user.client, "TSK"),
            created_by=request.user,
        )
        return Response(TaskSerializer(task).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="create-quotation")
    @transaction.atomic
    def create_quotation(self, request, pk=None):
        from apps.sales.models import Quotation
        from apps.sales.serializers import QuotationSerializer

        deal = self.get_object()
        if deal.party_id is None:
            raise ValidationFailed(
                "Link a customer to this deal before creating a quotation.",
                field_errors={"customerId": ["Required."]},
            )

        quotation = Quotation(
            client_id=request.client_id,
            party=deal.party,
            doc_date=timezone.localdate(),
            status="Draft",
            crm_deal=deal,
            crm_lead=deal.lead,
            subject=deal.title,
            created_by=request.user,
        )
        quotation.freeze_party_snapshot()
        quotation.quotation_number = allocate_number(
            request.user.client, "QT", quotation.doc_date
        )
        quotation.save()

        deal.quotation = quotation
        deal.save(update_fields=["quotation", "updated_at"])
        return Response(
            QuotationSerializer(quotation, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="create-project")
    @transaction.atomic
    def create_project(self, request, pk=None):
        deal = self.get_object()
        if deal.crm_project_id:
            raise Conflict("This deal already has a project.", code=Codes.ALREADY_DONE)

        project = CrmProject.objects.create(
            client_id=request.client_id,
            name=request.data.get("name") or deal.title,
            party=deal.party,
            deal=deal,
            owner=deal.owner or request.user,
            status="Active",
            value=deal.value,
            created_by=request.user,
        )
        deal.crm_project = project
        deal.save(update_fields=["crm_project", "updated_at"])
        return Response(
            CrmProjectSerializer(project).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["get"])
    def pipeline(self, request):
        """``GET /crm/deals/pipeline/`` -- stage totals and conversion rates."""
        queryset = self.filter_queryset(self.get_queryset())
        rows = (
            queryset.values("stage")
            .annotate(count=Count("id"), value=money_sum("value"))
            .order_by("stage")
        )
        total = queryset.count() or 1
        won = queryset.filter(stage="Won").count()
        return Response(
            {
                "stages": [
                    {
                        "stage": row["stage"],
                        "count": row["count"],
                        "value": round2(row["value"]),
                        "sharePct": round(row["count"] / total * 100, 1),
                    }
                    for row in rows
                ],
                "conversionRatePct": round(won / total * 100, 1),
                "totalDeals": queryset.count(),
                "totalValue": round2(
                    queryset.aggregate(value=money_sum("value"))["value"]
                ),
            }
        )


class ContractViewSet(TenantModelViewSet):
    queryset = Contract.objects.select_related("party", "deal")
    serializer_class = ContractSerializer
    audit_entity_type = "CrmContract"
    audit_label_field = "contract_number"
    status_field = "status"
    default_date_field = "start_date"
    search_fields = ["contract_number", "title", "party__name"]
    ordering = ["-start_date"]
    filter_map = {"customerId": "party_id", "dealId": "deal_id"}
    permission_map = {"read": ["view_lead"], "write": ["manage_deals"]}

    def perform_create(self, serializer):
        serializer.validated_data["contract_number"] = allocate_number(
            self.request.user.client, "CON", serializer.validated_data.get("start_date")
        )
        return super().perform_create(serializer)

    @action(detail=False, methods=["get"])
    def templates(self, request):
        """``CONTRACT_TEMPLATES`` (api.md §9.5), served from the server."""
        return Response(
            envelope(
                [
                    {
                        "key": "supply",
                        "name": "Supply Agreement",
                        "description": "Recurring supply of fabricated goods.",
                    },
                    {
                        "key": "amc",
                        "name": "Annual Maintenance Contract",
                        "description": "Scheduled servicing and spares cover.",
                    },
                    {
                        "key": "installation",
                        "name": "Installation & Commissioning",
                        "description": "On-site erection and handover.",
                    },
                    {
                        "key": "nda",
                        "name": "Non-Disclosure Agreement",
                        "description": "Mutual confidentiality.",
                    },
                ]
            )
        )

    @action(detail=True, methods=["get"])
    def print(self, request, pk=None):
        from apps.core.printing import print_payload

        contract = self.get_object()
        return Response(
            print_payload(
                contract, self.get_serializer_class(), request=request, title="Contract"
            )
        )

    @action(detail=True, methods=["post"], url_path="send-for-signature")
    def send_for_signature(self, request, pk=None):
        from apps.core.printing import PdfNotAvailable

        self.get_object()
        raise PdfNotAvailable(
            "E-signature is not configured on this workspace.",
            code="ESIGN_NOT_CONFIGURED",
        )


class CrmProjectViewSet(TenantModelViewSet):
    queryset = CrmProject.objects.select_related("party", "deal", "owner")
    serializer_class = CrmProjectSerializer
    audit_entity_type = "CrmProject"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code", "party__name"]
    ordering = ["-created_at"]
    permission_map = {"read": ["view_projects"], "write": ["create_project"]}


# ---------------------------------------------------------------------------
# Configuration, allocation, tracking, forms (api.md §9.5 - §9.7)
# ---------------------------------------------------------------------------
class SourceViewSet(TenantModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    audit_entity_type = "CrmSource"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]


class IndustryViewSet(TenantModelViewSet):
    queryset = Industry.objects.all()
    serializer_class = IndustrySerializer
    audit_entity_type = "CrmIndustry"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]


class LostReasonViewSet(TenantModelViewSet):
    queryset = LostReason.objects.all()
    serializer_class = LostReasonSerializer
    audit_entity_type = "CrmLostReason"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]


class UserAllocationViewSet(TenantModelViewSet):
    queryset = UserAllocation.objects.select_related("user", "industry")
    serializer_class = UserAllocationSerializer
    audit_entity_type = "CrmUserAllocation"
    status_field = None
    ordering = ["user__name"]
    permission_map = {"read": ["view_task"], "write": ["manage_task_allocation"]}


class UserLocationViewSet(ReadOnlyTenantViewSet):
    """Field-user location tracking (api.md §9.6)."""

    queryset = UserLocation.objects.select_related("user")
    serializer_class = UserLocationSerializer
    filter_soft_deleted = False
    status_field = None
    filter_map = {"userId": "user_id"}
    ordering = ["-recorded_at"]
    permission_map = {"read": ["view_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        on_date = self.request.query_params.get("date")
        if on_date:
            queryset = queryset.filter(recorded_at__date=on_date)
        return queryset

    def create(self, request):
        """Mobile ping ``{ userId, lat, lng, accuracy, recordedAt }``."""
        row = UserLocation.objects.create(
            client_id=request.client_id,
            user_id=request.data.get("userId") or request.user.id,
            latitude=request.data.get("lat") or request.data.get("latitude"),
            longitude=request.data.get("lng") or request.data.get("longitude"),
            accuracy=request.data.get("accuracy"),
            recorded_at=request.data.get("recordedAt") or timezone.now(),
        )
        return Response(
            UserLocationSerializer(row).data, status=status.HTTP_201_CREATED
        )


class FormViewSet(TenantModelViewSet):
    queryset = Form.objects.all()
    serializer_class = FormSerializer
    audit_entity_type = "CrmForm"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name", "slug"]
    ordering = ["name"]
    filter_map = {"kind": "kind", "isPublished": "is_published"}
    permission_map = {"read": ["view_lead"], "write": ["manage_pipeline"]}

    @action(detail=False, methods=["get"], url_path="field-library")
    def field_library(self, request):
        """``FIELD_LIBRARY`` field types (api.md §9.7)."""
        return Response(
            envelope(
                [
                    {"type": "text", "label": "Single line text"},
                    {"type": "textarea", "label": "Paragraph"},
                    {"type": "number", "label": "Number"},
                    {"type": "email", "label": "Email"},
                    {"type": "phone", "label": "Phone"},
                    {"type": "date", "label": "Date"},
                    {"type": "select", "label": "Dropdown"},
                    {"type": "multiselect", "label": "Multi-select"},
                    {"type": "radio", "label": "Radio group"},
                    {"type": "checkbox", "label": "Checkbox"},
                    {"type": "file", "label": "File upload"},
                    {"type": "currency", "label": "Currency"},
                    {"type": "heading", "label": "Section heading"},
                ]
            )
        )

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """Produce a public capture URL (api.md §9.7)."""
        from django.utils.text import slugify

        form = self.get_object()
        if not form.slug:
            base = slugify(form.name)[:100] or "form"
            slug, suffix = base, 1
            while Form.objects.filter(
                client_id=request.client_id, slug=slug, deleted_at__isnull=True
            ).exclude(pk=form.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"
            form.slug = slug

        form.is_published = True
        form.published_at = timezone.now()
        form.save(update_fields=["slug", "is_published", "published_at", "updated_at"])
        return Response(
            {
                "slug": form.slug,
                "url": f"/public/forms/{form.slug}/",
                "publishedAt": form.published_at,
            }
        )


class CrmSetupView(APIView):
    """``GET/PUT /crm/setup/`` -- stages, sources, industries, lost reasons."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_lead"], "write": ["manage_pipeline"]}

    def get(self, request):
        client_id = request.client_id
        return Response(
            {
                "stages": StageSerializer(
                    Stage.objects.filter(client_id=client_id, deleted_at__isnull=True)
                    .annotate(lead_count=Count("leads", filter=Q(leads__deleted_at__isnull=True)))
                    .order_by("sequence"),
                    many=True,
                ).data,
                "dealStages": DealStageSerializer(
                    DealStage.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).order_by("sequence"),
                    many=True,
                ).data,
                "sources": SourceSerializer(
                    Source.objects.filter(client_id=client_id, deleted_at__isnull=True),
                    many=True,
                ).data,
                "industries": IndustrySerializer(
                    Industry.objects.filter(client_id=client_id, deleted_at__isnull=True),
                    many=True,
                ).data,
                "lostReasons": LostReasonSerializer(
                    LostReason.objects.filter(client_id=client_id, deleted_at__isnull=True),
                    many=True,
                ).data,
            }
        )

    @transaction.atomic
    def put(self, request):
        client_id = request.client_id
        created = {}
        for key, model, serializer_class in (
            ("sources", Source, SourceSerializer),
            ("industries", Industry, IndustrySerializer),
            ("lostReasons", LostReason, LostReasonSerializer),
        ):
            names = request.data.get(key)
            if names is None:
                continue
            existing = set(
                model.objects.filter(
                    client_id=client_id, deleted_at__isnull=True
                ).values_list("name", flat=True)
            )
            new_rows = [
                model(client_id=client_id, name=name)
                for name in names
                if name and name not in existing
            ]
            model.objects.bulk_create(new_rows)
            created[key] = len(new_rows)
        return self.get(request)


class CrmDashboardView(APIView):
    """``GET /crm/dashboard/`` -- funnel, by-source, by-owner, recent."""

    permission_classes = [HasModulePermission]
    required_permissions = ["show_crm_dashboard"]

    def get(self, request):
        client_id = request.client_id
        leads = Lead.objects.filter(client_id=client_id, deleted_at__isnull=True)
        deals = Deal.objects.filter(client_id=client_id, deleted_at__isnull=True)

        funnel = (
            leads.values("stage__name", "stage__sequence")
            .annotate(count=Count("id"), value=money_sum("amount"))
            .order_by("stage__sequence")
        )
        by_source = (
            leads.values("source__name").annotate(count=Count("id")).order_by("-count")[:10]
        )
        by_owner = (
            leads.values("owner__name")
            .annotate(count=Count("id"), value=money_sum("amount"))
            .order_by("-count")[:10]
        )
        recent = leads.order_by("-created_at")[:10]
        services.annotate_lead_counters(client_id, list(recent))

        return Response(
            {
                "kpis": {
                    "totalLeads": leads.count(),
                    "openDeals": deals.exclude(stage__in=["Won", "Lost"]).count(),
                    "wonDeals": deals.filter(stage="Won").count(),
                    "pipelineValue": round2(
                        deals.exclude(stage__in=["Won", "Lost"]).aggregate(
                            value=money_sum("value")
                        )["value"]
                    ),
                    "openTasks": Task.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).exclude(status="Completed").count(),
                },
                "funnel": [
                    {
                        "stage": row["stage__name"],
                        "count": row["count"],
                        "value": round2(row["value"]),
                    }
                    for row in funnel
                ],
                "bySource": [
                    {"source": row["source__name"] or "Unknown", "count": row["count"]}
                    for row in by_source
                ],
                "byOwner": [
                    {
                        "owner": row["owner__name"] or "Unassigned",
                        "count": row["count"],
                        "value": round2(row["value"]),
                    }
                    for row in by_owner
                ],
                "recentLeads": LeadSerializer(recent, many=True).data,
            }
        )


class CrmReportView(APIView):
    """``GET /crm/reports/{reportKey}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_lead"]

    def get(self, request, report_key):
        client_id = request.client_id
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        leads = Lead.objects.filter(client_id=client_id, deleted_at__isnull=True)
        deals = Deal.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if date_from:
            leads = leads.filter(created_on__gte=date_from)
            deals = deals.filter(created_at__gte=date_from)
        if date_to:
            leads = leads.filter(created_on__lte=date_to)
            deals = deals.filter(created_at__lte=date_to)

        if report_key == "crm-pipeline":
            rows = (
                deals.values("stage")
                .annotate(count=Count("id"), value=money_sum("value"))
                .order_by("stage")
            )
            return Response(
                envelope(
                    [
                        {
                            "stage": row["stage"],
                            "count": row["count"],
                            "value": round2(row["value"]),
                        }
                        for row in rows
                    ]
                )
            )

        if report_key == "crm-conversion":
            total = leads.count() or 1
            converted = leads.filter(converted_deal__isnull=False).count()
            won = deals.filter(stage="Won").count()
            return Response(
                {
                    "totalLeads": leads.count(),
                    "converted": converted,
                    "conversionRatePct": round(converted / total * 100, 1),
                    "wonDeals": won,
                    "winRatePct": round(won / (deals.count() or 1) * 100, 1),
                }
            )

        raise NotFound(f"Unknown report '{report_key}'.")


class ReminderView(APIView):
    """``GET /crm/reminders/`` -- the reminder centre feed."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_task"]

    def get(self, request):
        today = timezone.localdate()
        tasks = Task.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True, assignee=request.user
        ).exclude(status="Completed").select_related("lead").order_by("due_date")

        overdue = [t for t in tasks if t.due_date and t.due_date < today]
        due_today = [t for t in tasks if t.due_date == today]
        upcoming = [t for t in tasks if t.due_date and t.due_date > today][:20]

        return Response(
            {
                "overdue": TaskSerializer(overdue, many=True).data,
                "dueToday": TaskSerializer(due_today, many=True).data,
                "upcoming": TaskSerializer(upcoming, many=True).data,
                "counts": {
                    "overdue": len(overdue),
                    "dueToday": len(due_today),
                    "upcoming": len(upcoming),
                },
            }
        )
