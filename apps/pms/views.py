"""PMS endpoints (api.md §10)."""
import hashlib
import secrets
from datetime import datetime, timedelta

from decimal import Decimal
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import notify, record_audit
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import TenantModelViewSet

from . import services
from .models import (
    CLOSED_STAGE_STATUSES,
    Approval,
    Delay,
    Department,
    Document,
    ProductionEntryLink,
    Project,
    ProjectStage,
    ProofShare,
    StageConfig,
    Task,
)
from .serializers import (
    ApplyTemplateSerializer,
    ApprovalSerializer,
    AssignStageSerializer,
    CompleteProjectSerializer,
    DecideSerializer,
    DelaySerializer,
    DepartmentSerializer,
    DocumentSerializer,
    FromOrderSerializer,
    HandoffSerializer,
    LogDelaySerializer,
    PmsSettingsSerializer,
    ProgressSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectStageSerializer,
    ProofShareSerializer,
    RequestApprovalSerializer,
    ShareProofSerializer,
    StageConfigSerializer,
    StagePercentagesSerializer,
    StageStatusSerializer,
    TaskSerializer,
)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Configuration (api.md §10.1)
# ---------------------------------------------------------------------------
class DepartmentViewSet(TenantModelViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    audit_entity_type = "PmsDepartment"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]
    permission_map = {"read": ["view_pms"], "write": ["view_pms"]}

    def check_delete_allowed(self, department):
        """409 when in use unless ``?reassignTo=`` (api.md §10.1)."""
        usage = self._usage(department)
        total = sum(usage.values())
        if not total:
            if Department.objects.filter(
                client_id=self.get_client_id(), deleted_at__isnull=True
            ).count() <= 1:
                raise Conflict(
                    "At least one department must remain.", code=Codes.LAST_ONE
                )
            return

        reassign_to = self.request.query_params.get("reassignTo")
        if not reassign_to:
            raise Conflict(
                f"This department is referenced by {total} record(s).",
                code=Codes.IN_USE,
                detail="Pass ?reassignTo=<departmentId> to move them first.",
                payload={"usage": usage},
            )

        target = Department.objects.filter(
            pk=reassign_to, client_id=self.get_client_id(), deleted_at__isnull=True
        ).first()
        if target is None or target.pk == department.pk:
            raise ValidationFailed(
                "Choose a different department to reassign to.",
                field_errors={"reassignTo": ["Unknown or identical department."]},
            )

        StageConfig.objects.filter(department=department).update(department=target)
        ProjectStage.objects.filter(department=department).update(department=target)
        Task.objects.filter(department=department).update(department=target)
        Project.objects.filter(current_department=department).update(
            current_department=target
        )

    def _usage(self, department):
        return {
            "stageConfigs": StageConfig.objects.filter(
                department=department, deleted_at__isnull=True
            ).count(),
            "projectStages": ProjectStage.objects.filter(
                department=department, deleted_at__isnull=True
            ).count(),
            "tasks": Task.objects.filter(
                department=department, deleted_at__isnull=True
            ).count(),
        }

    @action(detail=True, methods=["get"])
    def usage(self, request, pk=None):
        return Response(self._usage(self.get_object()))


class StageConfigViewSet(TenantModelViewSet):
    queryset = StageConfig.objects.select_related("department")
    serializer_class = StageConfigSerializer
    audit_entity_type = "PmsStageConfig"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name", "description"]
    ordering = ["sequence"]
    filter_map = {"departmentId": "department_id", "isActive": "is_active"}
    permission_map = {"read": ["view_pms"], "write": ["view_pms"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("sequence"):
            last = StageConfig.objects.filter(
                client_id=self.get_client_id(), deleted_at__isnull=True
            ).order_by("-sequence").values_list("sequence", flat=True).first()
            serializer.validated_data["sequence"] = (last or 0) + 1
        return super().perform_create(serializer)

    def check_delete_allowed(self, config):
        # Instances are frozen copies (name, gates, durations) and
        # ``stage_config`` is SET_NULL, so removing a template never rewrites a
        # running project -- it only stops future projects from getting it.
        # That is what the delete dialog promises, so a template must delete
        # cleanly instead of resurrecting with a 409. Even the last template
        # may go: an empty pipeline is a valid state (the list renders its
        # empty state and new projects simply start with no stages).
        return None

    @action(detail=True, methods=["get"])
    def usage(self, request, pk=None):
        config = self.get_object()
        projects = Project.objects.filter(
            stages__stage_config=config, deleted_at__isnull=True
        ).distinct()
        return Response(
            {
                "projectCount": projects.count(),
                "projects": ProjectListSerializer(projects[:50], many=True).data,
            }
        )

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        config = self.get_object()
        config.is_active = not config.is_active
        config.save(update_fields=["is_active", "updated_at"])
        return Response(self.get_serializer(config).data)

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def reorder(self, request):
        """``{ id, direction }`` or a full ``{ order: [] }`` (api.md §10.1).

        Renumbering happens through a temporary offset so two rows never hold
        the same sequence mid-statement -- the portable equivalent of the
        deferrable unique index db.md §10.1 describes.
        """
        client_id = request.client_id
        configs = list(
            StageConfig.objects.filter(
                client_id=client_id, deleted_at__isnull=True
            ).order_by("sequence")
        )

        order = request.data.get("order")
        if order:
            index = {str(config_id): position for position, config_id in enumerate(order)}
            configs.sort(key=lambda c: index.get(str(c.id), len(index)))
        else:
            config_id = request.data.get("id")
            direction = request.data.get("direction")
            if not config_id or direction not in ("up", "down"):
                raise ValidationFailed(
                    "Provide either an order list or an id with a direction.",
                    field_errors={"direction": ["Expected 'up' or 'down'."]},
                )
            position = next(
                (i for i, c in enumerate(configs) if str(c.id) == str(config_id)), None
            )
            if position is None:
                raise NotFound("That stage template no longer exists.")
            target = position - 1 if direction == "up" else position + 1
            if 0 <= target < len(configs):
                configs[position], configs[target] = configs[target], configs[position]

        offset = 10_000
        for index, config in enumerate(configs, start=1):
            StageConfig.objects.filter(pk=config.pk).update(sequence=index + offset)
        for index, config in enumerate(configs, start=1):
            StageConfig.objects.filter(pk=config.pk).update(sequence=index)

        return Response(
            envelope(
                self.get_serializer(
                    StageConfig.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).order_by("sequence"),
                    many=True,
                ).data
            )
        )


class PmsSettingsView(APIView):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["view_pms"]}

    def get(self, request):
        return Response(services.settings_payload(request.client_id))

    def put(self, request):
        serializer = PmsSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        settings = services.get_settings(request.client_id)
        mapping = {
            "atRiskThresholdPct": "at_risk_threshold_pct",
            "requireClientApprovalOnDesign": "require_client_approval_on_design",
            "requireQaCertificate": "require_qa_certificate",
            "notifications": "notifications",
            "defaultDepartmentCapacity": "default_department_capacity",
            "departmentCapacity": "department_capacity",
            "statusColors": "status_colors",
            "delayCategories": "delay_categories",
        }
        for key, field in mapping.items():
            if key in data:
                setattr(settings, field, data[key])

        # api.md §10.1 -- `overdue` can be recoloured but never removed.
        colors = settings.status_colors or {}
        colors.setdefault("overdue", "#d03b3b")
        settings.status_colors = colors

        settings.save()
        return Response(services.settings_payload(request.client_id))

    patch = put


# ---------------------------------------------------------------------------
# Projects (api.md §10.2)
# ---------------------------------------------------------------------------
def create_project_from_order(order, *, project_manager_id=None, priority="Medium",
                              stage_config_ids=None, stage_weights=None, user=None,
                              start_date=None, specifications=None):
    """``POST /pms/projects/from-order/`` -- pulls customer + product from the order."""
    first_line = order.line_items.filter(deleted_at__isnull=True).order_by("line_no").first()

    project = Project.objects.create(
        client_id=order.client_id,
        code=allocate_number(order.client, "PRJ"),
        sales_order=order,
        party=order.party,
        customer_name=order.party_name or order.party.name,
        product_name=first_line.item_name if first_line else None,
        order_value=order.total,
        quantity=first_line.qty if first_line else None,
        specifications=specifications,
        project_manager_id=project_manager_id,
        priority=priority,
        status="Draft",
        start_date=start_date or timezone.now(),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    order.pms_project = project
    order.save(update_fields=["pms_project", "updated_at"])

    if stage_config_ids:
        apply_stage_template(
            project, stage_config_ids, stage_weights=stage_weights, user=user
        )

    record_audit(
        client=order.client_id,
        actor=user,
        action="PROJECT_CREATED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"Project created from order {order.order_number}",
    )
    return project


@transaction.atomic
def apply_stage_template(project, config_ids, *, stage_weights=None, user=None):
    """Instantiate stages from templates, copying the fields that matter.

    db.md §10.2: configs are copied, not referenced, for name, gates and
    durations -- editing a template must not retroactively change a running
    project's gates.
    """
    configs = list(
        StageConfig.objects.filter(
            client_id=project.client_id, pk__in=config_ids, deleted_at__isnull=True
        ).order_by("sequence")
    )
    if not configs:
        raise ValidationFailed(
            "No stage templates matched.",
            field_errors={"configIds": ["Unknown stage template ids."]},
        )

    if stage_weights:
        total_w = sum(
            float(stage_weights.get(str(c.id)) or stage_weights.get(c.name) or 0)
            for c in configs
        )
        if round(total_w, 2) != 100.0:
            raise ValidationFailed(
                f"Total stage percentage must equal 100% (currently {round(total_w, 2)}%).",
                field_errors={
                    "stageWeights": [
                        f"Total stage percentage must equal 100% (currently {round(total_w, 2)}%)."
                    ]
                },
            )

    start_sequence = (
        ProjectStage.objects.filter(project=project, deleted_at__isnull=True)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
        or 0
    )

    created = []
    num_configs = len(configs)
    equal_pct = round(100.0 / num_configs, 2) if num_configs > 0 else 0

    for offset, config in enumerate(configs, start=1):
        if stage_weights:
            weight_val = stage_weights.get(str(config.id)) or stage_weights.get(config.name) or 0
            weight = Decimal(str(weight_val))
        else:
            # Distribute 100% equally with rounding adjustment on the final stage
            if offset == num_configs:
                weight = Decimal(str(round(100.0 - equal_pct * (num_configs - 1), 2)))
            else:
                weight = Decimal(str(equal_pct))

        created.append(
            ProjectStage.objects.create(
                client_id=project.client_id,
                project=project,
                stage_config=config,
                name=config.name,
                sequence=start_sequence + offset,
                department=config.department,
                planned_duration=config.default_duration,
                duration_unit=config.duration_unit,
                required_approval=config.required_approval,
                required_document=config.required_document,
                weight_pct=weight,
                status="Not Started",
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
        )

    if project.current_stage_id is None and created:
        project.current_stage = created[0]
        project.current_department = created[0].department
        project.status = "In Progress" if project.status == "Draft" else project.status
        project.save(update_fields=["current_stage", "current_department", "status", "updated_at"])

    services.recalculate_project(project)
    record_audit(
        client=project.client_id,
        actor=user,
        action="STAGES_CONFIGURED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"{len(created)} stage(s) configured",
    )
    return created


class ProjectViewSet(TenantModelViewSet):
    queryset = Project.objects.select_related(
        "party", "project_manager", "current_stage", "current_department", "sales_order"
    )
    serializer_class = ProjectListSerializer
    audit_entity_type = "PmsProject"
    audit_label_field = "code"
    status_field = "status"
    search_fields = ["code", "customer_name", "product_name", "specifications"]
    ordering = ["-created_at"]
    default_date_field = "start_date"
    allowed_date_fields = (
        "start_date", "expected_completion_date", "actual_completion_date", "created_at",
    )
    filter_map = {
        "customer": "customer_name",
        "customerId": "party_id",
        "projectManagerId": "project_manager_id",
        "priority": "priority",
    }
    permission_map = {
        "read": ["view_pms"],
        "write": ["create_pms_project"],
        "complete": ["complete_project"],
        "apply_stage_template": ["assign_stage"],
        # Production permissions are checked per method inside the action.
        "production_link": ["view_pms"],
    }
    #: The UI uses ``code`` in URLs, so both a uuid and a code resolve.
    lookup_value_regex = "[^/]+"

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup = self.kwargs.get("pk")
        project = queryset.filter(code=lookup).first()
        if project is None:
            try:
                project = queryset.filter(pk=lookup).first()
            except (ValueError, TypeError, Exception):
                project = None
        if project is None:
            raise NotFound("That project no longer exists.")
        self.check_object_permissions(self.request, project)
        return project

    def get_serializer_class(self):
        if self.action in ("retrieve", "create", "update", "partial_update"):
            return ProjectDetailSerializer
        return ProjectListSerializer

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        params = self.request.query_params

        department = params.get("department")
        if department:
            queryset = queryset.filter(
                Q(current_department__name=department) | Q(stages__department__name=department)
            ).distinct()

        stage_name = params.get("stageName")
        if stage_name:
            queryset = queryset.filter(current_stage__name=stage_name)

        if params.get("delayedOnly") == "true":
            queryset = queryset.filter(
                Q(status="Delayed")
                | Q(stages__delays__resolved_at__isnull=True, stages__delays__deleted_at__isnull=True)
            ).distinct()

        return queryset

    def get_aggregates(self, queryset):
        return {
            **services.dashboard_kpis(self.get_client_id()),
            "filtered": queryset.count(),
        }

    def retrieve(self, request, *args, **kwargs):
        project = self.get_object()
        self._concurrency_instance = project
        from .pipeline_services import sync_project_pipeline_stages
        pipeline_res = sync_project_pipeline_stages(project)
        context = self._detail_context(project)
        context["pipeline_summary"] = pipeline_res["stockEvaluation"]
        context["pipeline_stages_map"] = {s["sequence"]: s for s in pipeline_res["stages"]}
        return Response(ProjectDetailSerializer(project, context=context).data)

    def _detail_context(self, project):
        """One round trip per child table, assembled here (db.md §15)."""
        stages = list(
            project.stages.filter(deleted_at__isnull=True)
            .select_related("department", "assigned_user")
            .order_by("sequence")
        )
        stage_ids = [stage.id for stage in stages]

        def bucket(rows):
            grouped = {}
            for row in rows:
                grouped.setdefault(row.stage_id, []).append(row)
            return grouped

        context = self.get_serializer_context()
        context.update(
            {
                "stages": stages,
                "tasks_by_stage": bucket(
                    Task.objects.filter(
                        stage_id__in=stage_ids, deleted_at__isnull=True
                    ).select_related("assigned_user", "department")
                ),
                "documents_by_stage": bucket(
                    Document.objects.filter(
                        stage_id__in=stage_ids, deleted_at__isnull=True
                    ).select_related("file", "uploaded_by")
                ),
                "approvals_by_stage": bucket(
                    Approval.objects.filter(
                        stage_id__in=stage_ids, deleted_at__isnull=True
                    ).select_related("requested_by")
                ),
                "delays_by_stage": bucket(
                    Delay.objects.filter(
                        stage_id__in=stage_ids, deleted_at__isnull=True
                    ).select_related("responsible_user")
                ),
                "at_risk_threshold": services.get_settings(
                    project.client_id
                ).at_risk_threshold_pct,
            }
        )
        return context

    def perform_create(self, serializer):
        serializer.validated_data["code"] = allocate_number(
            self.request.user.client, "PRJ"
        )
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        # A generic PATCH must never slip past the completion gate: completing
        # goes through the service so open stages still block it (api.md §10.3).
        instance = serializer.instance
        if (
            serializer.validated_data.get("status") == "Completed"
            and getattr(instance, "status", None) != "Completed"
        ):
            serializer.validated_data.pop("status", None)
            updated = super().perform_update(serializer)
            services.complete_project(updated, user=self.request.user, force=False)
            updated.refresh_from_db()
            self._concurrency_instance = updated
            return updated
        return super().perform_update(serializer)

    @action(detail=False, methods=["post"], url_path="from-order")
    @transaction.atomic
    def from_order(self, request):
        from apps.sales.models import SalesOrder

        serializer = FromOrderSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        target_id = data["salesOrderId"]
        order = None
        try:
            order = SalesOrder.objects.filter(
                pk=target_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()
        except (ValueError, TypeError, Exception):
            order = None
        if order is None:
            order = SalesOrder.objects.filter(
                order_number=target_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()
        if order is None:
            raise NotFound("That sales order no longer exists.")
        if order.pms_project_id:
            raise Conflict(
                "That order already has a project.",
                code=Codes.ALREADY_DONE,
                payload={"projectId": str(order.pms_project_id)},
            )

        stage_weights = data.get("stageWeights") or {}
        if not stage_weights and data.get("stages"):
            for s in data["stages"]:
                cid = s.get("configId") or s.get("stageConfigId") or s.get("id")
                pct = s.get("percentage")
                if pct is None:
                    pct = s.get("weightPct") or s.get("weight") or 0
                if cid:
                    stage_weights[str(cid)] = pct

        project = create_project_from_order(
            order,
            project_manager_id=data.get("projectManagerId"),
            priority=data.get("priority", "Medium"),
            stage_config_ids=data.get("stageConfigIds") or list(stage_weights.keys()),
            stage_weights=stage_weights or None,
            user=request.user,
            start_date=data.get("startDate"),
            specifications=data.get("specifications"),
        )
        return Response(
            ProjectDetailSerializer(project, context=self._detail_context(project)).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="apply-stage-template")
    @transaction.atomic
    def apply_stage_template(self, request, pk=None):
        project = self.get_object()
        serializer = ApplyTemplateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        apply_stage_template(
            project,
            serializer.validated_data["configIds"],
            stage_weights=serializer.validated_data.get("stageWeights"),
            user=request.user,
        )
        project.refresh_from_db()
        return Response(
            ProjectDetailSerializer(project, context=self._detail_context(project)).data
        )

    @action(detail=True, methods=["post", "patch"], url_path="stage-percentages")
    @transaction.atomic
    def stage_percentages(self, request, pk=None):
        project = self.get_object()

        # Permission gate: Only the project creator (or superuser) can modify stage percentages
        if (
            getattr(request.user, "is_authenticated", False)
            and not getattr(request.user, "is_superuser", False)
            and project.created_by_id is not None
            and project.created_by_id != request.user.id
        ):
            raise PermissionDenied("Only the project creator can modify stage percentages.")

        serializer = StagePercentagesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        stage_items = serializer.validated_data["stages"]

        active_stages = {
            str(s.id): s
            for s in project.stages.filter(deleted_at__isnull=True)
        }

        for item in stage_items:
            sid = str(item.get("id") or item.get("stageId"))
            pct = item.get("percentage")
            if pct is None:
                pct = item.get("weightPct") or item.get("weight") or 0
            if sid in active_stages:
                stage = active_stages[sid]
                stage.weight_pct = Decimal(str(pct))
                stage.save(update_fields=["weight_pct", "updated_at"])

        services.recalculate_project(project)
        record_audit(
            client=project.client_id,
            actor=request.user,
            action="STAGES_CONFIGURED",
            entity_type="PmsProject",
            entity_id=project.id,
            entity_label=project.code,
            description="Stage percentages updated",
        )
        project.refresh_from_db()
        return Response(
            ProjectDetailSerializer(project, context=self._detail_context(project)).data
        )

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        serializer = CompleteProjectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = services.complete_project(
            self.get_object(), user=request.user, force=serializer.validated_data["force"]
        )
        return Response(
            ProjectDetailSerializer(project, context=self._detail_context(project)).data
        )

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """Gantt rows for one project."""
        project = self.get_object()
        stages = project.stages.filter(deleted_at__isnull=True).select_related(
            "department", "assigned_user"
        ).order_by("sequence")
        return Response(
            envelope(
                [
                    {
                        "stageId": str(stage.id),
                        "name": stage.name,
                        "department": stage.department.name if stage.department_id else None,
                        "color": stage.department.color if stage.department_id else None,
                        "assignee": stage.assigned_user.name if stage.assigned_user_id else None,
                        "start": stage.start_datetime,
                        "end": stage.expected_completion_datetime,
                        "actualStart": stage.actual_start_datetime,
                        "actualEnd": stage.actual_completion_datetime,
                        "completionPct": stage.completion_pct,
                        "status": stage.status,
                        "isOverdue": services.is_stage_overdue(stage),
                    }
                    for stage in stages
                ]
            )
        )

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        from apps.core.models import AuditLog
        from apps.core.serializers_platform import AuditLogSerializer

        project = self.get_object()
        stage_ids = list(project.stages.values_list("id", flat=True))
        task_ids = list(project.tasks.values_list("id", flat=True))
        rows = AuditLog.objects.filter(
            client_id=request.client_id
        ).filter(Q(entity_id=project.id) | Q(entity_id__in=stage_ids + task_ids))[:200]
        return Response(envelope(AuditLogSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def approvals(self, request, pk=None):
        project = self.get_object()
        rows = project.approvals.filter(deleted_at__isnull=True).select_related(
            "requested_by", "document"
        )
        return Response(envelope(ApprovalSerializer(rows, many=True).data))

    @action(detail=True, methods=["get", "post"], url_path="customer-returns")
    @transaction.atomic
    def customer_returns(self, request, pk=None):
        """Comprehensive Customer Return, QC Inspection & Stock Routing Workspace.
        Enforces 5-phase verified lifecycle:
        1. Return Authorization & Request (Draft document, zero inventory inflation)
        2. Physical Inward Receipt into SYS-QC-HOLD (Quarantined, blocked from sale)
        3. Mandatory Piece-by-Piece QC Inspection with defect logging (DefectMaster)
        4. Authorized Stock Disposition (Customer Usable Stock, Own Project Buffer, Reprocess JWO, Scrap)
        5. Decoupled Credit Note posting to general ledger with invoice & commission reversal.
        """
        project = self.get_object()
        client_id = project.client_id
        import json
        from decimal import Decimal
        from django.db.models import Q, Sum
        from django.utils import timezone
        from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
        from apps.core.money import ZERO, D, round4, round2
        from apps.core.numbering import allocate_number
        from apps.sales.models import SalesReturn, SalesReturnLine, SalesInvoice, SalesInvoiceLine, DeliveryChallan, SalesOrder
        from apps.inventory.models import QCInspection, QCInspectionLine, QCInspectionLineDefect, LotTaka, GreyStockAllocation
        from apps.inventory import services as stock_svc
        from apps.inventory import allocation_services as allocations_svc
        from apps.masters.models import Location, DefectMaster, ReturnReasonMaster, Party, Item
        from apps.jobwork.models import JobWorkOrder, JWOReprocess
        from apps.sales.services import recalculate_document
        from apps.accounting import services as ledger
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        order = getattr(project, "sales_order", None)
        if not order and project.crm_order_id:
            order = SalesOrder.objects.filter(client_id=client_id, order_number=project.crm_order_id).first()

        main_wh = Location.objects.filter(client_id=client_id, code="WH-MAIN").first() or Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").first()
        qc_hold_loc = stock_svc.system_location(client_id, "QC_HOLD")
        reprocess_loc = stock_svc.system_location(client_id, "REPROCESS")
        second_quality_loc = stock_svc.system_location(client_id, "SECOND_QUALITY")
        reject_loc = stock_svc.system_location(client_id, "REJECT")

        def _get_notes_dict(notes_str):
            if not notes_str:
                return {}
            try:
                return json.loads(notes_str)
            except Exception:
                return {"customer_remarks": str(notes_str)}

        # -------------------------------------------------------------
        # 1. POST ACTION HANDLERS
        # -------------------------------------------------------------
        if request.method == "POST":
            action_type = request.data.get("action_type") or request.data.get("actionType")

            # A. CREATE RETURN REQUEST (Authorization, Zero Stock Impact)
            if action_type == "create_return_request":
                invoice_id = request.data.get("invoice_id") or request.data.get("invoiceId")
                if not invoice_id:
                    raise ValidationFailed("Target Sales Invoice is required.", field_errors={"invoiceId": ["Required."]})

                invoice = SalesInvoice.objects.filter(client_id=client_id, id=invoice_id, deleted_at__isnull=True).first()
                if not invoice:
                    raise NotFound("Sales Invoice not found.")
                if invoice.status == "Cancelled":
                    raise BusinessRuleViolation("Cannot return against a cancelled invoice.", code="PAYMENT_ON_CANCELLED")

                return_reason = (request.data.get("return_reason") or request.data.get("returnReason") or "Fabric Damaged / Defective").strip()
                customer_remarks = (request.data.get("remarks") or request.data.get("customerRemarks") or "").strip()
                doc_date_str = request.data.get("doc_date") or request.data.get("docDate")
                doc_date = timezone.datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else timezone.localdate()

                raw_lines = request.data.get("lines") or []
                if not raw_lines:
                    raise ValidationFailed("At least one line item must be selected for return.", field_errors={"lines": ["Required."]})

                total_requested_qty = ZERO
                validated_lines = []
                for item_d in raw_lines:
                    inv_line_id = item_d.get("invoice_line_id") or item_d.get("invoiceLineId")
                    inv_line = SalesInvoiceLine.objects.filter(id=inv_line_id, sales_invoice=invoice).first()
                    if not inv_line:
                        raise NotFound(f"Invoice line {inv_line_id} not found on invoice {invoice.invoice_number}.")

                    req_qty = round4(D(item_d.get("requested_qty") or item_d.get("qty") or 0))
                    if req_qty <= ZERO:
                        continue

                    # Server-side guard: Cannot return more than remaining invoiced quantity
                    remaining_returnable = round4(D(inv_line.qty) - D(inv_line.returned_qty or 0))
                    if req_qty > remaining_returnable + Decimal("0.0001"):
                        raise BusinessRuleViolation(
                            f"Requested return of {req_qty} M exceeds available returnable balance of {remaining_returnable} M for {inv_line.item_name or 'item'}.",
                            code="OVER_RETURN"
                        )

                    taka_id = item_d.get("lot_taka_id") or item_d.get("lotTakaId")
                    taka_obj = LotTaka.objects.filter(client_id=client_id, id=taka_id).first() if taka_id else None

                    validated_lines.append((inv_line, req_qty, taka_obj))
                    total_requested_qty += req_qty

                if total_requested_qty <= ZERO:
                    raise ValidationFailed("Total requested return quantity must be greater than zero.")

                ret_number = allocate_number(client_id, "SR", doc_date)

                metadata = {
                    "project_code": project.code,
                    "return_stage": "REQUESTED",
                    "requested_qty": float(total_requested_qty),
                    "received_qty": 0.0,
                    "customer_remarks": customer_remarks,
                    "return_reason": return_reason,
                    "credit_note_status": "Pending",
                    "qc_status": "Pending Receipt",
                    "stock_destination": "Pending Receipt",
                }

                sales_return = SalesReturn.objects.create(
                    client_id=client_id,
                    party=invoice.party or project.party,
                    party_name=getattr(invoice.party, "name", None) or project.customer_name or "Customer",
                    party_gstin=getattr(invoice.party, "gstin", None) or getattr(invoice.party, "gst_number", None),
                    sales_invoice=invoice,
                    doc_date=doc_date,
                    status="Draft",  # Stays Draft until physical receipt and authorized processing
                    return_number=ret_number,
                    credit_note_number="",  # Not issued yet
                    reason=return_reason,
                    location=main_wh,
                    notes=json.dumps(metadata),
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                created_lines = []
                for idx, (inv_line, req_qty, taka_obj) in enumerate(validated_lines, start=1):
                    srl = SalesReturnLine.objects.create(
                        client_id=client_id,
                        sales_return=sales_return,
                        sales_invoice_line=inv_line,
                        line_no=idx,
                        item=inv_line.item,
                        sku=inv_line.sku,
                        item_name=inv_line.item_name,
                        uom=inv_line.uom or "Meters",
                        qty=req_qty,
                        rate=inv_line.rate,
                        amount=round2(req_qty * inv_line.rate),
                        tax_pct=inv_line.tax_pct,
                        tax_amount=round2(round2(req_qty * inv_line.rate) * (inv_line.tax_pct / Decimal("100.00"))),
                        line_total=round2(req_qty * inv_line.rate) + round2(round2(req_qty * inv_line.rate) * (inv_line.tax_pct / Decimal("100.00"))),
                        returned_qty=req_qty,
                        condition="Hold",  # All returns default to QC Hold
                        lot_taka=taka_obj,
                    )
                    created_lines.append(srl)

                recalculate_document(sales_return, lines=created_lines)
                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "return_id": str(sales_return.id),
                    "return_number": sales_return.return_number,
                    "requested_qty": float(total_requested_qty),
                    "status": "Return Request Authorized",
                })

            # B. RECEIVE PHYSICAL RETURN (Quarantine Inward into SYS-QC-HOLD)
            elif action_type == "receive_physical_return":
                return_id = request.data.get("return_id") or request.data.get("returnId")
                sales_return = SalesReturn.objects.select_for_update().filter(client_id=client_id, id=return_id, deleted_at__isnull=True).first()
                if not sales_return:
                    raise NotFound("Sales Return record not found.")

                meta = _get_notes_dict(sales_return.notes)
                if meta.get("return_stage") not in ("REQUESTED", None):
                    raise Conflict("Physical receipt has already been recorded for this return.", code="ALREADY_RECEIVED")

                receipt_date_str = request.data.get("receipt_date") or request.data.get("receiptDate")
                receipt_date = timezone.datetime.strptime(receipt_date_str, "%Y-%m-%d").date() if receipt_date_str else timezone.localdate()
                receiving_employee = (request.data.get("receiving_employee") or request.data.get("receivingEmployee") or getattr(request.user, "get_full_name", lambda: "")() or "Store Incharge").strip()
                condition_on_arrival = (request.data.get("condition_on_arrival") or request.data.get("condition") or "Received in Good Packaging").strip()

                loc_id = request.data.get("warehouse_id") or request.data.get("warehouseId")
                receiving_wh = Location.objects.filter(client_id=client_id, id=loc_id).first() if loc_id else main_wh

                lines_payload = request.data.get("lines") or []
                lines_map = {str(item.get("line_id") or item.get("id")): item for item in lines_payload if item.get("line_id") or item.get("id")}

                total_received = ZERO
                for line in sales_return.line_items.select_related("sales_invoice_line", "item", "lot_taka").all():
                    pay_item = lines_map.get(str(line.id), {})
                    rcvd_qty = round4(D(pay_item.get("received_qty") or pay_item.get("actual_meter") or line.returned_qty or 0))
                    if rcvd_qty <= ZERO:
                        rcvd_qty = round4(D(line.returned_qty))

                    line.returned_qty = rcvd_qty
                    line.qty = rcvd_qty
                    line.amount = round2(rcvd_qty * line.rate)
                    line.line_total = round2(rcvd_qty * line.rate) + round2(round2(rcvd_qty * line.rate) * (line.tax_pct / Decimal("100.00")))
                    line.save(update_fields=["returned_qty", "qty", "amount", "line_total", "updated_at"])

                    use_taka = line.lot_taka
                    item_obj = line.item or (line.sales_invoice_line.item if line.sales_invoice_line else None)

                    # Post stock movement into SYS-QC-HOLD (Quarantined stock, not saleable!)
                    stock_svc.post_movement(
                        client_id=client_id,
                        item=item_obj,
                        location=qc_hold_loc,
                        type="SALES_RETURN_HOLD",
                        quantity=rcvd_qty,
                        unit_cost=D(line.rate or 0),
                        reference_type="SalesReturn",
                        reference_id=sales_return.id,
                        reference_number=sales_return.return_number,
                        movement_date=receipt_date,
                        lot_taka=use_taka,
                        notes=f"Customer Return Inward for {project.code} - Quarantined in QC Hold",
                        user=request.user,
                    )

                    if use_taka:
                        use_taka.location = qc_hold_loc
                        use_taka.qc_status = "On Hold"
                        use_taka.status = "QC Hold"
                        use_taka.save(update_fields=["location", "qc_status", "status", "updated_at"])

                    total_received += rcvd_qty

                meta["return_stage"] = "RECEIVED_QC_PENDING"
                meta["received_qty"] = float(total_received)
                meta["receipt_date"] = str(receipt_date)
                meta["receiving_employee"] = receiving_employee
                meta["condition_on_arrival"] = condition_on_arrival
                meta["qc_status"] = "QC Pending"

                sales_return.location = receiving_wh or main_wh
                sales_return.notes = json.dumps(meta)
                sales_return.save(update_fields=["location", "notes", "updated_at"])

                recalculate_document(sales_return)
                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "return_id": str(sales_return.id),
                    "received_qty": float(total_received),
                    "status": "Physically Received — Quarantined in QC Hold",
                })

            # C. MANDATORY QC INSPECTION & DEFECT LOGGING
            elif action_type == "inspect_qc":
                return_id = request.data.get("return_id") or request.data.get("returnId")
                sales_return = SalesReturn.objects.select_for_update().filter(client_id=client_id, id=return_id, deleted_at__isnull=True).first()
                if not sales_return:
                    raise NotFound("Sales Return record not found.")

                meta = _get_notes_dict(sales_return.notes)
                if meta.get("return_stage") not in ("RECEIVED_QC_PENDING", "QC_INSPECTED"):
                    raise BusinessRuleViolation("Physical receipt must be recorded before conducting QC inspection.", code="RECEIPT_REQUIRED")

                inspection_date_str = request.data.get("inspection_date") or request.data.get("inspectionDate")
                inspection_date = timezone.datetime.strptime(inspection_date_str, "%Y-%m-%d").date() if inspection_date_str else timezone.localdate()
                method = (request.data.get("inspection_method") or request.data.get("inspectionMethod") or "ASTM 4-Point Inspection (D5430)").strip()
                inspector_name = (request.data.get("inspector_name") or request.data.get("inspectorName") or getattr(request.user, "get_full_name", lambda: "")() or "QC Inspector").strip()
                shift = (request.data.get("shift") or "General Shift").strip()
                qc_notes = (request.data.get("notes") or request.data.get("remarks") or "").strip()

                raw_qc_lines = request.data.get("lines") or []
                if not raw_qc_lines:
                    raise ValidationFailed("At least one QC inspection line is required.", field_errors={"lines": ["Required."]})

                total_inspected = ZERO
                passed_qty = ZERO
                second_quality_qty = ZERO
                reprocess_qty = ZERO
                rejected_qty = ZERO
                hold_qty = ZERO

                parsed_qc_lines = []
                for ql in raw_qc_lines:
                    act_m = round4(D(ql.get("actual_meter") or ql.get("quantity") or ql.get("qty") or 0))
                    if act_m <= ZERO:
                        continue

                    disp = ql.get("disposition") or "Pass"
                    if disp not in ("Pass", "Hold", "Reprocess", "Reject", "Second Quality"):
                        disp = "Pass"

                    grade = ql.get("grade") or ("A" if disp == "Pass" else "Second" if disp == "Second Quality" else "Reject")
                    taka_num = str(ql.get("taka_number") or ql.get("takaNo") or "1")
                    lot_num = str(ql.get("lot_number") or ql.get("lotNo") or "")
                    line_remarks = ql.get("remarks") or ""
                    taka_id = ql.get("lot_taka_id") or ql.get("lotTakaId")
                    taka_obj = LotTaka.objects.filter(client_id=client_id, id=taka_id).first() if taka_id else None

                    defects = ql.get("defects") or []

                    if disp == "Pass":
                        passed_qty += act_m
                    elif disp == "Second Quality":
                        second_quality_qty += act_m
                    elif disp == "Reprocess":
                        reprocess_qty += act_m
                    elif disp == "Reject":
                        rejected_qty += act_m
                    else:
                        hold_qty += act_m

                    total_inspected += act_m
                    parsed_qc_lines.append({
                        "lot_taka": taka_obj,
                        "taka_number": taka_num,
                        "lot_number": lot_num,
                        "actual_meter": act_m,
                        "disposition": disp,
                        "grade": grade,
                        "remarks": line_remarks,
                        "defects": defects,
                    })

                if total_inspected <= ZERO:
                    raise ValidationFailed("Total inspected meters must be greater than zero.")

                # Reconcile check: sum of inspected categories must equal total inspected
                rec_check = passed_qty + second_quality_qty + reprocess_qty + rejected_qty + hold_qty
                if abs(rec_check - total_inspected) > Decimal("0.001"):
                    raise BusinessRuleViolation("Accepted, reprocessing, hold, second quality and rejected quantities do not reconcile.", code="QC_RECONCILE_FAILED")

                # Overall decision determination
                overall_status = "Approved"
                if rejected_qty == total_inspected:
                    overall_status = "Rejected"
                elif reprocess_qty > ZERO and passed_qty == ZERO:
                    overall_status = "Rework"
                elif hold_qty > ZERO:
                    overall_status = "On Hold"
                elif passed_qty > ZERO and (reprocess_qty > ZERO or rejected_qty > ZERO or second_quality_qty > ZERO):
                    overall_status = "Partially Approved"

                qc_number = allocate_number(client_id, "QC", inspection_date)
                first_item = sales_return.line_items.first().item if sales_return.line_items.exists() else None

                qc = QCInspection.objects.create(
                    client_id=client_id,
                    inspection_number=qc_number,
                    source_document_type="SALES_RETURN",
                    source_document_id=sales_return.id,
                    source_document_number=sales_return.return_number,
                    inspection_date=inspection_date,
                    inspector=request.user if getattr(request.user, "is_authenticated", False) else None,
                    shift=shift,
                    status=overall_status,
                    total_received_qty=D(meta.get("received_qty", total_inspected)),
                    total_inspected_qty=total_inspected,
                    passed_qty=passed_qty,
                    second_quality_qty=second_quality_qty,
                    reprocess_qty=reprocess_qty,
                    rejected_qty=rejected_qty,
                    hold_qty=hold_qty,
                    notes=f"Customer Return QC for {sales_return.return_number} (Project: {project.code}). {qc_notes}".strip(),
                    fabric_item=first_item,
                    inspection_type="Customer Return QC",
                    inspection_method=method,
                )

                for pql in parsed_qc_lines:
                    qcl = QCInspectionLine.objects.create(
                        client_id=client_id,
                        inspection=qc,
                        lot_taka=pql["lot_taka"],
                        taka_number=pql["taka_number"],
                        lot_number=pql["lot_number"],
                        actual_meter=pql["actual_meter"],
                        original_meter=pql["actual_meter"],
                        disposition=pql["disposition"],
                        grade=pql["grade"],
                        remarks=pql["remarks"],
                    )

                    for def_d in pql["defects"]:
                        def_id = def_d.get("defect_id") or def_d.get("defectId")
                        def_obj = DefectMaster.objects.filter(client_id=client_id, id=def_id).first() if def_id else None
                        if def_obj:
                            QCInspectionLineDefect.objects.create(
                                client_id=client_id,
                                inspection_line=qcl,
                                defect=def_obj,
                                defect_count=int(def_d.get("defect_count") or def_d.get("count") or 1),
                                points=int(def_d.get("points") or 1),
                                remarks=def_d.get("remarks") or "",
                            )

                meta["return_stage"] = "QC_INSPECTED"
                meta["qc_status"] = overall_status
                meta["qc_inspection_number"] = qc.inspection_number
                meta["qc_passed_qty"] = float(passed_qty)
                meta["qc_second_quality_qty"] = float(second_quality_qty)
                meta["qc_reprocess_qty"] = float(reprocess_qty)
                meta["qc_rejected_qty"] = float(rejected_qty)
                meta["qc_hold_qty"] = float(hold_qty)
                meta["inspector_name"] = inspector_name

                sales_return.notes = json.dumps(meta)
                sales_return.save(update_fields=["notes", "updated_at"])

                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "inspection_id": str(qc.id),
                    "inspection_number": qc.inspection_number,
                    "overall_status": overall_status,
                    "passed_qty": float(passed_qty),
                    "reprocess_qty": float(reprocess_qty),
                    "rejected_qty": float(rejected_qty),
                    "hold_qty": float(hold_qty),
                })

            # D. AUTHORIZED STOCK DISPOSITION & ALLOCATION ROUTING
            elif action_type == "route_stock":
                return_id = request.data.get("return_id") or request.data.get("returnId")
                sales_return = SalesReturn.objects.select_for_update().filter(client_id=client_id, id=return_id, deleted_at__isnull=True).first()
                if not sales_return:
                    raise NotFound("Sales Return record not found.")

                meta = _get_notes_dict(sales_return.notes)
                if meta.get("return_stage") not in ("QC_INSPECTED", "ROUTED"):
                    raise BusinessRuleViolation("QC inspection must be completed before routing stock.", code="QC_REQUIRED")

                destination = (request.data.get("destination") or "Customer Usable Stock").strip()
                if destination not in ("Customer Usable Stock", "Own Project Buffer", "Reprocess JWO", "QC Hold", "Reject / Scrap"):
                    destination = "Customer Usable Stock"

                remarks = (request.data.get("remarks") or "").strip()

                qc = QCInspection.objects.filter(client_id=client_id, source_document_type="SALES_RETURN", source_document_id=sales_return.id).order_by("-created_at").first()
                if not qc:
                    raise NotFound("Linked QC Inspection record not found.")

                # Process piece-by-piece movements out of SYS-QC-HOLD based on QC disposition
                for ql in qc.lines.select_related("lot_taka", "lot_taka__item").all():
                    lt = ql.lot_taka
                    qty_m = ql.actual_meter
                    if qty_m <= ZERO:
                        continue

                    item_obj = (lt.item if lt else None) or qc.fabric_item or (sales_return.line_items.first().item if sales_return.line_items.exists() else None)
                    unit_cost = D(lt.rate if lt and lt.rate else (sales_return.line_items.first().rate if sales_return.line_items.exists() else 0))

                    if ql.disposition == "Pass":
                        # Move to WH-MAIN
                        stock_svc.post_movement(
                            client_id=client_id,
                            item=item_obj,
                            location=main_wh,
                            type="QC_PASS",
                            quantity=qty_m,
                            unit_cost=unit_cost,
                            reference_type="QCInspection",
                            reference_id=qc.id,
                            reference_number=qc.inspection_number,
                            movement_date=timezone.localdate(),
                            lot_taka=lt,
                            notes=f"Customer Return QC Pass -> {destination}",
                            user=request.user,
                        )
                        if lt:
                            lt.location = main_wh
                            lt.qc_status = "Passed"
                            lt.status = "Available"
                            lt.save(update_fields=["location", "qc_status", "status", "updated_at"])

                            # Allocation transfer: Customer Project vs Own Project
                            if destination == "Own Project Buffer":
                                allocations_svc.set_project_lot_allocation(
                                    project=project,
                                    lot_taka=lt,
                                    quantity=qty_m,
                                    allocation_type="Own Project",
                                    uom=lt.uom or "Meters",
                                )
                            else:
                                allocations_svc.set_project_lot_allocation(
                                    project=project,
                                    lot_taka=lt,
                                    quantity=qty_m,
                                    allocation_type="Customer Project",
                                    uom=lt.uom or "Meters",
                                )

                    elif ql.disposition == "Second Quality":
                        # Move to SYS-SECOND-QUALITY
                        stock_svc.post_movement(
                            client_id=client_id,
                            item=item_obj,
                            location=second_quality_loc,
                            type="QC_SECOND",
                            quantity=qty_m,
                            unit_cost=unit_cost,
                            reference_type="QCInspection",
                            reference_id=qc.id,
                            reference_number=qc.inspection_number,
                            movement_date=timezone.localdate(),
                            lot_taka=lt,
                            notes=f"Customer Return Second Quality -> Stock",
                            user=request.user,
                        )
                        if lt:
                            lt.location = second_quality_loc
                            lt.qc_status = "Passed"
                            lt.status = "Second Quality"
                            lt.save(update_fields=["location", "qc_status", "status", "updated_at"])

                    elif ql.disposition == "Reprocess":
                        # Move to SYS-REPROCESS
                        stock_svc.post_movement(
                            client_id=client_id,
                            item=item_obj,
                            location=reprocess_loc,
                            type="QC_REPROCESS",
                            quantity=qty_m,
                            unit_cost=unit_cost,
                            reference_type="QCInspection",
                            reference_id=qc.id,
                            reference_number=qc.inspection_number,
                            movement_date=timezone.localdate(),
                            lot_taka=lt,
                            notes=f"Customer Return Reprocessing required: {remarks}",
                            user=request.user,
                        )
                        if lt:
                            lt.location = reprocess_loc
                            lt.qc_status = "Reprocess"
                            lt.status = "Reprocess"
                            lt.save(update_fields=["location", "qc_status", "status", "updated_at"])

                        # Link to JWO Reprocess if active JWO exists
                        jwo = JobWorkOrder.objects.filter(client_id=client_id, project=project, deleted_at__isnull=True).first()
                        if jwo:
                            JWOReprocess.objects.create(
                                client_id=client_id,
                                job_work_order=jwo,
                                no=f"REP-{sales_return.return_number}",
                                inward_no=sales_return.return_number,
                                date=timezone.localdate(),
                                qty=qty_m,
                                reason=sales_return.reason or "Customer Return Reprocess",
                                status="Pending",
                                remarks=remarks or f"Rework from Return {sales_return.return_number}",
                            )

                    elif ql.disposition == "Reject":
                        # Move to SYS-REJECT
                        stock_svc.post_movement(
                            client_id=client_id,
                            item=item_obj,
                            location=reject_loc,
                            type="QC_REJECT",
                            quantity=qty_m,
                            unit_cost=unit_cost,
                            reference_type="QCInspection",
                            reference_id=qc.id,
                            reference_number=qc.inspection_number,
                            movement_date=timezone.localdate(),
                            lot_taka=lt,
                            notes=f"Customer Return Rejected/Scrap: {remarks}",
                            user=request.user,
                        )
                        if lt:
                            lt.location = reject_loc
                            lt.qc_status = "Rejected"
                            lt.status = "Reject"
                            lt.save(update_fields=["location", "qc_status", "status", "updated_at"])

                meta["return_stage"] = "ROUTED"
                meta["stock_destination"] = destination
                meta["routing_remarks"] = remarks

                sales_return.notes = json.dumps(meta)
                sales_return.save(update_fields=["notes", "updated_at"])

                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "return_id": str(sales_return.id),
                    "destination": destination,
                    "status": "Stock Routed Successfully",
                })

            # E. ISSUE CREDIT NOTE (Decoupled Financial Adjustment to General Ledger)
            elif action_type == "issue_credit_note":
                return_id = request.data.get("return_id") or request.data.get("returnId")
                sales_return = SalesReturn.objects.select_for_update().filter(client_id=client_id, id=return_id, deleted_at__isnull=True).first()
                if not sales_return:
                    raise NotFound("Sales Return record not found.")

                meta = _get_notes_dict(sales_return.notes)
                if sales_return.status == "Posted" and sales_return.credit_note_number:
                    raise Conflict("Credit note has already been issued for this return.", code="ALREADY_ISSUED")

                credit_date_str = request.data.get("credit_date") or request.data.get("creditDate")
                credit_date = timezone.datetime.strptime(credit_date_str, "%Y-%m-%d").date() if credit_date_str else timezone.localdate()
                approval_notes = (request.data.get("approval_notes") or request.data.get("notes") or "").strip()

                cn_number = allocate_number(client_id, "CN", credit_date)

                sales_return.credit_note_number = cn_number
                sales_return.status = "Posted"
                sales_return.posted_at = timezone.now()
                sales_return.posted_by = request.user if getattr(request.user, "is_authenticated", False) else None
                sales_return.save(update_fields=["credit_note_number", "status", "posted_at", "posted_by", "updated_at"])

                # Post credit note to accounting ledger (Dr Sales Return, Dr GST Output | Cr Debtors)
                entry = ledger.post_sales_return(sales_return, user=request.user)
                if entry is not None:
                    sales_return.journal_entry = entry
                    sales_return.save(update_fields=["journal_entry", "updated_at"])

                # Reverse incentive & broker commission pro-rata
                from apps.pms.production_services import reverse_incentives_for_invoice
                from apps.sales.broker_services import reverse_broker_commission_for_invoice

                if sales_return.sales_invoice:
                    reverse_incentives_for_invoice(
                        sales_return.sales_invoice, user=request.user, reason=f"Credit Note {sales_return.credit_note_number}"
                    )
                    reverse_broker_commission_for_invoice(
                        sales_return.sales_invoice, user=request.user, reason=f"Credit Note {sales_return.credit_note_number}",
                        return_val=sales_return.taxable_value,
                    )

                meta["credit_note_status"] = "Issued"
                meta["credit_note_number"] = cn_number
                meta["credit_note_date"] = str(credit_date)
                meta["approval_notes"] = approval_notes

                sales_return.notes = json.dumps(meta)
                sales_return.save(update_fields=["notes", "updated_at"])

                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "return_id": str(sales_return.id),
                    "credit_note_number": cn_number,
                    "credit_amount": float(sales_return.total),
                    "journal_entry_id": str(entry.id) if entry else None,
                    "status": "Credit Note Issued & Posted to Ledger",
                })

            raise ValidationFailed(f"Unknown action_type: {action_type}")

        # -------------------------------------------------------------
        # 2. GET WORKSPACE DATASET (Dashboard KPIs + Register + Modals)
        # -------------------------------------------------------------
        # Gather all returns for this project
        invoices_for_project = list(SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(Q(sales_order=order) | Q(notes__icontains=project.code)).distinct())
        invoice_ids = [inv.id for inv in invoices_for_project]

        returns_qs = SalesReturn.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(
            Q(sales_invoice__in=invoices_for_project) |
            Q(sales_invoice__sales_order=order) |
            Q(reason__icontains=project.code) |
            Q(notes__icontains=project.code)
        ).select_related("party", "sales_invoice", "sales_invoice__delivery_challan", "location", "journal_entry").prefetch_related("line_items", "line_items__lot_taka").distinct().order_by("-doc_date", "-created_at")

        returns_list = []
        total_requests_count = returns_qs.count()
        pending_receipts_qty = 0.0
        qc_pending_qty = 0.0
        qc_accepted_qty = 0.0
        reprocessing_qty = 0.0
        qc_hold_qty = 0.0
        rejected_qty = 0.0
        credit_notes_total = 0.0
        credit_notes_count = 0

        all_return_ids = [r.id for r in returns_qs]
        qc_inspections = {
            str(qc.source_document_id): qc
            for qc in QCInspection.objects.filter(
                client_id=client_id, source_document_type="SALES_RETURN", source_document_id__in=all_return_ids
            ).order_by("-created_at")
        }

        for ret in returns_qs:
            meta = _get_notes_dict(ret.notes)
            stage = meta.get("return_stage") or ("ROUTED" if ret.status == "Posted" else "REQUESTED")
            req_qty = float(meta.get("requested_qty") or sum(float(l.returned_qty or 0) for l in ret.line_items.all()))
            rcvd_qty = float(meta.get("received_qty") or (req_qty if stage in ("RECEIVED_QC_PENDING", "QC_INSPECTED", "ROUTED") else 0.0))

            qc_obj = qc_inspections.get(str(ret.id))
            if qc_obj:
                qc_accepted_qty += float(qc_obj.passed_qty or 0)
                reprocessing_qty += float(qc_obj.reprocess_qty or 0)
                rejected_qty += float(qc_obj.rejected_qty or 0)
                qc_hold_qty += float(qc_obj.hold_qty or 0)
            elif stage == "RECEIVED_QC_PENDING":
                qc_pending_qty += rcvd_qty

            if stage == "REQUESTED":
                pending_receipts_qty += req_qty

            cn_status = meta.get("credit_note_status") or ("Issued" if ret.credit_note_number else "Pending")
            if ret.credit_note_number or ret.status == "Posted":
                credit_notes_total += float(ret.total or 0)
                credit_notes_count += 1

            ret_lines = []
            for l in ret.line_items.all():
                ret_lines.append({
                    "id": str(l.id),
                    "lineNo": l.line_no,
                    "itemName": l.item_name or (l.item.name if l.item else "Fabric"),
                    "sku": l.sku or "",
                    "uom": l.uom or "Meters",
                    "returnedQty": float(l.returned_qty or 0),
                    "rate": float(l.rate or 0),
                    "amount": float(l.amount or 0),
                    "taxPct": float(l.tax_pct or 0),
                    "lineTotal": float(l.line_total or 0),
                    "condition": l.condition or "Hold",
                    "lotTaka": l.lot_taka.taka_number if l.lot_taka else None,
                    "lotNo": l.lot_taka.lot_number if l.lot_taka else None,
                })

            inv = ret.sales_invoice
            dc = inv.delivery_challan if inv else None
            so = inv.sales_order if inv else order

            # Original delivered qty from invoice
            deliv_qty = float(inv.line_items.aggregate(s=Sum("qty"))["s"] or 0) if inv else 0.0

            returns_list.append({
                "id": str(ret.id),
                "returnNumber": ret.return_number or f"RET-{str(ret.id)[:6]}",
                "creditNoteNumber": ret.credit_note_number or "",
                "creditNoteStatus": cn_status,
                "creditNoteAmount": float(ret.total or 0),
                "docDate": str(ret.doc_date),
                "status": ret.status,
                "returnStage": stage,
                "reason": ret.reason or meta.get("return_reason") or "Fabric Damaged / Defective",
                "customerName": ret.party_name or (ret.party.name if ret.party else project.customer_name),
                "salesOrderNumber": so.order_number if so else "—",
                "salesInvoiceNumber": inv.invoice_number if inv else "—",
                "invoiceId": str(inv.id) if inv else None,
                "deliveryChallanNumber": dc.challan_number if dc else "—",
                "fabricItem": project.product_name or (inv.line_items.first().item_name if inv and inv.line_items.exists() else "Fabric"),
                "fabricQuality": (project.specifications.get("fabric_quality") if isinstance(project.specifications, dict) else None) or project.product_name or "Standard Quality",
                "shade": (project.specifications.get("color_shade") if isinstance(project.specifications, dict) else None) or "Standard",
                "deliveredQty": deliv_qty,
                "requestedQty": req_qty,
                "receivedQty": rcvd_qty,
                "qcStatus": meta.get("qc_status") or (qc_obj.status if qc_obj else "Pending Receipt"),
                "stockDestination": meta.get("stock_destination") or ("Quarantined in QC Hold" if stage == "RECEIVED_QC_PENDING" else "Pending Receipt"),
                "locationName": ret.location.name if ret.location else "Main Warehouse",
                "customerRemarks": meta.get("customer_remarks") or "",
                "lines": ret_lines,
                "qcInspection": {
                    "inspectionNumber": qc_obj.inspection_number,
                    "inspectionDate": str(qc_obj.inspection_date),
                    "status": qc_obj.status,
                    "passedQty": float(qc_obj.passed_qty or 0),
                    "secondQualityQty": float(qc_obj.second_quality_qty or 0),
                    "reprocessQty": float(qc_obj.reprocess_qty or 0),
                    "rejectedQty": float(qc_obj.rejected_qty or 0),
                    "holdQty": float(qc_obj.hold_qty or 0),
                    "notes": qc_obj.notes or "",
                } if qc_obj else None,
            })

        # Available dispatches/invoices for this project to return against
        available_invoices = []
        for inv in invoices_for_project:
            inv_lines = []
            for il in inv.line_items.all():
                invoiced_m = float(il.qty or 0)
                already_ret_m = float(il.returned_qty or 0)
                returnable_m = max(0.0, invoiced_m - already_ret_m)
                inv_lines.append({
                    "id": str(il.id),
                    "itemName": il.item_name or (il.item.name if il.item else "Fabric"),
                    "sku": il.sku or "",
                    "uom": il.uom or "Meters",
                    "invoicedQty": invoiced_m,
                    "alreadyReturnedQty": already_ret_m,
                    "returnableQty": returnable_m,
                    "rate": float(il.rate or 0),
                    "taxPct": float(il.tax_pct or 0),
                })
            available_invoices.append({
                "id": str(inv.id),
                "invoiceNumber": inv.invoice_number,
                "docDate": str(inv.doc_date),
                "total": float(inv.total or 0),
                "challanNumber": inv.delivery_challan.challan_number if inv.delivery_challan else "—",
                "status": inv.status,
                "lines": inv_lines,
            })

        # Defect master items for QC
        defect_catalog = [
            {"id": str(d.id), "code": d.code, "name": d.name, "category": d.category}
            for d in DefectMaster.objects.filter(client_id=client_id, is_active=True).order_by("category", "name")
        ]

        # Standard + Master Return Reasons
        custom_reasons = list(ReturnReasonMaster.objects.filter(client_id=client_id, is_active=True).values_list("name", flat=True))
        standard_reasons = [
            "Colour / Shade Not Approved",
            "Fabric Damaged / Defective",
            "Customer No Longer Requires Fabric",
            "Dimensional Shrinkage / Width Mismatch",
            "Late Delivery / Order Cancelled",
        ]
        return_reasons = list(dict.fromkeys(standard_reasons + custom_reasons))

        locations = [
            {"id": str(loc.id), "name": loc.name, "code": loc.code}
            for loc in Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").order_by("name")
        ]

        return Response({
            "project": {
                "id": str(project.id),
                "code": project.code,
                "productName": project.product_name or "Finished Fabric",
                "customerName": project.customer_name or (project.party.name if project.party else "Customer"),
                "uom": "Meters",
            },
            "summary": {
                "totalReturnRequests": total_requests_count,
                "pendingReturnReceipts": pending_receipts_qty,
                "qcPendingQty": qc_pending_qty,
                "qcAcceptedQty": qc_accepted_qty,
                "reprocessingQty": reprocessing_qty,
                "qcHoldQty": qc_hold_qty,
                "rejectedQty": rejected_qty,
                "creditNotesIssued": credit_notes_total,
                "creditNotesCount": credit_notes_count,
                "uom": "Meters",
            },
            "returns": returns_list,
            "availableInvoices": available_invoices,
            "returnReasons": return_reasons,
            "defectMaster": defect_catalog,
            "locations": locations,
        })

    def _create_real_purchase_order(self, project, data, user=None):
        from apps.purchase.models import PurchaseOrder, PurchaseOrderLine
        from apps.masters.models import Party, Item
        from apps.sales.services import recalculate_document
        from apps.pms.pipeline_services import get_project_cfv, sync_project_pipeline_stages
        import json

        vendor_id = data.get("vendorId") or data.get("supplierId")
        supplier_name = data.get("supplierName")
        quantity = Decimal(str(data.get("quantity") or data.get("qty") or 0))
        rate = Decimal(str(data.get("ratePerMeter") or data.get("rate") or 0))
        expected_date = data.get("deliveryDate") or data.get("expectedDate") or None
        notes = data.get("notes") or f"Grey Purchase PO for Project {project.code}"
        item_id = data.get("itemId")
        fabric_quality = data.get("fabricQuality")

        vendor = None
        if vendor_id:
            try:
                vendor = Party.objects.filter(client_id=project.client_id, pk=vendor_id, deleted_at__isnull=True).first()
            except Exception:
                vendor = None
        if not vendor and supplier_name:
            vendor = Party.objects.filter(client_id=project.client_id, name__iexact=supplier_name.strip(), deleted_at__isnull=True).first()
        if not vendor:
            vendor = Party.objects.filter(client_id=project.client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True).first()
        if not vendor:
            vendor = Party.objects.filter(client_id=project.client_id, deleted_at__isnull=True).first()

        if not vendor:
            raise ValidationFailed("No vendor available in the system.", field_errors={"vendorId": ["No vendor found."]})

        if quantity <= Decimal("0"):
            raise ValidationFailed("Order quantity must be greater than zero.", field_errors={"quantity": ["Invalid quantity."]})

        fabric_item = None
        if item_id:
            try:
                fabric_item = Item.objects.filter(client_id=project.client_id, pk=item_id, deleted_at__isnull=True).first()
            except Exception:
                fabric_item = None
        if not fabric_item and fabric_quality:
            fabric_item = Item.objects.filter(client_id=project.client_id, name__icontains=fabric_quality, deleted_at__isnull=True).first()
        if not fabric_item and project.product_name:
            fabric_item = Item.objects.filter(client_id=project.client_id, name__icontains=project.product_name, deleted_at__isnull=True).first()
        if not fabric_item:
            fabric_item = Item.objects.filter(client_id=project.client_id, item_kind="Fabric", deleted_at__isnull=True).first()
        if not fabric_item:
            fabric_item = Item.objects.filter(client_id=project.client_id, deleted_at__isnull=True).first()

        po_no = allocate_number(project.client, "PO", timezone.localdate())
        po = PurchaseOrder(
            client=project.client,
            po_number=po_no,
            party=vendor,
            doc_date=timezone.localdate(),
            expected_date=expected_date,
            status="Approved",
            reference_number=project.code,
            purchase_type="Grey Stock Purchase",
            project=project,
            notes=notes,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        po.freeze_party_snapshot()
        po.save()

        if fabric_item:
            tax_pct = fabric_item.tax_pct if fabric_item.tax_pct is not None else Decimal("5.00")
            line = PurchaseOrderLine(
                client=project.client,
                purchase_order=po,
                line_no=1,
                item=fabric_item,
                qty=quantity,
                rate=rate,
                tax_pct=tax_pct,
                uom=fabric_item.uom or "Meters",
            )
            line.freeze_item_snapshot()
            line.save()
            recalculate_document(po)

        cfv = get_project_cfv(project)
        cfv["shortage_action"] = "PURCHASE_PO"
        linked_ids = cfv.get("linked_purchase_order_ids", [])
        if str(po.id) not in linked_ids:
            linked_ids.append(str(po.id))
        cfv["linked_purchase_order_ids"] = linked_ids
        cfv["grey_stock_po"] = {
            "id": str(po.id),
            "orderNumber": po.po_number,
            "supplierName": vendor.name,
            "vendorId": str(vendor.id),
            "quantity": float(quantity),
            "rate": float(rate),
            "total": float(getattr(po, "total", None) or (quantity * rate)),
            "status": po.status,
            "date": str(po.doc_date),
            "expectedDate": str(po.expected_date) if po.expected_date else None,
        }
        project.specifications = json.dumps(cfv)
        project.save(update_fields=["specifications", "updated_at"])

        sync_project_pipeline_stages(project)

        stage2 = project.stages.filter(Q(sequence=2) | Q(name="Create Grey Purchase PO")).first()
        if stage2:
            stage2.status = "Completed"
            stage2.completion_pct = 100
            stage2.save(update_fields=["status", "completion_pct", "updated_at"])

        return po, vendor, fabric_item

    @action(detail=True, methods=["get"], url_path="production-lifecycle")
    def production_lifecycle(self, request, pk=None):
        """Each production run of this project: Purchase Order (Vendor A
        Production), then the requirement PDF section 05 stages."""
        from apps.pms.lifecycle_services import project_flow_lifecycles

        project = self.get_object()
        return Response({"projectId": str(project.id), "lifecycles": project_flow_lifecycles(project)})

    @action(detail=True, methods=["get", "post"], url_path="purchase-orders")
    @transaction.atomic
    def purchase_orders(self, request, pk=None):
        project = self.get_object()
        from apps.purchase.models import PurchaseOrder
        from apps.pms.pipeline_services import get_project_cfv
        from apps.masters.models import Party, Item

        if request.method == "POST":
            po, vendor, fabric_item = self._create_real_purchase_order(project, request.data, request.user)
            fl = po.line_items.first()
            po_total = float(getattr(po, "total", None) or ((fl.qty * fl.rate) if fl else 0.0))
            po_data = {
                "id": str(po.id),
                "poNumber": po.po_number,
                "vendorName": vendor.name if vendor else "Supplier",
                "vendorId": str(vendor.id) if vendor else None,
                "quantity": float(fl.qty) if fl else 0.0,
                "rate": float(fl.rate) if fl else 0.0,
                "grandTotal": po_total,
                "total": po_total,
                "status": po.status,
                "docDate": str(po.doc_date),
                "expectedDate": str(po.expected_date) if po.expected_date else None,
                "itemName": fabric_item.name if fabric_item else "Grey Fabric",
                "notes": po.notes or "",
                "message": f"Purchase Order {po.po_number} successfully created for {vendor.name if vendor else 'Supplier'}.",
            }
            return Response({
                "success": True,
                "data": po_data,
                **po_data,
            })

        cfv = get_project_cfv(project)
        linked_ids = cfv.get("linked_purchase_order_ids", [])
        real_pos = PurchaseOrder.objects.filter(
            client_id=project.client_id,
            deleted_at__isnull=True,
        ).filter(
            Q(project=project) |
            Q(reference_number=project.code) |
            Q(id__in=linked_ids) |
            Q(notes__icontains=project.code)
        ).exclude(
            # Final Job Work POs carry the project for the production lifecycle;
            # the purchase-branch stage workspaces read this list and never had them.
            purchase_type="Final Job Work"
        ).select_related("party").prefetch_related("line_items__item").order_by("-created_at")

        data = []
        for rpo in real_pos:
            fl = rpo.line_items.first()
            rpo_total = float(getattr(rpo, "total", None) or ((fl.qty * fl.rate) if fl else 0.0))
            po_lines = [
                {
                    "id": str(l.id),
                    "itemId": str(l.item_id) if l.item_id else None,
                    "itemName": l.item_name or (l.item.name if l.item else "Grey Fabric"),
                    "qty": float(l.qty),
                    "rate": float(l.rate),
                    "uom": l.uom or "Mtr",
                }
                for l in rpo.line_items.all()
            ]
            data.append({
                "id": str(rpo.id),
                "poNumber": rpo.po_number or f"PO-{rpo.id}",
                "docDate": str(rpo.doc_date),
                "expectedDate": str(rpo.expected_date) if rpo.expected_date else None,
                "vendorId": str(rpo.party_id) if rpo.party else None,
                "vendorName": rpo.party_name or (rpo.party.name if rpo.party else "—"),
                "vendor": rpo.party_name or (rpo.party.name if rpo.party else "—"),
                "status": rpo.status,
                "itemId": str(fl.item_id) if (fl and fl.item_id) else None,
                "itemName": fl.item.name if (fl and fl.item) else "Grey Fabric",
                "qty": float(fl.qty) if fl else 0.0,
                "quantity": float(fl.qty) if fl else 0.0,
                "rate": float(fl.rate) if fl else 0.0,
                "grandTotal": rpo_total,
                "total": rpo_total,
                "notes": rpo.notes or "",
                "items": po_lines,
                "lineItems": po_lines,
            })

        # Fetch active vendors from real Party table
        vendors_qs = Party.objects.filter(
            client_id=project.client_id,
            deleted_at__isnull=True,
        ).filter(Q(type__in=["Vendor", "Both", "Processor"]) | Q(is_processor=True)).order_by("name")
        if not vendors_qs.exists():
            vendors_qs = Party.objects.filter(
                client_id=project.client_id,
                deleted_at__isnull=True,
            ).order_by("name")

        # Fetch active fabric items from real Item table
        items_qs = Item.objects.filter(
            client_id=project.client_id,
            deleted_at__isnull=True,
        ).order_by("name")

        available_vendors = [
            {"id": str(v.id), "name": v.name, "code": v.code or "", "type": v.type}
            for v in vendors_qs
        ]
        available_items = [
            {
                "id": str(i.id),
                "name": i.name,
                "sku": i.sku or "",
                "itemKind": i.item_kind or "Fabric",
                "costPrice": float(i.cost_price or 0),
            }
            for i in items_qs
        ]

        env = envelope(data)
        env["orders"] = data
        env["availableVendors"] = available_vendors
        env["availableItems"] = available_items
        return Response(env)

    @action(detail=True, methods=["get", "post"], url_path="grey-stock-allocation")
    @transaction.atomic
    def grey_stock_allocation(self, request, pk=None):
        """Stage 1 workspace: real grey stock allocation for this project.

        ``allocations`` carries the desired metres per lot; each change goes
        through the allocation service (row lock, QC-passed stock only, never
        beyond unallocated stock) instead of being stored as JSON.
        """
        project = self.get_object()
        from apps.core.permissions import require_permission
        from apps.inventory import allocation_services as allocations_svc
        from apps.pms.pipeline_services import evaluate_stock_and_route, sync_project_pipeline_stages, get_project_cfv
        import json

        if request.method == "POST":
            cfv = get_project_cfv(project)
            if "allocations" in request.data:
                require_permission(request.user, "create_transfer")
                for row in request.data.get("allocations") or []:
                    lot_id = row.get("lotTakaId") or row.get("id")
                    if not lot_id:
                        continue
                    allocations_svc.set_project_lot_allocation(
                        client_id=project.client_id,
                        project=project,
                        lot_taka_id=lot_id,
                        target_qty=row.get("allocateQty") or 0,
                        allocation_type=row.get("allocationType") or "Customer Project",
                        user=request.user,
                        remarks=row.get("remarks"),
                    )
                # Superseded by the allocation table; dropped so it cannot drift.
                cfv.pop("grey_stock_allocations", None)
                cfv.pop("grey_stock_allocated_meters", None)
            if "subStep" in request.data:
                cfv["grey_stock_sub_step"] = request.data["subStep"]
            if "shortageAction" in request.data:
                cfv["shortage_action"] = request.data["shortageAction"]
            if "createPurchaseOrder" in request.data or ("purchaseOrder" in request.data and isinstance(request.data["purchaseOrder"], dict) and (request.data["purchaseOrder"].get("vendorId") or request.data["purchaseOrder"].get("ratePerMeter") or request.data["purchaseOrder"].get("quantity"))):
                project.specifications = json.dumps(cfv)
                project.save(update_fields=["specifications", "updated_at"])
                po_data = request.data.get("createPurchaseOrder") or request.data["purchaseOrder"]
                self._create_real_purchase_order(project, po_data, request.user)
                cfv = get_project_cfv(project)
            elif "purchaseOrder" in request.data:
                cfv["grey_stock_po"] = request.data["purchaseOrder"]
            if "productionOrder" in request.data:
                cfv["grey_stock_production_order"] = request.data["productionOrder"]

            project.specifications = json.dumps(cfv)
            project.save(update_fields=["specifications", "updated_at"])
            sync_project_pipeline_stages(project)

        stock_eval = evaluate_stock_and_route(project)
        req_qty = float(stock_eval.get("required_meters") or project.requirement_meters or project.quantity or 0)
        cfv = get_project_cfv(project)

        active = allocations_svc.project_allocations(project.client_id, project)
        mine_by_lot = {}
        for allocation in active:
            mine_by_lot[allocation.lot_taka_id] = mine_by_lot.get(allocation.lot_taka_id, Decimal("0")) + allocation.quantity

        fabrics_data, lots_data = allocations_svc.fabric_stock_summary(
            project.client_id, project=project
        )

        total_allocated = float(sum(mine_by_lot.values(), Decimal("0")))
        customer_allocated = float(sum((a.quantity for a in active if getattr(a, "allocation_type", "Customer Project") == "Customer Project"), Decimal("0")))
        own_allocated = float(sum((a.quantity for a in active if getattr(a, "allocation_type", None) == "Own Project"), Decimal("0")))
        shortage = max(0.0, req_qty - total_allocated)
        alloc_pct = round((total_allocated / req_qty) * 100) if req_qty > 0 else 0
        shortage_pct = round((shortage / req_qty) * 100) if req_qty > 0 else 0

        stage1 = project.stages.filter(sequence=1).first()
        if request.method == "POST" and stage1 and req_qty > 0:
            if total_allocated >= req_qty:
                stage1.status = "Completed"
                stage1.completion_pct = 100
            elif total_allocated > 0:
                stage1.status = "In Progress"
                stage1.completion_pct = min(99, int(round((total_allocated / req_qty) * 100)))
            stage1.save(update_fields=["status", "completion_pct", "updated_at"])

        # Activity comes from what actually happened -- allocation rows and
        # linked POs -- never from canned timestamps.
        stage_logs = []
        for allocation in allocations_svc.project_allocations(project.client_id, project, include_released=True):
            actor = allocation.created_by
            stage_logs.append({
                "timestamp": timezone.localtime(allocation.created_at).strftime("%d/%m/%Y %I:%M %p"),
                "user": (getattr(actor, "name", None) or getattr(actor, "email", None) or "System") if actor else "System",
                "action": f"{allocation.quantity:,.2f} {allocation.uom or ''} allocated from {allocation.lot_taka.lot_number}/{allocation.lot_taka.taka_number}.",
                "status": "Success",
            })
            if allocation.released_at:
                releaser = allocation.released_by
                stage_logs.append({
                    "timestamp": timezone.localtime(allocation.released_at).strftime("%d/%m/%Y %I:%M %p"),
                    "user": (getattr(releaser, "name", None) or getattr(releaser, "email", None) or "System") if releaser else "System",
                    "action": f"Allocation of {allocation.quantity:,.2f} {allocation.uom or ''} from {allocation.lot_taka.lot_number}/{allocation.lot_taka.taka_number} released.",
                    "status": "Info",
                })

        from apps.purchase.models import PurchaseOrder
        linked_ids = cfv.get("linked_purchase_order_ids", [])
        real_pos = PurchaseOrder.objects.filter(
            client_id=project.client_id,
            deleted_at__isnull=True,
        ).filter(
            Q(project=project) |
            Q(reference_number=project.code) |
            Q(id__in=linked_ids)
        ).select_related("party").prefetch_related("line_items__item").order_by("-created_at")

        linked_pos_list = []
        for rpo in real_pos:
            fl = rpo.line_items.first()
            rpo_total = float(getattr(rpo, "total", None) or ((fl.qty * fl.rate) if fl else 0.0))
            linked_pos_list.append({
                "id": str(rpo.id),
                "poNumber": rpo.po_number or f"PO-{rpo.id}",
                "docDate": str(rpo.doc_date),
                "expectedDate": str(rpo.expected_date) if rpo.expected_date else None,
                "vendorId": str(rpo.party_id) if rpo.party else None,
                "vendorName": rpo.party_name or (rpo.party.name if rpo.party else "—"),
                "status": rpo.status,
                "purchaseType": rpo.purchase_type,
                "itemName": fl.item.name if (fl and fl.item) else None,
                "qty": float(fl.qty) if fl else 0.0,
                "rate": float(fl.rate) if fl else 0.0,
                "grandTotal": rpo_total,
                "total": rpo_total,
                "notes": rpo.notes or "",
            })
            stage_logs.append({
                "timestamp": timezone.localtime(rpo.created_at).strftime("%d/%m/%Y %I:%M %p"),
                "user": (getattr(rpo.created_by, "name", None) or "System") if rpo.created_by_id else "System",
                "action": f"{rpo.purchase_type} PO {rpo.po_number} raised on {rpo.party_name or 'supplier'} ({rpo.status}).",
                "status": "Info",
            })
        stage_logs.sort(key=lambda entry: datetime.strptime(entry["timestamp"], "%d/%m/%Y %I:%M %p"))

        def _fmt(value):
            return value.strftime("%d/%m/%Y") if value else None

        res_payload = {
            "projectId": str(project.id),
            "projectCode": project.code,
            "stageId": str(stage1.id) if stage1 else None,
            "stageName": "Stage 1: Grey Stock Check & Allocation",
            "stageSubtitle": "Check available grey fabric stock in OSCAR and allocate for this project.",
            "stageWeight": 10,
            "plannedDurationDays": 5,
            "startDate": _fmt(stage1.start_datetime if stage1 and stage1.start_datetime else project.start_date),
            "expectedCompletionDate": _fmt(stage1.expected_completion_datetime if stage1 and stage1.expected_completion_datetime else project.expected_completion_date),
            "requiredMeters": req_qty,
            "allocatedMeters": total_allocated,
            "customerAllocatedMeters": customer_allocated,
            "ownAllocatedMeters": own_allocated,
            "shortageMeters": shortage,
            "allocatedPct": alloc_pct,
            "shortagePct": shortage_pct,
            "subStep": cfv.get("grey_stock_sub_step", 1),
            "shortageAction": cfv.get("shortage_action", None),
            "purchaseOrder": cfv.get("grey_stock_po", None),
            "linkedPurchaseOrders": linked_pos_list,
            "productionOrder": cfv.get("grey_stock_production_order", None),
            "stageLogs": stage_logs,
            "fabrics": fabrics_data,
            "lots": lots_data,
            "allocations": [allocations_svc.serialize_allocation(a) for a in active],
        }
        return Response(res_payload)

    @action(detail=True, methods=["post"], url_path=r"grey-stock-allocation/(?P<allocation_id>[^/.]+)/release")
    @transaction.atomic
    def release_grey_stock_allocation(self, request, pk=None, allocation_id=None):
        project = self.get_object()
        from apps.core.permissions import require_permission
        from apps.inventory import allocation_services as allocations_svc
        from apps.inventory.models import GreyStockAllocation
        import uuid

        require_permission(request.user, "create_transfer")
        try:
            uuid.UUID(str(allocation_id))
        except ValueError:
            raise NotFound("That allocation does not exist.")
        if not GreyStockAllocation.objects.filter(
            client_id=project.client_id, project=project, pk=allocation_id
        ).exists():
            raise NotFound("That allocation does not belong to this project.")
        allocation = allocations_svc.release_allocation(
            client_id=project.client_id,
            allocation_id=allocation_id,
            user=request.user,
            remarks=request.data.get("remarks"),
        )
        return Response(allocations_svc.serialize_allocation(allocation))

    @action(detail=True, methods=["get"], url_path="customer-project-details")
    def customer_project_details(self, request, pk=None):
        """Part 2: Full Customer Project workspace records & traceability."""
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q
        from apps.core.money import ZERO, D, round4, round2
        from apps.inventory.models import GreyStockAllocation, LotTaka, QCInspection
        from apps.jobwork.models import ProcessPlan, JobWorkOrder, JWOOutward, JWOInward
        from apps.sales.models import DeliveryChallan, SalesInvoice

        req_qty = D(str(project.requirement_meters or project.quantity or 0))
        spec = project.specifications or {}
        if isinstance(spec, str) and spec.strip().startswith("{"):
            try:
                import json
                spec = json.loads(spec)
            except Exception:
                spec = {}

        # 1. Customer Allocations
        alloc_qs = GreyStockAllocation.objects.filter(
            client_id=client_id, project=project, status=GreyStockAllocation.STATUS_ACTIVE, deleted_at__isnull=True
        ).select_related("lot_taka", "lot_taka__item", "lot_taka__location", "lot_taka__fabric_quality")

        cust_allocs = [a for a in alloc_qs if getattr(a, "allocation_type", None) != GreyStockAllocation.ALLOCATION_OWN]
        cust_alloc_qty = sum((D(str(a.quantity or 0)) for a in cust_allocs), ZERO)

        # 2. Process Plans & JWOs
        plans_qs = ProcessPlan.objects.filter(
            client_id=client_id, project=project, deleted_at__isnull=True
        ).exclude(status="Cancelled")
        cust_plans = [p for p in plans_qs if getattr(p, "project_destination", None) != "Own Project"]
        planned_qty = sum((D(str(p.expected_qty or p.planned_qty or 0)) for p in cust_plans), ZERO)

        jwos_qs = JobWorkOrder.objects.filter(
            client_id=client_id, project=project, deleted_at__isnull=True
        ).exclude(status="Cancelled").prefetch_related("outwards", "inwards", "charges")
        cust_jwos = [j for j in jwos_qs if getattr(j, "project_destination", None) != "Own Project"]

        outward_qty = ZERO
        inward_qty = ZERO
        processing_rows = []
        for jwo in (cust_jwos or jwos_qs):
            jwo_out = sum((D(str(getattr(o, "qty", 0) or getattr(o, "meter", 0) or 0)) for o in jwo.outwards.filter(deleted_at__isnull=True)), ZERO)
            jwo_in = sum((D(str(getattr(i, "qty", 0) or getattr(i, "meter", 0) or 0)) for i in jwo.inwards.filter(deleted_at__isnull=True)), ZERO)
            outward_qty += jwo_out
            inward_qty += jwo_in
            pending = max(ZERO, (D(str(jwo.planned_qty or 0)) or jwo_out) - jwo_in)
            processing_rows.append({
                "jwoNo": jwo.jwo_no,
                "process": jwo.process,
                "vendorName": jwo.vendor_name or (jwo.vendor.name if jwo.vendor else "External Processor"),
                "plannedQty": float(jwo.planned_qty or 0),
                "outwardQty": float(jwo_out),
                "inwardQty": float(jwo_in),
                "pendingQty": float(pending),
                "rate": float(jwo.rate or 0),
                "expectedLoss": float(jwo.expected_loss_pct or 0),
                "expectedReturnDate": str(jwo.expected_completion) if jwo.expected_completion else None,
                "status": jwo.status,
            })

        # 3. QC inspections for project
        qc_qs = QCInspection.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(
            Q(source_document_number__in=[j.jwo_no for j in jwos_qs])
            | Q(notes__icontains=project.code)
            | Q(source_document_number=project.code)
        )
        qc_passed_qty = sum((D(str(q.passed_qty or 0)) for q in qc_qs if q.status in ("Approved", "Completed", "Passed")), ZERO)
        qc_rejected_qty = sum((D(str(q.rejected_qty or 0)) for q in qc_qs), ZERO)
        qc_reprocess_qty = sum((D(str(q.reprocess_qty or 0)) for q in qc_qs), ZERO)

        # 4. Dispatches
        dc_qs = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled")
        if project.sales_order_id:
            dc_qs = dc_qs.filter(Q(sales_order=project.sales_order) | Q(notes__icontains=project.code))
        else:
            dc_qs = dc_qs.filter(notes__icontains=project.code)
        dispatched_qty = sum((D(str(getattr(d, "total_quantity", 0) or 0)) for d in dc_qs), ZERO)
        dispatch_rows = []
        for dc in dc_qs:
            dispatch_rows.append({
                "id": str(dc.id),
                "challanNo": dc.challan_number,
                "date": str(getattr(dc, "dispatch_date", None) or getattr(dc, "doc_date", None) or getattr(dc, "created_at", "")),
                "quantity": float(getattr(dc, "total_quantity", 0) or getattr(dc, "total", 0) or 0),
                "transporter": getattr(dc, "transporter", ""),
                "lrNumber": getattr(dc, "lr_number", ""),
                "destination": getattr(dc, "shipping_address", "") or getattr(dc, "delivery_location", "") or project.customer_name,
                "status": dc.status,
            })

        # 5. Invoices & Payment
        inv_qs = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled")
        if project.sales_order_id:
            inv_qs = inv_qs.filter(Q(sales_order=project.sales_order) | Q(notes__icontains=project.code))
        else:
            inv_qs = inv_qs.filter(notes__icontains=project.code)
        invoiced_amount = sum((D(str(inv.total_amount or 0)) for inv in inv_qs), ZERO)
        paid_amount = sum((D(str(getattr(inv, "amount_paid", 0) or 0)) for inv in inv_qs), ZERO)
        remaining_req = max(ZERO, req_qty - dispatched_qty)

        fabric_allocations = []
        for a in cust_allocs:
            lot = a.lot_taka
            fabric_allocations.append({
                "id": str(a.id),
                "lotTakaId": str(lot.id),
                "lotNumber": lot.lot_number,
                "takaNumber": lot.taka_number,
                "fabricItem": getattr(lot.item, "name", "Fabric"),
                "fabricQuality": (getattr(lot.fabric_quality, "name", None) if lot.fabric_quality else None) or (getattr(lot.item, "fabric_quality", None) if lot.item else None) or "Standard",
                "sourcePo": lot.source_document_number or getattr(lot, "source_po_number", "-"),
                "allocatedQty": float(a.quantity),
                "availableQty": float(lot.available_quantity or 0),
                "location": lot.location.name if lot.location else "Warehouse",
                "status": a.status,
            })

        return Response({
            "summary": {
                "customerRequirement": float(req_qty),
                "customerAllocatedFabric": float(cust_alloc_qty),
                "processingPlanned": float(planned_qty),
                "sentForProcessing": float(outward_qty),
                "processingInward": float(inward_qty),
                "qcPassedQuantity": float(qc_passed_qty),
                "dispatchedQuantity": float(dispatched_qty),
                "remainingRequirement": float(remaining_req),
            },
            "customerOrderDetails": {
                "customerName": project.customer_name or "Direct Customer",
                "salesOrderNumber": getattr(project.sales_order, "order_number", None) or spec.get("sales_order_number") or project.code,
                "fabricQuality": spec.get("fabric_quality") or getattr(project, "product_name", "Fabric"),
                "design": spec.get("design_no") or getattr(project, "design_no", "Standard"),
                "shade": spec.get("color_shade") or getattr(project, "shade", "Standard"),
                "width": spec.get("width") or "44 inches",
                "requiredQuantity": float(req_qty),
                "deliveryDate": str(project.expected_completion_date) if project.expected_completion_date else None,
                "orderStatus": project.status,
            },
            "fabricAllocations": fabric_allocations,
            "processingTracking": processing_rows,
            "qcAndFulfilment": {
                "qcPassedQuantity": float(qc_passed_qty),
                "rejectedQuantity": float(qc_rejected_qty),
                "reprocessQuantity": float(qc_reprocess_qty),
                "dispatchedQuantity": float(dispatched_qty),
                "dispatches": dispatch_rows,
                "invoicedAmount": float(invoiced_amount),
                "paidAmount": float(paid_amount),
                "paymentStatus": "Paid" if (paid_amount >= invoiced_amount and invoiced_amount > ZERO) else ("Partially Paid" if paid_amount > ZERO else "Unpaid"),
                "customerFulfilmentBalance": float(remaining_req),
            }
        })

    @action(detail=True, methods=["get"], url_path="own-project-details")
    def own_project_details(self, request, pk=None):
        """Part 3: Full Own Project buffer stock workspace records."""
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q
        from apps.core.money import ZERO, D, round4, round2
        from apps.inventory.models import GreyStockAllocation, LotTaka, QCInspection
        from apps.jobwork.models import ProcessPlan, JobWorkOrder

        alloc_qs = GreyStockAllocation.objects.filter(
            client_id=client_id, project=project, status=GreyStockAllocation.STATUS_ACTIVE, deleted_at__isnull=True
        ).select_related("lot_taka", "lot_taka__item", "lot_taka__location", "lot_taka__fabric_quality")

        own_allocs = [a for a in alloc_qs if getattr(a, "allocation_type", None) == GreyStockAllocation.ALLOCATION_OWN]
        extra_allocated_qty = sum((D(str(a.quantity or 0)) for a in own_allocs), ZERO)

        plans_qs = ProcessPlan.objects.filter(
            client_id=client_id, project=project, deleted_at__isnull=True
        ).exclude(status="Cancelled")
        own_plans = [p for p in plans_qs if getattr(p, "project_destination", None) == "Own Project"]
        planned_qty = sum((D(str(p.expected_qty or p.planned_qty or 0)) for p in own_plans), ZERO)

        jwos_qs = JobWorkOrder.objects.filter(
            client_id=client_id, project=project, deleted_at__isnull=True
        ).exclude(status="Cancelled").prefetch_related("outwards", "inwards")
        own_jwos = [j for j in jwos_qs if getattr(j, "project_destination", None) == "Own Project"]

        outward_qty = ZERO
        inward_qty = ZERO
        processing_plans = []
        for jwo in own_jwos:
            jwo_out = sum((D(str(getattr(o, "qty", 0) or getattr(o, "meter", 0) or 0)) for o in jwo.outwards.filter(deleted_at__isnull=True)), ZERO)
            jwo_in = sum((D(str(getattr(i, "qty", 0) or getattr(i, "meter", 0) or 0)) for i in jwo.inwards.filter(deleted_at__isnull=True)), ZERO)
            outward_qty += jwo_out
            inward_qty += jwo_in
            processing_plans.append({
                "jwoNo": jwo.jwo_no,
                "process": jwo.process,
                "vendorName": jwo.vendor_name or (jwo.vendor.name if jwo.vendor else "External Processor"),
                "plannedQty": float(jwo.planned_qty or 0),
                "outwardQty": float(jwo_out),
                "inwardQty": float(jwo_in),
                "rate": float(jwo.rate or 0),
                "expectedLoss": float(jwo.expected_loss_pct or 0),
                "expectedReturnDate": str(jwo.expected_completion) if jwo.expected_completion else None,
                "status": jwo.status,
            })

        own_stock_details = []
        total_valuation = ZERO
        for a in own_allocs:
            lot = a.lot_taka
            rate = D(str(getattr(lot, "purchase_rate", 0) or (lot.item.standard_cost if lot.item else 0) or 100))
            val = D(str(a.quantity)) * rate
            total_valuation += val
            own_stock_details.append({
                "id": str(a.id),
                "lotTakaId": str(lot.id),
                "lotNumber": lot.lot_number,
                "takaNumber": lot.taka_number,
                "fabricItem": getattr(lot.item, "name", "Fabric"),
                "fabricQuality": (getattr(lot.fabric_quality, "name", None) if lot.fabric_quality else None) or (getattr(lot.item, "fabric_quality", None) if lot.item else None) or "Standard",
                "sourcePo": lot.source_document_number or "-",
                "allocatedQty": float(a.quantity),
                "availableQty": float(lot.available_quantity or 0),
                "location": lot.location.name if lot.location else "Warehouse",
                "status": lot.status,
                "valuation": float(val),
            })

        processing_loss = max(ZERO, outward_qty - inward_qty)
        available_own_stock = extra_allocated_qty if outward_qty == ZERO else inward_qty

        return Response({
            "summary": {
                "extraFabricAllocated": float(extra_allocated_qty),
                "processingPlanned": float(planned_qty),
                "sentForProcessing": float(outward_qty),
                "processingInward": float(inward_qty),
                "qcPassedQuantity": float(inward_qty),
                "processingLoss": float(processing_loss),
                "availableOwnStock": float(available_own_stock),
            },
            "ownStockDetails": own_stock_details,
            "ownProcessingPlans": processing_plans,
            "ownInventoryTracking": {
                "inwardQuantity": float(inward_qty),
                "qcPassedQuantity": float(inward_qty),
                "rejectedQuantity": 0.0,
                "reprocessingQuantity": 0.0,
                "actualProcessingLoss": float(processing_loss),
                "availableForFutureSales": float(available_own_stock),
                "inventoryValuation": float(total_valuation),
            }
        })

    @action(detail=True, methods=["get", "post"], url_path="closure-audit")
    @transaction.atomic
    def closure_audit(self, request, pk=None):
        """Part 12: Stage 18 Project Closure & Reconciliation Audit."""
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q
        from apps.core.money import ZERO, D, round4, round2
        from apps.inventory.models import GreyStockAllocation, QCInspection
        from apps.jobwork.models import JobWorkOrder
        from apps.sales.models import DeliveryChallan, SalesInvoice
        from apps.core.exceptions import Conflict, ValidationFailed

        req_qty = D(str(project.requirement_meters or project.quantity or 0))

        # Check dispatches
        dcs = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled")
        if project.sales_order_id:
            dcs = dcs.filter(Q(sales_order=project.sales_order) | Q(notes__icontains=project.code))
        else:
            dcs = dcs.filter(notes__icontains=project.code)
        dispatched_qty = sum((D(str(getattr(d, "total_quantity", 0) or getattr(d, "total", 0) or 0)) for d in dcs), ZERO)
        customer_balance = max(ZERO, req_qty - dispatched_qty)

        # Check JWOs (Sent vs Received)
        jwos = JobWorkOrder.objects.filter(client_id=client_id, project=project, deleted_at__isnull=True).exclude(status="Cancelled")
        total_outward = ZERO
        total_inward = ZERO
        for j in jwos:
            total_outward += sum((D(str(getattr(o, "qty", 0) or getattr(o, "meter", 0) or 0)) for o in j.outwards.filter(deleted_at__isnull=True)), ZERO)
            total_inward += sum((D(str(getattr(i, "qty", 0) or getattr(i, "meter", 0) or 0)) for i in j.inwards.filter(deleted_at__isnull=True)), ZERO)
        vendor_pending = max(ZERO, total_outward - total_inward)

        # Check QC
        qcs = QCInspection.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(source_document_number__in=[j.jwo_no for j in jwos]) | Q(notes__icontains=project.code) | Q(source_document_number=project.code)
        )
        qc_passed = sum((D(str(q.passed_qty or 0)) for q in qcs if q.status in ("Approved", "Completed", "Passed")), ZERO)
        qc_hold = sum((D(str(q.hold_qty or 0)) for q in qcs), ZERO)

        # Check Invoices & Payment
        invs = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled")
        if project.sales_order_id:
            invs = invs.filter(Q(sales_order=project.sales_order) | Q(notes__icontains=project.code))
        else:
            invs = invs.filter(notes__icontains=project.code)
        invoiced_amount = sum((D(str(inv.total_amount or 0)) for inv in invs), ZERO)
        paid_amount = sum((D(str(getattr(inv, "amount_paid", 0) or 0)) for inv in invs), ZERO)
        payment_outstanding = max(ZERO, invoiced_amount - paid_amount)

        # Retained Own Stock
        own_allocs = GreyStockAllocation.objects.filter(
            client_id=client_id, project=project, allocation_type=GreyStockAllocation.ALLOCATION_OWN,
            status=GreyStockAllocation.STATUS_ACTIVE, deleted_at__isnull=True
        )
        retained_own_stock = sum((D(str(a.quantity or 0)) for a in own_allocs), ZERO)

        # Check open stages
        from apps.pms.models import CLOSED_STAGE_STATUSES
        open_stages = list(project.stages.filter(deleted_at__isnull=True).exclude(status__in=CLOSED_STAGE_STATUSES).exclude(name="Project Closure"))

        blockers = []
        if customer_balance > ZERO and req_qty > ZERO:
            blockers.append(f"Customer dispatch unfulfilled: {customer_balance:,.2f} M pending.")
        if vendor_pending > ZERO:
            blockers.append(f"Processor balance outstanding: {vendor_pending:,.2f} M still with vendor.")
        if qc_hold > ZERO:
            blockers.append(f"QC Hold unresolved: {qc_hold:,.2f} M pending inspection.")
        if payment_outstanding > ZERO and invoiced_amount > ZERO:
            blockers.append(f"Customer payment unpaid: Rs {payment_outstanding:,.2f} outstanding.")
        if open_stages:
            blockers.append(f"{len(open_stages)} lifecycle stage(s) still open.")

        can_close = len(blockers) == 0

        if request.method == "POST":
            if not can_close and not request.data.get("force"):
                raise ValidationFailed(
                    f"Cannot close project: {'; '.join(blockers)}",
                    code="PROJECT_CLOSURE_BLOCKED",
                    payload={"blockers": blockers}
                )
            project.status = "Completed"
            project.overall_completion_pct = 100
            project.save(update_fields=["status", "overall_completion_pct", "updated_at"])

            closure_stage = project.stages.filter(name="Project Closure").first()
            if closure_stage:
                closure_stage.status = "Completed"
                closure_stage.completion_pct = 100
                closure_stage.save(update_fields=["status", "completion_pct", "updated_at"])

            return Response({
                "success": True,
                "status": "Completed",
                "message": f"Project {project.code} successfully audited and closed.",
            })

        return Response({
            "projectCode": project.code,
            "customerName": project.customer_name,
            "status": project.status,
            "customerRequirement": float(req_qty),
            "dispatchedQuantity": float(dispatched_qty),
            "customerBalance": float(customer_balance),
            "vendorSent": float(total_outward),
            "vendorReceived": float(total_inward),
            "vendorPending": float(vendor_pending),
            "qcPassed": float(qc_passed),
            "qcHold": float(qc_hold),
            "invoicedAmount": float(invoiced_amount),
            "paidAmount": float(paid_amount),
            "paymentOutstanding": float(payment_outstanding),
            "retainedOwnStock": float(retained_own_stock),
            "openStagesCount": len(open_stages),
            "blockers": blockers,
            "canClose": can_close,
        })

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        """Action endpoint alias for project closure."""
        return self.closure_audit(request, pk=pk)

    def _get_stage(self, project, stage_id):
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            stage = project.stages.filter(pk=stage_id, deleted_at__isnull=True).first()
        except (DjangoValidationError, ValueError, TypeError):
            stage = None
        if stage is None:
            raise NotFound("That stage no longer exists.")
        return stage

    def _get_document(self, stage, doc_id):
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            document = stage.documents.filter(pk=doc_id, deleted_at__isnull=True).first()
        except (DjangoValidationError, ValueError, TypeError):
            document = None
        if document is None:
            raise NotFound("That document no longer exists.")
        return document

    @action(detail=True, methods=["get", "post"], url_path="qc-stock-allocation")
    @transaction.atomic
    def qc_stock_allocation(self, request, pk=None):
        """Stage 15 workspace: Finished goods QC Inspection & Stock Allocation.
        Loads actual inward receipts and takas from Stage 14, inspects piece-by-piece,
        posts real stock disposition movements out of SYS-QC-HOLD into warehouse,
        and manages transaction-safe Customer vs Own project stock allocation.
        """
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q
        from django.utils import timezone
        from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
        from apps.core.money import ZERO, D, round4, round2
        from apps.inventory.models import QCInspection, QCInspectionLine, QCInspectionLineDefect, LotTaka, GreyStockAllocation
        from apps.inventory import allocation_services as allocations_svc
        from apps.inventory import services as stock_svc
        from apps.masters.models import Location, DefectMaster
        from apps.jobwork.models import JobWorkOrder, JWOInward, JWOInwardTaka
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        # 1. Gather all linked JWOs and inwards for this project
        jwos = list(
            JobWorkOrder.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .filter(Q(project_id=project.id) | Q(remarks__icontains=project.code) | Q(jwo_no__icontains=project.code))
        )
        jwo_ids = [j.id for j in jwos]
        inwards = list(
            JWOInward.objects.filter(client_id=client_id, job_work_order_id__in=jwo_ids, deleted_at__isnull=True)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        inward_ids = [i.id for i in inwards]
        inward_nos = [i.no for i in inwards if i.no]

        qc_loc = Location.objects.filter(client_id=client_id, code="SYS-QC-HOLD").first()
        main_wh = Location.objects.filter(client_id=client_id, code="WH-MAIN").first() or Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").first()

        # Handle POST actions
        if request.method == "POST":
            action_type = request.data.get("action_type") or request.data.get("action")

            if action_type in ("save_inspection", "approve_inspection", "create_inspection"):
                inward_id = request.data.get("inward_id") or request.data.get("inwardId")
                inward_obj = next((i for i in inwards if str(i.id) == str(inward_id)), None) if inward_id else inwards[0] if inwards else None

                inspection_date = request.data.get("inspection_date") or request.data.get("inspectionDate") or timezone.localdate()
                method = request.data.get("inspection_method") or request.data.get("inspectionMethod") or "ASTM 4-Point Inspection (D5430)"
                notes = request.data.get("notes") or request.data.get("remarks") or ""
                defects_found = request.data.get("defects_found") or request.data.get("defectsFound") or ""
                quality_grade = request.data.get("quality_grade") or request.data.get("qualityGrade") or "Grade A"

                lines_data = request.data.get("lines") or []
                should_approve = action_type == "approve_inspection" or request.data.get("approve", False)

                # Generate inspection number
                from apps.core.numbering import allocate_number
                try:
                    insp_number = allocate_number(request.user.client, "QC", inspection_date)
                except Exception:
                    insp_number = f"QC-{project.code}-{int(timezone.now().timestamp())}"

                inspection = QCInspection.objects.create(
                    client_id=client_id,
                    inspection_number=insp_number,
                    source_document_type="JWO_INWARD",
                    source_document_id=inward_obj.id if inward_obj else None,
                    source_document_number=inward_obj.no if inward_obj else project.code,
                    inspection_date=inspection_date,
                    inspector=request.user,
                    status="Pending",
                    inspection_type="Finished Goods QC",
                    inspection_method=method,
                    notes=notes,
                    defects_found=defects_found,
                    quality_grade=quality_grade,
                    fabric_item=None,
                )

                tot_inspected = ZERO
                tot_passed = ZERO
                tot_second = ZERO
                tot_reprocess = ZERO
                tot_reject = ZERO
                tot_hold = ZERO

                for ld in lines_data:
                    lot_taka_id = ld.get("lot_taka_id") or ld.get("lotTakaId") or ld.get("id")
                    lt = LotTaka.objects.filter(client_id=client_id, id=lot_taka_id).first() if lot_taka_id else None

                    actual_meter = round4(D(ld.get("actual_meter") or ld.get("actualMeter") or (lt.received_quantity if lt else 0) or 0))
                    if actual_meter <= ZERO:
                        continue
                    width = round2(D(ld.get("width") or (lt.width if lt else 44) or 44))
                    taka_no = str(ld.get("taka_number") or ld.get("takaNo") or (lt.taka_number if lt else "1"))
                    lot_no = str(ld.get("lot_number") or ld.get("lotNo") or (lt.lot_number if lt else "LOT-1"))

                    defects_arr = ld.get("defects") or []
                    tot_pts = 0
                    for df in defects_arr:
                        df_pts = int(df.get("points") or 1)
                        df_cnt = int(df.get("count") or df.get("defect_count") or 1)
                        tot_pts += (df_pts * df_cnt)

                    width_m = width * Decimal("0.0254")
                    area = actual_meter * width_m
                    pts_100 = round2((Decimal(tot_pts) * Decimal("100")) / area) if area > ZERO else Decimal("0.00")

                    disp = ld.get("disposition")
                    grd = ld.get("grade")
                    if not disp:
                        if pts_100 <= Decimal("20.0"):
                            grd = grd or "A"
                            disp = "Pass"
                        elif pts_100 <= Decimal("28.0"):
                            grd = grd or "B"
                            disp = "Pass"
                        elif pts_100 <= Decimal("40.0"):
                            grd = grd or "C"
                            disp = "Second Quality"
                        else:
                            grd = grd or "Second Quality"
                            disp = "Reject"

                    line_obj = QCInspectionLine.objects.create(
                        client_id=client_id,
                        inspection=inspection,
                        lot_taka=lt,
                        taka_number=taka_no,
                        lot_number=lot_no,
                        original_meter=lt.received_quantity if lt else actual_meter,
                        actual_meter=actual_meter,
                        roll_weight=ld.get("roll_weight") or ld.get("rollWeight") or (lt.roll_weight if lt else None),
                        width=width,
                        gsm=ld.get("gsm") or (lt.gsm if lt else None),
                        grade=grd or "A",
                        penalty_points=tot_pts,
                        points_per_100_sq_meter=pts_100,
                        disposition=disp,
                        remarks=ld.get("remarks"),
                    )

                    for df in defects_arr:
                        defect_id = df.get("defect_id") or df.get("defectId")
                        dm = DefectMaster.objects.filter(client_id=client_id, id=defect_id).first() if defect_id else None
                        if not dm:
                            dm = DefectMaster.objects.filter(client_id=client_id).first()
                        if dm:
                            QCInspectionLineDefect.objects.create(
                                client_id=client_id,
                                inspection_line=line_obj,
                                defect=dm,
                                defect_count=int(df.get("count") or df.get("defect_count") or 1),
                                points=int(df.get("points") or 1),
                                distance_meter=df.get("distance_meter") or df.get("distanceMeter"),
                                remarks=df.get("remarks"),
                            )

                    tot_inspected += actual_meter
                    if disp == "Pass":
                        tot_passed += actual_meter
                    elif disp == "Second Quality":
                        tot_second += actual_meter
                    elif disp == "Reprocess":
                        tot_reprocess += actual_meter
                    elif disp == "Reject":
                        tot_reject += actual_meter
                    else:
                        tot_hold += actual_meter

                inspection.total_inspected_qty = tot_inspected
                inspection.passed_qty = tot_passed
                inspection.second_quality_qty = tot_second
                inspection.reprocess_qty = tot_reprocess
                inspection.rejected_qty = tot_reject
                inspection.hold_qty = tot_hold
                inspection.save()

                if should_approve:
                    # Move physical stock out of SYS-QC-HOLD based on disposition
                    for line in inspection.lines.select_related("lot_taka", "lot_taka__location", "lot_taka__item"):
                        lt = line.lot_taka
                        if not lt:
                            continue
                        disp = line.disposition
                        m = line.actual_meter or lt.available_quantity or ZERO
                        if m <= ZERO:
                            continue

                        status_val = "Available"
                        qc_status_val = "Passed"
                        target_loc = None
                        mov_type = None

                        if disp == "Pass":
                            status_val = "Available"
                            qc_status_val = "Passed"
                            mov_type = "QC_PASS"
                            target_loc = main_wh
                        elif disp == "Second Quality":
                            status_val = "Second Quality"
                            qc_status_val = "Second Quality"
                            mov_type = "QC_SECOND_QUALITY"
                            target_loc = stock_svc.resolve_system_location(client_id, "SECOND_QUALITY")
                        elif disp == "Reprocess":
                            status_val = "Reprocess"
                            qc_status_val = "Reprocess"
                            mov_type = "QC_REPROCESS"
                            target_loc = stock_svc.resolve_system_location(client_id, "REPROCESS")
                        elif disp == "Reject":
                            status_val = "Reject"
                            qc_status_val = "Reject"
                            mov_type = "QC_REJECT"
                            target_loc = stock_svc.resolve_system_location(client_id, "REJECT")
                        else:
                            status_val = "QC Hold"
                            qc_status_val = "On Hold"
                            target_loc = lt.location

                        if mov_type and target_loc and str(lt.location_id) != str(target_loc.id):
                            # Ensure available_quantity is positive before relocating
                            if lt.available_quantity <= ZERO:
                                lt.available_quantity = m
                                lt.save(update_fields=["available_quantity"])
                            stock_svc.relocate_lot(
                                client_id=client_id,
                                lot=lt,
                                quantity=m,
                                to_location_id=target_loc.id,
                                out_type=mov_type,
                                in_type=mov_type,
                                reference_type="QCInspection",
                                reference_id=inspection.id,
                                reference_number=inspection.inspection_number,
                                user=request.user,
                            )

                        lt.refresh_from_db()
                        lt.status = status_val
                        lt.qc_status = qc_status_val
                        lt.save(update_fields=["status", "qc_status", "updated_at"])

                    inspection.status = "Approved"
                    inspection.save(update_fields=["status", "updated_at"])

                sync_project_pipeline_stages(project)
                return Response({"success": True, "inspection_id": str(inspection.id), "inspection_number": inspection.inspection_number, "status": inspection.status})

            elif action_type == "allocate_stock":
                lot_taka_id = request.data.get("lot_taka_id") or request.data.get("lotTakaId")
                qty = request.data.get("quantity") or request.data.get("allocate_qty") or request.data.get("allocateQty")
                alloc_type = request.data.get("allocation_type") or request.data.get("allocationType") or GreyStockAllocation.ALLOCATION_CUSTOMER
                remarks = request.data.get("remarks")

                if not lot_taka_id or not qty:
                    raise ValidationFailed("Lot Taka ID and quantity are required.")

                alloc = allocations_svc.allocate_to_project(
                    client_id=client_id,
                    project=project,
                    lot_taka_id=lot_taka_id,
                    quantity=qty,
                    allocation_type=alloc_type,
                    user=request.user,
                    remarks=remarks,
                )
                sync_project_pipeline_stages(project)
                return Response({"success": True, "allocation_id": str(alloc.id), "quantity": float(alloc.quantity), "allocation_type": alloc.allocation_type})

            elif action_type == "release_allocation":
                alloc_id = request.data.get("allocation_id") or request.data.get("allocationId")
                if not alloc_id:
                    raise ValidationFailed("Allocation ID is required.")
                alloc = allocations_svc.release_allocation(
                    client_id=client_id,
                    allocation_id=alloc_id,
                    user=request.user,
                    remarks=request.data.get("remarks") or "Released from PMS Stage 15 workspace",
                )
                sync_project_pipeline_stages(project)
                return Response({"success": True, "released": True, "allocation_id": str(alloc.id)})

            raise ValidationFailed(f"Unknown action_type: {action_type}")

        # 2. GET: Build detailed Stage 15 workspace dataset
        # A. Takas received from Stage 14
        inward_takas_list = []
        for iw in inwards:
            jwo = iw.job_work_order
            vendor_name = (jwo.vendor.name if (jwo and jwo.vendor) else None) or (jwo.vendor_name if jwo else None) or "Processor"
            pp = jwo.process_plan if jwo else None
            fabric_name = (pp.fabric_item if pp and pp.fabric_item else None) or "Finished Fabric"
            fabric_quality = (pp.fabric_quality if pp and pp.fabric_quality else None) or "Standard"
            process_type = (jwo.process if jwo else None) or (pp.process if pp else None) or "Processing"

            t_rows = list(iw.takas.select_related("received_taka", "source_taka").all())
            if not t_rows:
                # If inward was recorded without separate taka rows, create a surrogate/dock taka record
                surrogate_taka = LotTaka.objects.filter(
                    client_id=client_id, source_document_id=jwo.id, source_document_number=iw.no
                ).first()
                if not surrogate_taka:
                    from apps.masters.models import Item
                    target_item = None
                    if pp and pp.fabric_item:
                        target_item = Item.objects.filter(client_id=client_id, name__iexact=pp.fabric_item).first() or Item.objects.filter(client_id=client_id, sku__iexact=pp.fabric_item).first()
                    if not target_item:
                        target_item = Item.objects.filter(client_id=client_id).first()
                    surrogate_taka = LotTaka.objects.create(
                        client_id=client_id,
                        item=target_item,
                        location=qc_loc or main_wh,
                        lot_number=f"LOT-{iw.no}",
                        taka_number="1",
                        taka_id=f"TAK-{iw.no}-1",
                        received_quantity=iw.qty,
                        available_quantity=iw.qty,
                        status="QC Hold",
                        qc_status="Pending",
                        source_document_type="JobWorkInward",
                        source_document_id=jwo.id,
                        source_document_number=iw.no,
                    )
                inward_takas_list.append({
                    "id": str(surrogate_taka.id),
                    "lotTakaId": str(surrogate_taka.id),
                    "takaId": surrogate_taka.taka_id,
                    "takaNumber": surrogate_taka.taka_number,
                    "lotNumber": surrogate_taka.lot_number,
                    "receivedMeters": float(iw.qty),
                    "availableQuantity": float(surrogate_taka.available_quantity),
                    "width": float(surrogate_taka.width or 44),
                    "rollWeight": float(surrogate_taka.roll_weight or 0),
                    "shade": getattr(surrogate_taka, "shade", None) or "Natural",
                    "inwardId": str(iw.id),
                    "inwardNumber": iw.no,
                    "inwardDate": str(iw.date),
                    "jwoId": str(jwo.id),
                    "jwoNumber": jwo.jwo_no,
                    "vendorName": vendor_name,
                    "fabricName": fabric_name,
                    "fabricQuality": fabric_quality,
                    "processType": process_type,
                    "status": surrogate_taka.status,
                    "qcStatus": surrogate_taka.qc_status,
                    "locationName": surrogate_taka.location.name if surrogate_taka.location else "QC Hold",
                    "locationCode": surrogate_taka.location.code if surrogate_taka.location else "SYS-QC-HOLD",
                })
            else:
                for tr in t_rows:
                    rt = tr.received_taka
                    st = tr.source_taka
                    t_id = str(rt.id) if rt else str(tr.id)
                    inward_takas_list.append({
                        "id": t_id,
                        "lotTakaId": str(rt.id) if rt else None,
                        "takaId": (rt.taka_id if rt else None) or f"TAK-{tr.taka_number}",
                        "takaNumber": tr.taka_number or (rt.taka_number if rt else "1"),
                        "lotNumber": (rt.lot_number if rt else None) or (st.lot_number if st else "LOT-1"),
                        "receivedMeters": float(tr.meters or (rt.received_quantity if rt else 0) or 0),
                        "availableQuantity": float(rt.available_quantity if rt else 0),
                        "width": float((tr.width or (rt.width if rt else None) or 44)),
                        "rollWeight": float((tr.weight_kg or (rt.roll_weight if rt else None) or 0)),
                        "shade": tr.shade or (rt.shade if rt else "Natural"),
                        "inwardId": str(iw.id),
                        "inwardNumber": iw.no,
                        "inwardDate": str(iw.date),
                        "jwoId": str(jwo.id),
                        "jwoNumber": jwo.jwo_no,
                        "vendorName": vendor_name,
                        "fabricName": fabric_name,
                        "fabricQuality": fabric_quality,
                        "processType": process_type,
                        "status": rt.status if rt else "QC Hold",
                        "qcStatus": rt.qc_status if rt else "Pending",
                        "locationName": (rt.location.name if rt and rt.location else "QC Hold"),
                        "locationCode": (rt.location.code if rt and rt.location else "SYS-QC-HOLD"),
                    })

        # B. Load active and released allocations for this project
        alloc_rows = list(
            GreyStockAllocation.objects.filter(client_id=client_id, project=project, deleted_at__isnull=True)
            .select_related("lot_taka", "lot_taka__item", "created_by", "released_by")
            .order_by("-created_at")
        )
        alloc_by_taka = {}
        alloc_history = []
        for a in alloc_rows:
            is_active = a.status == GreyStockAllocation.STATUS_ACTIVE
            if is_active:
                if a.lot_taka_id not in alloc_by_taka:
                    alloc_by_taka[a.lot_taka_id] = {"customer": ZERO, "own": ZERO}
                if a.allocation_type == GreyStockAllocation.ALLOCATION_OWN:
                    alloc_by_taka[a.lot_taka_id]["own"] += a.quantity
                else:
                    alloc_by_taka[a.lot_taka_id]["customer"] += a.quantity

            creator = a.created_by
            releaser = a.released_by
            alloc_history.append({
                "id": str(a.id),
                "lotTakaId": str(a.lot_taka_id),
                "lotNumber": a.lot_taka.lot_number if a.lot_taka else "—",
                "takaNumber": a.lot_taka.taka_number if a.lot_taka else "—",
                "fabricName": (a.lot_taka.item.name if a.lot_taka and a.lot_taka.item else None) or (a.item.name if a.item else "Fabric"),
                "quantity": float(a.quantity),
                "uom": a.uom or "Meters",
                "allocationType": a.allocation_type,
                "status": a.status,
                "remarks": a.remarks,
                "createdAt": timezone.localtime(a.created_at).strftime("%d/%m/%Y %I:%M %p"),
                "createdBy": getattr(creator, "name", None) or getattr(creator, "email", "Admin") if creator else "Admin",
                "releasedAt": timezone.localtime(a.released_at).strftime("%d/%m/%Y %I:%M %p") if a.released_at else None,
                "releasedBy": getattr(releaser, "name", None) or getattr(releaser, "email", None) if releaser else None,
            })

        # Enrich inward takas with allocation balances
        for it in inward_takas_list:
            t_id = it.get("lotTakaId") or it.get("id")
            takes = alloc_by_taka.get(t_id, {"customer": ZERO, "own": ZERO})
            c_alloc = float(takes["customer"])
            o_alloc = float(takes["own"])
            tot_alloc = c_alloc + o_alloc
            it["customerAllocatedQty"] = c_alloc
            it["ownAllocatedQty"] = o_alloc
            it["totalAllocatedQty"] = tot_alloc

            avail = float(it.get("availableQuantity") or 0)
            is_passed = it.get("qcStatus") in ("Passed", "Pass") and it.get("status") not in ("QC Hold", "Reject", "Reprocess")
            rem_eligible = max(0.0, avail - tot_alloc) if is_passed else 0.0
            it["remainingEligibleQty"] = rem_eligible

            if tot_alloc > 0:
                if tot_alloc >= avail and avail > 0:
                    it["allocationStatus"] = "Fully Allocated"
                else:
                    it["allocationStatus"] = "Partially Allocated"
            else:
                it["allocationStatus"] = "Unallocated" if is_passed else "Not Eligible"

        # C. Load QC Inspections for this project
        qc_inspections_qs = QCInspection.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(source_document_id__in=inward_ids) | Q(source_document_number__in=inward_nos)
            | Q(notes__icontains=project.code) | Q(source_document_number=project.code)
        ).prefetch_related("lines__defects__defect", "lines__lot_taka").order_by("-inspection_date", "-created_at")

        inspections_list = []
        for q in qc_inspections_qs:
            lines_list = []
            for l in q.lines.all():
                d_list = []
                for df in l.defects.all():
                    d_list.append({
                        "id": str(df.id),
                        "defectCode": df.defect.code if df.defect else "DEF",
                        "defectName": df.defect.name if df.defect else "Defect",
                        "category": df.defect.category if df.defect else "weaving defect",
                        "severity": df.defect.severity if df.defect else "Minor",
                        "count": df.defect_count,
                        "points": df.points,
                        "distanceMeter": float(df.distance_meter) if df.distance_meter else None,
                    })
                lines_list.append({
                    "id": str(l.id),
                    "lotTakaId": str(l.lot_taka_id) if l.lot_taka_id else None,
                    "takaNumber": l.taka_number,
                    "lotNumber": l.lot_number,
                    "originalMeter": float(l.original_meter),
                    "actualMeter": float(l.actual_meter),
                    "rollWeight": float(l.roll_weight) if l.roll_weight else None,
                    "width": float(l.width) if l.width else None,
                    "gsm": float(l.gsm) if l.gsm else None,
                    "grade": l.grade,
                    "penaltyPoints": l.penalty_points,
                    "pointsPer100SqMeter": float(l.points_per_100_sq_meter),
                    "disposition": l.disposition,
                    "remarks": l.remarks,
                    "defects": d_list,
                })

            inspections_list.append({
                "id": str(q.id),
                "inspectionNumber": q.inspection_number,
                "sourceDocumentNumber": q.source_document_number or "—",
                "inspectionDate": str(q.inspection_date),
                "inspectorName": (getattr(q.inspector, "name", None) or getattr(q.inspector, "email", "QC Inspector")) if q.inspector else "QC Inspector",
                "status": q.status,
                "totalReceivedQty": float(q.total_received_qty),
                "totalInspectedQty": float(q.total_inspected_qty),
                "passedQty": float(q.passed_qty),
                "secondQualityQty": float(q.second_quality_qty),
                "reprocessQty": float(q.reprocess_qty),
                "rejectedQty": float(q.rejected_qty),
                "holdQty": float(q.hold_qty),
                "inspectionMethod": q.inspection_method,
                "defectsFound": q.defects_found,
                "qualityGrade": q.quality_grade,
                "notes": q.notes,
                "lines": lines_list,
            })

        # D. Defect Master Catalog (seed default textile defects if DB empty)
        defects_qs = list(DefectMaster.objects.filter(client_id=client_id, is_active=True).order_by("category", "name"))
        if not defects_qs:
            default_defects = [
                ("DEF-WEAV-01", "Broken Pick / Weft Miss", "weaving defect", "Major"),
                ("DEF-WEAV-02", "Slub / Thick Yarn", "weaving defect", "Minor"),
                ("DEF-HOLE-01", "Hole / Pin Hole / Tear", "hole", "Critical"),
                ("DEF-SHADE-01", "Shade Variation (Side to Center)", "shade", "Major"),
                ("DEF-STAIN-01", "Oil Spot / Grease Stain", "stain", "Minor"),
                ("DEF-STAIN-02", "Dyeing Patch / Color Bleed", "stain", "Major"),
                ("DEF-PRINT-01", "Printing Smudge / Misprint", "printing defect", "Major"),
                ("DEF-FIN-01", "Crease Mark / Skewness", "finish", "Minor"),
            ]
            for c_code, c_name, c_cat, c_sev in default_defects:
                dm, _ = DefectMaster.objects.get_or_create(
                    client_id=client_id, code=c_code,
                    defaults={"name": c_name, "category": c_cat, "severity": c_sev, "is_active": True}
                )
                defects_qs.append(dm)

        defect_catalog = [
            {"id": str(d.id), "code": d.code, "name": d.name, "category": d.category, "severity": d.severity, "defaultPoints": 4 if d.severity == "Critical" else (3 if d.severity == "Major" else 1)}
            for d in defects_qs
        ]

        # E. Summary Calculations
        total_inward = sum((float(iw.qty) for iw in inwards), 0.0)
        tot_passed = sum((float(q.passed_qty) for q in qc_inspections_qs if q.status in ("Approved", "Completed", "Passed")), 0.0)
        tot_hold = sum((float(q.hold_qty) for q in qc_inspections_qs), 0.0)
        tot_rejected = sum((float(q.rejected_qty) for q in qc_inspections_qs), 0.0)
        tot_inspected = sum((float(q.total_inspected_qty) for q in qc_inspections_qs), 0.0)

        # Pending QC = Total Inward - Total Inspected
        tot_pending_qc = max(0.0, total_inward - tot_inspected)

        # Allocations
        cust_allocated = sum((float(a.quantity) for a in alloc_rows if a.status == GreyStockAllocation.STATUS_ACTIVE and a.allocation_type == GreyStockAllocation.ALLOCATION_CUSTOMER), 0.0)
        own_allocated = sum((float(a.quantity) for a in alloc_rows if a.status == GreyStockAllocation.STATUS_ACTIVE and a.allocation_type == GreyStockAllocation.ALLOCATION_OWN), 0.0)
        avail_for_allocation = max(0.0, tot_passed - (cust_allocated + own_allocated))

        target_req = float(project.requirement_meters or project.quantity or 3000.0)

        return Response({
            "project": {
                "id": str(project.id),
                "code": project.code,
                "name": getattr(project, "project_name", None) or getattr(project, "name", None) or getattr(project, "title", None) or project.code,
                "targetRequirement": target_req,
                "uom": "Meters",
            },
            "summary": {
                "totalInwardQty": total_inward,
                "qcPendingQty": tot_pending_qc,
                "qcPassedQty": tot_passed,
                "qcHoldQty": tot_hold,
                "qcRejectedQty": tot_rejected,
                "customerAllocatedQty": cust_allocated,
                "ownAllocatedQty": own_allocated,
                "availableForAllocation": avail_for_allocation,
                "targetRequirementMeters": target_req,
                "uom": "Meters",
            },
            "inwardTakas": inward_takas_list,
            "inspections": inspections_list,
            "allocations": alloc_history,
            "defectMaster": defect_catalog,
        })

    @action(detail=True, methods=["get", "post"], url_path="customer-dispatch")
    @transaction.atomic
    def customer_dispatch(self, request, pk=None):
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q, Sum
        from django.utils import timezone
        from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
        from apps.core.money import ZERO, D, round4, round2
        from apps.core.numbering import allocate_number
        from apps.inventory.models import LotTaka, GreyStockAllocation, QCInspection
        from apps.inventory import services as stock_svc
        from apps.sales.models import DeliveryChallan, DeliveryChallanLine, SalesOrder, SalesOrderLine
        from apps.masters.models import Location, Party, Item
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        order = project.sales_order
        main_wh = Location.objects.filter(client_id=client_id, code="WH-MAIN").first() or Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").first()

        # 1. POST: Create customer dispatch challan or update LR details
        if request.method == "POST":
            action_type = request.data.get("action_type") or request.data.get("actionType")

            if action_type == "create_dispatch":
                selected_takas = request.data.get("selected_takas") or request.data.get("selectedTakas") or []
                if not selected_takas:
                    raise ValidationFailed("At least one taka must be selected for customer dispatch.", field_errors={"selectedTakas": ["Required."]})

                dispatch_date_str = request.data.get("dispatch_date") or request.data.get("dispatchDate")
                dispatch_date = timezone.datetime.strptime(dispatch_date_str, "%Y-%m-%d").date() if dispatch_date_str else timezone.localdate()

                loc_id = request.data.get("location_id") or request.data.get("locationId")
                dispatch_location = Location.objects.filter(client_id=client_id, id=loc_id).first() if loc_id else main_wh

                delivery_address = request.data.get("delivery_address") or request.data.get("deliveryAddress") or ""
                transporter = request.data.get("transporter") or ""
                vehicle_number = request.data.get("vehicle_number") or request.data.get("vehicleNo") or ""
                lr_number = request.data.get("lr_number") or request.data.get("lrNo") or ""
                freight_charges = Decimal(str(request.data.get("freight_charges") or request.data.get("freightCharges") or 0))
                notes = request.data.get("notes") or request.data.get("remarks") or ""

                # Resolve party
                party = project.party or (order.party if order else None)
                if not party:
                    party = Party.objects.filter(client_id=client_id, party_type__in=["Customer", "Both"]).first() or Party.objects.filter(client_id=client_id).first()
                if not party:
                    raise ValidationFailed("Customer party is required for dispatch challan.")

                if isinstance(selected_takas, str):
                    import json
                    try:
                        selected_takas = json.loads(selected_takas)
                    except Exception:
                        selected_takas = []

                # Validate takas and compute totals
                parsed_takas = []
                total_dispatch_qty = ZERO
                for item_sel in selected_takas:
                    if isinstance(item_sel, str):
                        taka_id = item_sel
                        qty = ZERO
                    else:
                        taka_id = item_sel.get("lot_taka_id") or item_sel.get("lotTakaId") or item_sel.get("id")
                        qty = round4(D(item_sel.get("dispatch_qty") or item_sel.get("quantity") or item_sel.get("qty") or 0))
                    if qty <= ZERO:
                        continue
                    taka = LotTaka.objects.select_for_update().filter(client_id=client_id, id=taka_id, deleted_at__isnull=True).first()
                    if not taka:
                        raise NotFound(f"Lot taka {taka_id} not found.")
                    if taka.qc_status not in ("Passed", "Pass"):
                        raise BusinessRuleViolation(f"Taka {taka.taka_id or taka.taka_number} is {taka.qc_status} in QC and cannot be dispatched.")
                    if qty > D(taka.available_quantity):
                        raise BusinessRuleViolation(
                            f"Requested {qty} M on taka {taka.taka_id or taka.taka_number}, but only {taka.available_quantity} M available."
                        )
                    parsed_takas.append((taka, qty))
                    total_dispatch_qty += qty

                if total_dispatch_qty <= ZERO:
                    raise ValidationFailed("Total dispatch quantity must be greater than zero.")

                # Allocate Challan Number
                dc_number = allocate_number(client_id, "DC", dispatch_date)

                # Create DeliveryChallan
                challan = DeliveryChallan.objects.create(
                    client_id=client_id,
                    party=party,
                    party_name=party.name if party else (project.customer_name or "Customer"),
                    party_gstin=getattr(party, "gstin", None) or getattr(party, "gst_number", None),
                    sales_order=order,
                    doc_date=dispatch_date,
                    dispatch_date=dispatch_date,
                    challan_number=dc_number,
                    status="Dispatched",
                    location=dispatch_location or main_wh,
                    transporter=transporter,
                    vehicle_number=vehicle_number,
                    lr_number=lr_number,
                    freight_charges=freight_charges,
                    allocated_freight_cost=freight_charges,
                    subtotal=Decimal("0.00"),
                    total=freight_charges,
                    delivery_location=delivery_address or (getattr(party, "shipping_address", None) or getattr(party, "address", None) or ""),
                    notes=f"Project: {project.code}. {notes}".strip(),
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                    posted_at=timezone.now(),
                    posted_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                # Create DeliveryChallanLine(s) grouped by Item
                items_map = {}
                for taka, qty in parsed_takas:
                    it = taka.item
                    if not it:
                        it = Item.objects.filter(client_id=client_id).first()
                    item_key = it.id if it else "none"
                    if item_key not in items_map:
                        items_map[item_key] = {"item": it, "takas": [], "total_qty": ZERO}
                    items_map[item_key]["takas"].append((taka, qty))
                    items_map[item_key]["total_qty"] += qty

                so_lines = list(order.line_items.all()) if order else []

                line_no = 1
                for item_key, g_data in items_map.items():
                    it = g_data["item"]
                    g_qty = g_data["total_qty"]
                    g_takas = g_data["takas"]

                    matched_so_line = None
                    rate = ZERO
                    if so_lines:
                        matched_so_line = next((sl for sl in so_lines if sl.item_id == (it.id if it else None)), so_lines[0])
                        rate = D(matched_so_line.rate) if matched_so_line else ZERO

                    taka_alloc_json = [
                        {
                            "lotTakaId": str(t.id),
                            "qty": str(q),
                            "takaNumber": t.taka_number,
                            "lotNumber": t.lot_number,
                        }
                        for t, q in g_takas
                    ]

                    DeliveryChallanLine.objects.create(
                        client_id=client_id,
                        delivery_challan=challan,
                        line_no=line_no,
                        item=it,
                        sku=it.sku if it else "",
                        item_name=it.name if it else (project.product_name or "Fabric"),
                        uom=it.uom if it else "Meters",
                        qty=g_qty,
                        rate=rate,
                        amount=round2(g_qty * rate),
                        line_total=round2(g_qty * rate),
                        taka_count=len(g_takas),
                        sales_order_line=matched_so_line,
                        taka_allocations=taka_alloc_json,
                    )
                    line_no += 1

                    if matched_so_line:
                        matched_so_line.dispatched_qty = min(matched_so_line.qty, round4(D(matched_so_line.dispatched_qty or 0) + g_qty))
                        matched_so_line.save(update_fields=["dispatched_qty", "updated_at"])

                # Post stock movement for each dispatched taka
                for taka, qty in parsed_takas:
                    use_loc = taka.location or dispatch_location or main_wh
                    stock_svc.post_movement(
                        client_id=client_id,
                        item=taka.item or (challan.line_items.first().item if challan.line_items.exists() else None),
                        location=use_loc,
                        type="SALE",
                        quantity=-qty,
                        unit_cost=D(taka.rate) if (taka.rate and D(taka.rate) > ZERO) else ZERO,
                        reference_type="DeliveryChallan",
                        reference_id=challan.id,
                        reference_number=challan.challan_number,
                        movement_date=challan.dispatch_date,
                        lot_taka=taka,
                        notes=f"PMS Stage 16 Customer Dispatch for {project.code}",
                        user=request.user,
                    )

                    # Update GreyStockAllocation if exists for this taka and project
                    active_alloc = GreyStockAllocation.objects.filter(
                        client_id=client_id, project=project, lot_taka=taka, status=GreyStockAllocation.STATUS_ACTIVE
                    ).first()
                    if active_alloc:
                        new_alloc_qty = round4(max(ZERO, D(active_alloc.quantity) - qty))
                        active_alloc.quantity = new_alloc_qty
                        if new_alloc_qty <= ZERO:
                            active_alloc.status = GreyStockAllocation.STATUS_RELEASED
                            active_alloc.remarks = f"Fully dispatched via DC {challan.challan_number}"
                        else:
                            active_alloc.remarks = f"Partially dispatched via DC {challan.challan_number}"
                        active_alloc.save(update_fields=["quantity", "status", "remarks", "updated_at"])

                # Recalculate totals on challan
                tot_amt = sum((l.line_total for l in challan.line_items.all()), ZERO)
                challan.subtotal = tot_amt
                challan.total = tot_amt + freight_charges
                challan.save(update_fields=["subtotal", "total", "updated_at"])

                # Advance sales order stage if all lines dispatched
                if order:
                    all_lines = list(order.line_items.all())
                    if all_lines and all(D(l.dispatched_qty or 0) >= D(l.qty) for l in all_lines):
                        order.stage = "Fully Dispatched"
                        order.save(update_fields=["stage", "updated_at"])
                    elif any(D(l.dispatched_qty or 0) > ZERO for l in all_lines):
                        order.stage = "Partially Dispatched"
                        order.save(update_fields=["stage", "updated_at"])

                # Trigger PMS pipeline synchronization
                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "challan_id": str(challan.id),
                    "challan_number": challan.challan_number,
                    "status": challan.status,
                    "dispatched_qty": float(total_dispatch_qty),
                })

            elif action_type == "update_lr":
                challan_id = request.data.get("challan_id") or request.data.get("challanId")
                if not challan_id:
                    raise ValidationFailed("Delivery challan ID is required.")
                challan = DeliveryChallan.objects.filter(client_id=client_id, id=challan_id).first()
                if not challan:
                    raise NotFound("Delivery challan not found.")

                if "transporter" in request.data:
                    challan.transporter = request.data.get("transporter")
                if "vehicle_number" in request.data or "vehicleNo" in request.data:
                    challan.vehicle_number = request.data.get("vehicle_number") or request.data.get("vehicleNo")
                if "lr_number" in request.data or "lrNo" in request.data:
                    challan.lr_number = request.data.get("lr_number") or request.data.get("lrNo")
                if "freight_charges" in request.data or "freightCharges" in request.data:
                    fc = Decimal(str(request.data.get("freight_charges") or request.data.get("freightCharges") or 0))
                    challan.freight_charges = fc
                    challan.allocated_freight_cost = fc
                    challan.total = challan.subtotal + fc
                if "delivery_address" in request.data or "deliveryAddress" in request.data:
                    challan.delivery_location = request.data.get("delivery_address") or request.data.get("deliveryAddress")
                if "notes" in request.data or "remarks" in request.data:
                    challan.notes = request.data.get("notes") or request.data.get("remarks")

                challan.save()
                sync_project_pipeline_stages(project)
                return Response({
                    "success": True,
                    "challan_id": str(challan.id),
                    "challan_number": challan.challan_number,
                    "lr_number": challan.lr_number,
                    "transporter": challan.transporter,
                    "vehicle_number": challan.vehicle_number,
                })

            raise ValidationFailed(f"Unknown action_type: {action_type}")

        # 2. GET: Build detailed Stage 16 Customer Dispatch workspace dataset
        # A. Query eligible takas for Customer Project
        alloc_qs = GreyStockAllocation.objects.filter(
            client_id=client_id, project=project, status=GreyStockAllocation.STATUS_ACTIVE, deleted_at__isnull=True
        ).select_related("lot_taka", "lot_taka__location", "lot_taka__item")

        from apps.jobwork.models import JobWorkOrder, JWOInward
        jwos = list(JobWorkOrder.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(project_id=project.id) | Q(remarks__icontains=project.code) | Q(jwo_no__icontains=project.code)
        ))
        jwo_ids = [j.id for j in jwos]
        inwards = list(JWOInward.objects.filter(client_id=client_id, job_work_order_id__in=jwo_ids, deleted_at__isnull=True))
        inward_ids = [i.id for i in inwards]
        inward_nos = [i.no for i in inwards if i.no]

        eligible_takas_map = {}

        # Add explicitly allocated takas
        for a in alloc_qs:
            lt = a.lot_taka
            if not lt or lt.available_quantity <= ZERO or lt.qc_status not in ("Passed", "Pass"):
                continue
            if a.allocation_type != GreyStockAllocation.ALLOCATION_CUSTOMER:
                continue
            eligible_takas_map[str(lt.id)] = {
                "id": str(lt.id),
                "lotTakaId": str(lt.id),
                "lotNumber": lt.lot_number,
                "takaNumber": lt.taka_number,
                "takaId": lt.taka_id or f"{lt.lot_number}-{lt.taka_number}",
                "fabricItem": lt.item.name if lt.item else (project.product_name or "Fabric"),
                "fabricQuality": getattr(lt.item, "fabric_quality", None) or "Standard",
                "shade": getattr(lt, "shade", None) or "Natural",
                "width": float(lt.width or 44),
                "rollWeight": float(lt.roll_weight or 0),
                "receivedMeters": float(lt.received_quantity or 0),
                "availableQuantity": float(lt.available_quantity or 0),
                "qcStatus": lt.qc_status or "Passed",
                "locationId": str(lt.location_id) if lt.location_id else None,
                "locationName": lt.location.name if lt.location else "Main Godown",
                "allocationId": str(a.id),
                "allocatedQty": float(a.quantity),
                "destination": "Customer Project",
                "salesOrderNumber": order.order_number if order else "SO-Pending",
                "customerName": project.customer_name or (project.party.name if project.party else "Customer"),
            }

        # Also add any QC passed takas from project inward not yet allocated/dispatched
        taka_inward_qs = LotTaka.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            available_quantity__gt=ZERO,
            qc_status__in=["Passed", "Pass"],
        ).exclude(status=LotTaka.STATUS_DISPATCHED).filter(
            Q(source_document_id__in=inward_ids)
            | Q(source_document_number__in=inward_nos)
            | Q(lot_number__icontains=project.code)
            | Q(remarks__icontains=project.code)
        ).select_related("location", "item")

        for lt in taka_inward_qs:
            if str(lt.id) not in eligible_takas_map:
                eligible_takas_map[str(lt.id)] = {
                    "id": str(lt.id),
                    "lotTakaId": str(lt.id),
                    "lotNumber": lt.lot_number,
                    "takaNumber": lt.taka_number,
                    "takaId": lt.taka_id or f"{lt.lot_number}-{lt.taka_number}",
                    "fabricItem": lt.item.name if lt.item else (project.product_name or "Fabric"),
                    "fabricQuality": getattr(lt.item, "fabric_quality", None) or "Standard",
                    "shade": getattr(lt, "shade", None) or "Natural",
                    "width": float(lt.width or 44),
                    "rollWeight": float(lt.roll_weight or 0),
                    "receivedMeters": float(lt.received_quantity or 0),
                    "availableQuantity": float(lt.available_quantity or 0),
                    "qcStatus": lt.qc_status or "Passed",
                    "locationId": str(lt.location_id) if lt.location_id else None,
                    "locationName": lt.location.name if lt.location else "Main Godown",
                    "allocationId": None,
                    "allocatedQty": float(lt.available_quantity or 0),
                    "destination": "Customer Project",
                    "salesOrderNumber": order.order_number if order else "SO-Pending",
                    "customerName": project.customer_name or (project.party.name if project.party else "Customer"),
                }

        eligible_takas_list = list(eligible_takas_map.values())

        # B. Delivery Challans for this project / sales order
        challans_qs = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if order:
            challans_qs = challans_qs.filter(
                Q(sales_order=order) | Q(notes__icontains=project.code)
            )
        else:
            challans_qs = challans_qs.filter(notes__icontains=project.code)

        challans_qs = challans_qs.prefetch_related("line_items__item", "party").order_by("-dispatch_date", "-created_at")

        challans_list = []
        already_dispatched = 0.0

        for ch in challans_qs:
            lines_data = []
            ch_total_qty = 0.0
            rolls_count = 0
            for l in ch.line_items.all():
                q_val = float(l.qty)
                ch_total_qty += q_val
                t_alloc = l.taka_allocations or []
                rolls_count += len(t_alloc) if t_alloc else (l.taka_count or 1)
                lines_data.append({
                    "id": str(l.id),
                    "itemName": l.item_name or (l.item.name if l.item else "Fabric"),
                    "sku": l.sku or (l.item.sku if l.item else ""),
                    "qty": q_val,
                    "uom": l.uom or "Meters",
                    "rate": float(l.rate or 0),
                    "amount": float(l.amount or 0),
                    "takaAllocations": t_alloc,
                })

            if ch.status in ("Dispatched", "In Transit", "Delivered", "Partially Delivered"):
                already_dispatched += ch_total_qty

            challans_list.append({
                "id": str(ch.id),
                "challanNumber": ch.challan_number,
                "docDate": str(ch.doc_date),
                "dispatchDate": str(ch.dispatch_date or ch.doc_date),
                "status": ch.status,
                "customerName": ch.party_name or (ch.party.name if ch.party else project.customer_name),
                "deliveryLocation": ch.delivery_location or (project.party.shipping_address if project.party else ""),
                "transporter": ch.transporter or "",
                "vehicleNumber": ch.vehicle_number or "",
                "lrNumber": ch.lr_number or "",
                "ewayBillNumber": ch.eway_bill_number or "",
                "allocatedFreightCost": float(ch.allocated_freight_cost or 0),
                "notes": ch.notes or "",
                "totalQty": ch_total_qty,
                "rollsCount": rolls_count,
                "lines": lines_data,
            })

        # C. QC Hold / Rejected quantity from Stage 15 QC
        project_qcs = list(QCInspection.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(source_document_id__in=inward_ids) | Q(source_document_number__in=inward_nos)
            | Q(notes__icontains=project.code) | Q(source_document_number=project.code)
        ))
        qc_hold_rejected = sum((float((q.hold_qty or 0) + (q.rejected_qty or 0)) for q in project_qcs), 0.0)

        target_req = float(project.requirement_meters or project.quantity or 3000.0)
        eligible_for_dispatch = sum((t["availableQuantity"] for t in eligible_takas_list), 0.0)
        pending_dispatch = max(0.0, target_req - already_dispatched)
        dispatched_pct = min(100, int(round((already_dispatched / target_req) * 100))) if target_req > 0 else 0

        # Master data helpers
        active_locations = [
            {"id": str(loc.id), "code": loc.code, "name": loc.name}
            for loc in Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").order_by("code")
        ]

        return Response({
            "project": {
                "id": str(project.id),
                "code": project.code,
                "name": getattr(project, "project_name", None) or getattr(project, "name", None) or getattr(project, "title", None) or project.code,
                "targetRequirement": target_req,
                "uom": "Meters",
            },
            "customer": {
                "id": str(project.party.id) if project.party else None,
                "name": project.customer_name or (project.party.name if project.party else "Customer"),
                "shippingAddress": (getattr(project.party, "shipping_address", None) or getattr(project.party, "address", None) or "") if project.party else "",
                "gstin": getattr(project.party, "gstin", "") if project.party else "",
                "phone": getattr(project.party, "phone", "") if project.party else "",
            },
            "salesOrder": {
                "id": str(order.id) if order else None,
                "orderNumber": order.order_number if order else "—",
                "orderDate": str(order.doc_date) if order else "—",
                "stage": order.stage if order else "—",
            },
            "summary": {
                "targetRequirementMeters": target_req,
                "eligibleForDispatch": eligible_for_dispatch,
                "alreadyDispatched": already_dispatched,
                "pendingDispatch": pending_dispatch,
                "qcHoldRejectedQty": qc_hold_rejected,
                "dispatchedPercentage": dispatched_pct,
                "uom": "Meters",
            },
            "eligibleTakas": eligible_takas_list,
            "challans": challans_list,
            "locations": active_locations,
        })

    @action(detail=True, methods=["get", "post"], url_path="invoice-payment")
    @transaction.atomic
    def invoice_payment(self, request, pk=None):
        project = self.get_object()
        client_id = project.client_id
        from decimal import Decimal
        from django.db.models import Q, Sum
        from django.utils import timezone
        from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
        from apps.core.money import ZERO, D, round4, round2
        from apps.core.numbering import allocate_number
        from apps.sales.models import (
            ProformaInvoice, ProformaInvoiceLine,
            SalesInvoice, SalesInvoiceLine,
            DeliveryChallan, SalesOrder, SalesOrderLine,
            PaymentIn, PaymentAllocation
        )
        from apps.sales.services import recalculate_document, finalize_invoice, record_payment_in
        from apps.masters.models import Location, Party, Item
        from apps.accounting.models import BankAccount
        from apps.accounting.services import system_account
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        order = project.sales_order
        party = project.party or (order.party if order else None)
        if not party:
            party = Party.objects.filter(client_id=client_id, party_type__in=["Customer", "Both"]).first() or Party.objects.filter(client_id=client_id).first()

        main_wh = Location.objects.filter(client_id=client_id, code="WH-MAIN").first() or Location.objects.filter(client_id=client_id, is_active=True).exclude(code__startswith="SYS-").first()

        # Handle POST actions
        if request.method == "POST":
            action_type = request.data.get("action_type") or request.data.get("actionType")

            if action_type == "create_proforma":
                qty = round4(D(request.data.get("quantity") or request.data.get("qty") or 0))
                rate = round2(D(request.data.get("rate") or 0))
                if qty <= ZERO:
                    raise ValidationFailed("Quantity must be greater than zero.", field_errors={"quantity": ["Must be > 0."]})
                if rate <= ZERO:
                    raise ValidationFailed("Rate per unit must be greater than zero.", field_errors={"rate": ["Must be > 0."]})

                doc_date_str = request.data.get("doc_date") or request.data.get("docDate")
                doc_date = timezone.datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else timezone.localdate()

                valid_until_str = request.data.get("valid_until") or request.data.get("validUntil")
                valid_until = timezone.datetime.strptime(valid_until_str, "%Y-%m-%d").date() if valid_until_str else (doc_date + timezone.timedelta(days=30))

                tax_pct = round2(D(request.data.get("tax_pct") or request.data.get("taxPct") or 5.0))
                notes = request.data.get("notes") or request.data.get("remarks") or ""
                challan_id = request.data.get("challan_id") or request.data.get("challanId")
                dc = DeliveryChallan.objects.filter(client_id=client_id, id=challan_id).first() if challan_id else None

                item_id = request.data.get("item_id") or request.data.get("itemId")
                item = Item.objects.filter(client_id=client_id, id=item_id).first() if item_id else None
                if not item:
                    item = (dc.line_items.first().item if dc and dc.line_items.exists() else None) or Item.objects.filter(client_id=client_id).first()

                pi_number = allocate_number(client_id, "PI", doc_date)

                proforma = ProformaInvoice.objects.create(
                    client_id=client_id,
                    party=party,
                    party_name=party.name if party else (project.customer_name or "Customer"),
                    party_gstin=getattr(party, "gstin", None) or getattr(party, "gst_number", None),
                    sales_order=order,
                    doc_date=doc_date,
                    valid_until=valid_until,
                    proforma_number=pi_number,
                    status="Issued",
                    billing_address=getattr(party, "billing_address", None) or {},
                    shipping_address=getattr(party, "shipping_address", None) or {},
                    notes=f"Project: {project.code}. {f'Challan: {dc.challan_number}. ' if dc else ''}{notes}".strip(),
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                so_line = order.line_items.first() if order else None
                base_amt = round2(qty * rate)
                tax_amt = round2(base_amt * (tax_pct / Decimal("100.00")))
                line_tot = base_amt + tax_amt

                line = ProformaInvoiceLine.objects.create(
                    client_id=client_id,
                    proforma_invoice=proforma,
                    line_no=1,
                    item=item,
                    sku=item.sku if item else "",
                    item_name=item.name if item else (project.product_name or "Finished Fabric"),
                    uom=item.uom if item else "Meters",
                    qty=qty,
                    rate=rate,
                    amount=base_amt,
                    tax_pct=tax_pct,
                    tax_amount=tax_amt,
                    line_total=line_tot,
                    sales_order_line=so_line,
                )

                recalculate_document(proforma, lines=[line])
                proforma.status = "Issued"
                proforma.save(update_fields=["status", "subtotal", "total", "taxable_value", "total_tax", "cgst", "sgst", "igst", "updated_at"])

                sync_project_pipeline_stages(project)

                return Response({
                    "success": True,
                    "proforma_id": str(proforma.id),
                    "proforma_number": proforma.proforma_number,
                    "total": float(proforma.total),
                    "status": proforma.status,
                })

            elif action_type == "convert_proforma_to_final":
                proforma_id = request.data.get("proforma_id") or request.data.get("proformaId")
                if not proforma_id:
                    raise ValidationFailed("proforma_id is required.")

                proforma = ProformaInvoice.objects.select_for_update().filter(client_id=client_id, id=proforma_id, deleted_at__isnull=True).first()
                if not proforma:
                    raise NotFound("Proforma Invoice not found.")
                if proforma.status == "Converted":
                    raise Conflict("This Proforma Invoice has already been converted to a Final Invoice.", code="ALREADY_CONVERTED")
                if proforma.status in ("Cancelled", "Expired"):
                    raise Conflict(f"Cannot convert a {proforma.status.lower()} Proforma Invoice.", code="ALREADY_CANCELLED")

                # Validate previously invoiced quantity vs dispatched quantity to prevent over-invoicing
                challans_for_proj = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                    status__in=["Dispatched", "In Transit", "Delivered"]
                ).filter(Q(sales_order=order) | Q(notes__icontains=project.code))
                total_dispatched_qty = sum((float(getattr(ch, "total_quantity", 0) or ch.line_items.aggregate(s=Sum("qty"))["s"] or 0) for ch in challans_for_proj), 0.0)

                invoices_for_proj = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled").filter(
                    Q(sales_order=order) | Q(notes__icontains=project.code)
                )
                already_invoiced_qty = sum((float(inv.line_items.aggregate(s=Sum("qty"))["s"] or 0) for inv in invoices_for_proj), 0.0)

                proforma_lines = list(proforma.line_items.select_related("item", "sales_order_line").all())
                proforma_qty = sum((float(l.qty) for l in proforma_lines), 0.0)

                if total_dispatched_qty > 0 and (already_invoiced_qty + proforma_qty > total_dispatched_qty + 0.01):
                    raise BusinessRuleViolation(
                        f"Conversion exceeds dispatched stock: {round(already_invoiced_qty + proforma_qty, 2)} M requested to bill, but only {round(total_dispatched_qty, 2)} M dispatched in Stage 16.",
                        code="OVER_INVOICE"
                    )

                # Sync SalesOrderLine quantity if lower than project requirement to prevent false overflow
                if order:
                    for ol in order.line_items.all():
                        if ol.qty < (project.requirement_meters or 3000):
                            ol.qty = project.requirement_meters or 3000
                            ol.save(update_fields=["qty", "updated_at"])

                dc = DeliveryChallan.objects.filter(sales_order=order).first()
                target_loc = (dc.location if dc else None) or main_wh

                # Create Final SalesInvoice
                invoice = SalesInvoice.objects.create(
                    client_id=client_id,
                    party=proforma.party,
                    party_name=proforma.party_name,
                    party_gstin=proforma.party_gstin,
                    sales_order=proforma.sales_order,
                    delivery_challan=dc,
                    proforma_invoice=proforma,
                    location=target_loc,
                    doc_date=timezone.localdate(),
                    status="Draft",
                    notes=f"Project: {project.code}. Converted from Proforma {proforma.proforma_number}. {proforma.notes}".strip(),
                    billing_address=proforma.billing_address,
                    shipping_address=proforma.shipping_address,
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                created_inv_lines = []
                for idx, pl in enumerate(proforma_lines, start=1):
                    dc_line = dc.line_items.first() if dc else None
                    inv_line = SalesInvoiceLine.objects.create(
                        client_id=client_id,
                        sales_invoice=invoice,
                        line_no=idx,
                        item=pl.item,
                        sku=pl.sku,
                        item_name=pl.item_name,
                        uom=pl.uom,
                        qty=pl.qty,
                        rate=pl.rate,
                        amount=pl.amount,
                        tax_pct=pl.tax_pct,
                        tax_amount=pl.tax_amount,
                        line_total=pl.line_total,
                        sales_order_line=pl.sales_order_line,
                        delivery_challan_line=dc_line,
                    )
                    created_inv_lines.append(inv_line)

                recalculate_document(invoice, lines=created_inv_lines)
                override_credit = bool(
                    request.data.get('override_credit_limit') or
                    request.data.get('overrideCreditLimit') or
                    (request.user and (getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)))
                )
                finalize_invoice(invoice, user=request.user, override_credit_limit=override_credit)

                proforma.status = "Converted"
                proforma.save(update_fields=["status", "updated_at"])

                sync_project_pipeline_stages(project)

                invoice.refresh_from_db()
                return Response({
                    "success": True,
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "total": float(invoice.total),
                    "status": invoice.status,
                })

            elif action_type == "create_final_invoice":
                qty = round4(D(request.data.get("quantity") or request.data.get("qty") or 0))
                rate = round2(D(request.data.get("rate") or 0))
                if qty <= ZERO:
                    raise ValidationFailed("Quantity must be greater than zero.", field_errors={"quantity": ["Must be > 0."]})
                if rate <= ZERO:
                    raise ValidationFailed("Rate per unit must be greater than zero.", field_errors={"rate": ["Must be > 0."]})

                doc_date_str = request.data.get("doc_date") or request.data.get("docDate")
                doc_date = timezone.datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else timezone.localdate()

                tax_pct = round2(D(request.data.get("tax_pct") or request.data.get("taxPct") or 5.0))
                notes = request.data.get("notes") or request.data.get("remarks") or ""

                challan_id = request.data.get("challan_id") or request.data.get("challanId")
                dc = DeliveryChallan.objects.filter(client_id=client_id, id=challan_id).first() if challan_id else DeliveryChallan.objects.filter(sales_order=order).first()

                # Validate previously invoiced quantity vs dispatched quantity
                challans_for_proj = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                    status__in=["Dispatched", "In Transit", "Delivered"]
                ).filter(Q(sales_order=order) | Q(notes__icontains=project.code))
                total_dispatched_qty = sum((float(getattr(ch, "total_quantity", 0) or ch.line_items.aggregate(s=Sum("qty"))["s"] or 0) for ch in challans_for_proj), 0.0)

                invoices_for_proj = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Cancelled").filter(
                    Q(sales_order=order) | Q(notes__icontains=project.code)
                )
                already_invoiced_qty = sum((float(inv.line_items.aggregate(s=Sum("qty"))["s"] or 0) for inv in invoices_for_proj), 0.0)

                if total_dispatched_qty > 0 and (already_invoiced_qty + float(qty) > total_dispatched_qty + 0.01):
                    raise BusinessRuleViolation(
                        f"Invoicing exceeds dispatched stock: {round(already_invoiced_qty + float(qty), 2)} M requested to bill, but only {round(total_dispatched_qty, 2)} M dispatched in Stage 16.",
                        code="OVER_INVOICE"
                    )

                # Sync SalesOrderLine quantity if lower than project requirement
                if order:
                    for ol in order.line_items.all():
                        if ol.qty < (project.requirement_meters or 3000):
                            ol.qty = project.requirement_meters or 3000
                            ol.save(update_fields=["qty", "updated_at"])

                item = (dc.line_items.first().item if dc and dc.line_items.exists() else None) or Item.objects.filter(client_id=client_id).first()
                target_loc = (dc.location if dc else None) or main_wh

                invoice = SalesInvoice.objects.create(
                    client_id=client_id,
                    party=party,
                    party_name=party.name if party else (project.customer_name or "Customer"),
                    party_gstin=getattr(party, "gstin", None) or getattr(party, "gst_number", None),
                    sales_order=order,
                    delivery_challan=dc,
                    location=target_loc,
                    doc_date=doc_date,
                    status="Draft",
                    notes=f"Project: {project.code}. {f'Challan: {dc.challan_number}. ' if dc else ''}{notes}".strip(),
                    billing_address=getattr(party, "billing_address", None) or {},
                    shipping_address=getattr(party, "shipping_address", None) or {},
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                base_amt = round2(qty * rate)
                tax_amt = round2(base_amt * (tax_pct / Decimal("100.00")))
                line_tot = base_amt + tax_amt
                so_line = order.line_items.first() if order else None
                dc_line = dc.line_items.first() if dc else None

                line = SalesInvoiceLine.objects.create(
                    client_id=client_id,
                    sales_invoice=invoice,
                    line_no=1,
                    item=item,
                    sku=item.sku if item else "",
                    item_name=item.name if item else (project.product_name or "Finished Fabric"),
                    uom=item.uom if item else "Meters",
                    qty=qty,
                    rate=rate,
                    amount=base_amt,
                    tax_pct=tax_pct,
                    tax_amount=tax_amt,
                    line_total=line_tot,
                    sales_order_line=so_line,
                    delivery_challan_line=dc_line,
                )

                recalculate_document(invoice, lines=[line])
                override_credit = bool(
                    request.data.get('override_credit_limit') or
                    request.data.get('overrideCreditLimit') or
                    (request.user and (getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)))
                )
                finalize_invoice(invoice, user=request.user, override_credit_limit=override_credit)

                sync_project_pipeline_stages(project)

                invoice.refresh_from_db()
                return Response({
                    "success": True,
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "total": float(invoice.total),
                    "status": invoice.status,
                })

            elif action_type == "record_payment":
                invoice_id = request.data.get("invoice_id") or request.data.get("invoiceId")
                if not invoice_id:
                    raise ValidationFailed("invoice_id is required.")

                invoice = SalesInvoice.objects.select_for_update().filter(client_id=client_id, id=invoice_id, deleted_at__isnull=True).first()
                if not invoice:
                    raise NotFound("Sales Invoice not found.")
                if invoice.status == "Cancelled":
                    raise BusinessRuleViolation("Cannot record payment against a cancelled invoice.", code="PAYMENT_ON_CANCELLED")
                if invoice.posted_at is None or invoice.status == "Draft":
                    raise BusinessRuleViolation("Cannot record payment against a draft invoice. Finalize it first.", code="PAYMENT_ON_DRAFT")

                amount = round2(D(request.data.get("amount") or 0))
                if amount <= ZERO:
                    raise ValidationFailed("Payment amount must be greater than zero.", field_errors={"amount": ["Must be > 0."]})

                outstanding_balance = round2(invoice.total - (invoice.amount_paid or ZERO))
                if amount > outstanding_balance + Decimal("0.01"):
                    raise BusinessRuleViolation(
                        f"Payment amount (₹{amount}) exceeds remaining invoice balance (₹{outstanding_balance}).",
                        code="PAYMENT_EXCEEDS_BALANCE"
                    )

                pay_date_str = request.data.get("payment_date") or request.data.get("paymentDate")
                payment_date = timezone.datetime.strptime(pay_date_str, "%Y-%m-%d").date() if pay_date_str else timezone.localdate()

                mode = request.data.get("mode") or "Bank"
                if mode not in ["Cash", "Bank", "UPI", "Cheque", "Card", "NEFT/RTGS"]:
                    mode = "Bank"

                bank_acc_id = request.data.get("bank_account_id") or request.data.get("bankAccountId")
                bank_acc = BankAccount.objects.filter(client_id=client_id, id=bank_acc_id, is_active=True).first() if bank_acc_id else None
                if not bank_acc and mode != "Cash":
                    bank_acc = BankAccount.objects.filter(client_id=client_id, is_active=True).first()
                    if not bank_acc:
                        acc = system_account(client_id, "bank")
                        bank_acc = BankAccount.objects.create(
                            client=invoice.client,
                            account=acc,
                            name="Primary Corporate Account",
                            type="Current",
                            bank_name="Corporate Bank",
                            is_default=True,
                            is_active=True,
                        )

                ref_no = request.data.get("reference_number") or request.data.get("referenceNumber") or ""
                notes = request.data.get("notes") or request.data.get("remarks") or ""

                payment = record_payment_in(
                    client=invoice.client,
                    party=invoice.party,
                    amount=amount,
                    payment_date=payment_date,
                    mode=mode if mode != "NEFT/RTGS" else "Bank",
                    bank_account=bank_acc if mode != "Cash" else None,
                    reference_number=ref_no,
                    notes=f"Project: {project.code}. Invoice: {invoice.invoice_number}. {notes}".strip(),
                    invoice=invoice,
                    user=request.user,
                )

                sync_project_pipeline_stages(project)

                invoice.refresh_from_db()
                return Response({
                    "success": True,
                    "payment_id": str(payment.id),
                    "payment_number": payment.payment_number,
                    "amount": float(payment.amount),
                    "invoice_status": invoice.status,
                    "amount_paid": float(invoice.amount_paid),
                    "outstanding_balance": float(invoice.total - invoice.amount_paid),
                })

            elif action_type == "cancel_proforma":
                proforma_id = request.data.get("proforma_id") or request.data.get("proformaId")
                proforma = ProformaInvoice.objects.filter(client_id=client_id, id=proforma_id).first()
                if not proforma:
                    raise NotFound("Proforma Invoice not found.")
                if proforma.status == "Converted":
                    raise Conflict("Converted Proforma Invoice cannot be cancelled.")
                proforma.status = "Cancelled"
                proforma.save(update_fields=["status", "updated_at"])
                return Response({"success": True, "proforma_id": str(proforma.id), "status": proforma.status})

            raise ValidationFailed(f"Unknown action_type: {action_type}")

        # 2. GET: Build detailed Stage 17 Invoice & Payment Collection workspace dataset
        # A. Delivery Challans from Stage 16
        challans_for_proj = DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=order) | Q(notes__icontains=project.code)
        ).prefetch_related("line_items").order_by("-dispatch_date", "-created_at")

        total_dispatched_qty = 0.0
        dispatches_list = []
        for ch in challans_for_proj:
            ch_qty = float(getattr(ch, "total_quantity", 0) or ch.line_items.aggregate(s=Sum("qty"))["s"] or 0)
            if ch.status in ("Dispatched", "In Transit", "Delivered"):
                total_dispatched_qty += ch_qty
            dispatches_list.append({
                "id": str(ch.id),
                "challanNumber": ch.challan_number,
                "dispatchDate": str(ch.dispatch_date or ch.doc_date),
                "totalQty": ch_qty,
                "transporter": ch.transporter or "Direct",
                "vehicleNumber": ch.vehicle_number or "",
                "lrNumber": ch.lr_number or "",
                "status": ch.status,
            })

        # B. Proforma Invoices
        proformas_qs = ProformaInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=order) | Q(notes__icontains=project.code)
        ).prefetch_related("line_items__item").order_by("-doc_date", "-created_at")

        proformas_list = []
        total_proforma_amt = 0.0
        for pi in proformas_qs:
            p_lines = []
            for l in pi.line_items.all():
                p_lines.append({
                    "id": str(l.id),
                    "itemName": l.item_name or (l.item.name if l.item else "Fabric"),
                    "qty": float(l.qty),
                    "uom": l.uom or "Meters",
                    "rate": float(l.rate),
                    "amount": float(l.amount),
                    "taxPct": float(l.tax_pct),
                    "taxAmount": float(l.tax_amount),
                    "lineTotal": float(l.line_total),
                })
            p_tot = float(pi.total or 0)
            if pi.status != "Cancelled":
                total_proforma_amt += p_tot

            proformas_list.append({
                "id": str(pi.id),
                "proformaNumber": pi.proforma_number or f"PI-{str(pi.id)[:6]}",
                "docDate": str(pi.doc_date),
                "validUntil": str(pi.valid_until) if pi.valid_until else "",
                "status": pi.status,
                "total": p_tot,
                "subtotal": float(pi.subtotal or 0),
                "taxableValue": float(pi.taxable_value or 0),
                "totalTax": float(pi.total_tax or 0),
                "notes": pi.notes or "",
                "lines": p_lines,
            })

        # C. Final Invoices
        invoices_qs = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=order) | Q(notes__icontains=project.code)
        ).prefetch_related("line_items__item", "delivery_challan", "proforma_invoice").order_by("-doc_date", "-created_at")

        invoices_list = []
        invoice_ids = []
        inv_map = {}
        total_final_inv_amt = 0.0
        total_payment_rcvd = 0.0
        total_invoiced_qty = 0.0

        for inv in invoices_qs:
            invoice_ids.append(inv.id)
            inv_lines = []
            inv_m_qty = 0.0
            for l in inv.line_items.all():
                q_val = float(l.qty)
                inv_m_qty += q_val
                inv_lines.append({
                    "id": str(l.id),
                    "itemName": l.item_name or (l.item.name if l.item else "Fabric"),
                    "qty": q_val,
                    "uom": l.uom or "Meters",
                    "rate": float(l.rate),
                    "amount": float(l.amount),
                    "taxPct": float(l.tax_pct),
                    "taxAmount": float(l.tax_amount),
                    "lineTotal": float(l.line_total),
                })

            i_tot = float(inv.total or 0)
            i_paid = float(inv.amount_paid or 0)
            i_bal = max(0.0, i_tot - i_paid)

            if inv.status != "Cancelled":
                total_final_inv_amt += i_tot
                total_payment_rcvd += i_paid
                total_invoiced_qty += inv_m_qty

            inv_no = inv.invoice_number or f"INV-DRAFT-{str(inv.id)[:6]}"
            inv_map[str(inv.id)] = {"number": inv_no, "date": str(inv.doc_date)}

            invoices_list.append({
                "id": str(inv.id),
                "invoiceNumber": inv_no,
                "docDate": str(inv.doc_date),
                "dueDate": str(inv.due_date) if inv.due_date else "",
                "status": inv.status,
                "total": i_tot,
                "subtotal": float(inv.subtotal or 0),
                "taxableValue": float(inv.taxable_value or 0),
                "totalTax": float(inv.total_tax or 0),
                "amountPaid": i_paid,
                "outstandingBalance": i_bal,
                "challanNumber": inv.delivery_challan.challan_number if inv.delivery_challan else "",
                "proformaNumber": inv.proforma_invoice.proforma_number if inv.proforma_invoice else "",
                "journalEntryId": str(inv.journal_entry_id) if inv.journal_entry_id else None,
                "notes": inv.notes or "",
                "lines": inv_lines,
            })

        # D. Payment History
        allocs = PaymentAllocation.objects.filter(
            client_id=client_id, document_type="SalesInvoice", document_id__in=invoice_ids
        ).order_by("-allocated_at")

        payment_ids = [a.payment_id for a in allocs]
        payments_dict = {
            str(p.id): p
            for p in PaymentIn.objects.filter(client_id=client_id, id__in=payment_ids).select_related("bank_account", "party")
        }

        payments_list = []
        for a in allocs:
            p_obj = payments_dict.get(str(a.payment_id))
            inv_info = inv_map.get(str(a.document_id), {})
            payments_list.append({
                "id": str(a.id),
                "paymentId": str(a.payment_id),
                "paymentNumber": p_obj.payment_number if p_obj else f"PAY-{str(a.payment_id)[:6]}",
                "paymentDate": str(p_obj.payment_date if p_obj else a.allocated_at.date()),
                "invoiceId": str(a.document_id),
                "invoiceNumber": inv_info.get("number", "—"),
                "customerName": (p_obj.party.name if p_obj and p_obj.party else None) or (party.name if party else "Customer"),
                "mode": p_obj.mode if p_obj else "Bank",
                "amount": float(a.amount),
                "referenceNumber": p_obj.reference_number if p_obj else "",
                "bankAccountName": (p_obj.bank_account.name if p_obj and p_obj.bank_account else None) or "Cash / Bank",
                "status": p_obj.status if p_obj else "Active",
            })

        # Active bank accounts
        banks = [
            {"id": str(b.id), "name": b.name, "bankName": b.bank_name, "accountNumber": b.account_number}
            for b in BankAccount.objects.filter(client_id=client_id, is_active=True).order_by("name")
        ]

        total_outstanding = max(0.0, total_final_inv_amt - total_payment_rcvd)
        target_req = float(project.requirement_meters or project.quantity or 3000.0)
        pending_invoicing_qty = max(0.0, total_dispatched_qty - total_invoiced_qty)

        return Response({
            "project": {
                "id": str(project.id),
                "code": project.code,
                "name": getattr(project, "project_name", None) or getattr(project, "name", None) or getattr(project, "title", None) or project.code,
                "targetRequirement": target_req,
                "uom": "Meters",
            },
            "customer": {
                "id": str(party.id) if party else None,
                "name": party.name if party else (project.customer_name or "Customer"),
                "shippingAddress": (getattr(party, "shipping_address", None) or getattr(party, "address", None) or "") if party else "",
                "gstin": getattr(party, "gstin", "") if party else "",
                "phone": getattr(party, "phone", "") if party else "",
                "currentBalance": float(getattr(party, "balance", 0) or 0) if party else 0.0,
            },
            "salesOrder": {
                "id": str(order.id) if order else None,
                "orderNumber": order.order_number if order else "—",
                "orderDate": str(order.doc_date) if order else "—",
                "stage": order.stage if order else "—",
                "total": float(order.total or 0) if order else 0.0,
            },
            "summary": {
                "targetRequirementMeters": target_req,
                "totalDispatchedQty": total_dispatched_qty,
                "totalProformaAmount": total_proforma_amt,
                "totalFinalInvoiceAmount": total_final_inv_amt,
                "totalPaymentReceived": total_payment_rcvd,
                "totalOutstandingBalance": total_outstanding,
                "invoicedQuantity": total_invoiced_qty,
                "pendingInvoicingQty": pending_invoicing_qty,
                "uom": "Meters",
            },
            "dispatches": dispatches_list,
            "proformas": proformas_list,
            "finalInvoices": invoices_list,
            "payments": payments_list,
            "bankAccounts": banks,
        })

    @action(detail=True, methods=["get", "post"], url_path="inventory-documents")
    @transaction.atomic
    def inventory_documents(self, request, pk=None):
        """Unified PMS Project Inventory & Documents Workspace.
        Provides stage-based document versioning, client proofing and approval tracking,
        along with complete fabric stock traceability (PO, GRN, JWO, Inward, QC, Customer/Own allocation).
        """
        project = self.get_object()
        client_id = project.client_id
        import secrets
        from decimal import Decimal
        from django.db.models import Q, Sum
        from django.utils import timezone
        from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
        from apps.core.files import public_url
        from apps.core.models import File
        from apps.pms.models import Document, Approval, ProofShare, ProjectStage
        from apps.purchase.models import PurchaseOrder, PurchaseBill, GoodsReceipt
        from apps.jobwork.models import ProcessPlan, JobWorkOrder, JWOOutward, JWOInward, JWOInwardTaka, JWOOutwardTaka
        from apps.inventory.models import QCInspection, LotTaka, GreyStockAllocation
        from apps.sales.models import DeliveryChallan, SalesInvoice, ProformaInvoice

        if request.method == "POST":
            action_type = request.data.get("action_type") or request.data.get("actionType")

            if action_type == "upload_document":
                stage_id = request.data.get("stage_id") or request.data.get("stageId")
                if not stage_id:
                    raise ValidationFailed("stage_id is required.")
                stage = project.stages.filter(id=stage_id, deleted_at__isnull=True).first()
                if not stage:
                    raise NotFound("Stage not found on this project.")

                file_name = (request.data.get("file_name") or request.data.get("fileName") or "document.pdf").strip()
                file_size = int(request.data.get("file_size") or request.data.get("fileSize") or 0)
                doc_type = (request.data.get("doc_type") or request.data.get("docType") or "Design Proof").strip()
                raw_comments = (request.data.get("comments") or "").strip()
                is_proof = bool(request.data.get("is_proof", True) if doc_type in ["Design Proof", "Fabric Photograph", "Shade Sample"] else request.data.get("is_proof", False))
                preview_url = request.data.get("preview_url") or request.data.get("file_data") or request.data.get("fileData")

                doc_key = request.data.get("doc_key") or request.data.get("docKey")
                if not doc_key:
                    doc_key = file_name.rsplit(".", 1)[0] if "." in file_name else file_name

                # Store doc_type in comments header [DOC_TYPE] Notes
                stored_comments = f"[{doc_type}] {raw_comments}".strip()

                # Find or create core.File
                file_id = request.data.get("file_id") or request.data.get("fileId")
                file_obj = File.objects.filter(client_id=client_id, id=file_id).first() if file_id else None
                if not file_obj:
                    content_type = "application/pdf" if file_name.lower().endswith(".pdf") else "image/png" if file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) else "application/octet-stream"
                    file_obj = File.objects.create(
                        client=project.client,
                        scope="pms_document",
                        storage_key=f"pms/{project.code}/{stage.id}/{secrets.token_hex(6)}_{file_name}",
                        file_name=file_name,
                        content_type=content_type,
                        file_size=file_size,
                        status="committed",
                        uploaded_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                    )

                # Versioning
                previous = Document.objects.filter(stage=stage, doc_key=doc_key, deleted_at__isnull=True).order_by("-version").first()
                version = (previous.version + 1) if previous else 1
                revision_reason = previous.revision_reason if previous else None
                if previous is not None:
                    Document.objects.filter(stage=stage, doc_key=doc_key).update(is_current=False)

                doc = Document.objects.create(
                    client_id=client_id,
                    project=project,
                    stage=stage,
                    doc_key=doc_key,
                    version=version,
                    file=file_obj,
                    file_name=file_name,
                    file_size=file_size or getattr(file_obj, "file_size", 0),
                    preview_url=preview_url if isinstance(preview_url, str) and len(preview_url) < 500000 else None,
                    uploaded_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                    comments=stored_comments,
                    revision_reason=revision_reason,
                    approval_status="Pending" if is_proof else "Approved",
                    is_current=True,
                    is_proof=is_proof,
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                record_audit(
                    client=client_id,
                    actor=request.user if getattr(request.user, "is_authenticated", False) else None,
                    action="DOCUMENT_UPLOADED",
                    entity_type="PmsDocument",
                    entity_id=doc.id,
                    entity_label=doc.file_name,
                    description=f"Uploaded {doc_type} v{version} to {stage.name}",
                )

                return Response({
                    "success": True,
                    "document_id": str(doc.id),
                    "file_name": doc.file_name,
                    "version": doc.version,
                    "stage_id": str(stage.id),
                    "approval_status": doc.approval_status,
                })

            elif action_type == "send_to_client":
                document_id = request.data.get("document_id") or request.data.get("documentId")
                document = Document.objects.filter(client_id=client_id, id=document_id, deleted_at__isnull=True).first()
                if not document:
                    raise NotFound("Document not found.")

                approver_name = (request.data.get("approver_name") or request.data.get("approverName") or project.customer_name or "Client").strip()
                approver_email = (request.data.get("approver_email") or request.data.get("approverEmail") or "").strip()

                approval = Approval.objects.create(
                    client_id=client_id,
                    project=project,
                    stage=document.stage,
                    document=document,
                    approver_type="Client",
                    approver_name=approver_name,
                    requested_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                    status="Pending",
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )
                document.approval_status = "Pending"
                document.save(update_fields=["approval_status", "updated_at"])

                if document.stage.status == "In Progress":
                    document.stage.status = "Under Review"
                    document.stage.save(update_fields=["status", "updated_at"])

                record_audit(
                    client=client_id,
                    actor=request.user if getattr(request.user, "is_authenticated", False) else None,
                    action="APPROVAL_REQUESTED",
                    entity_type="PmsApproval",
                    entity_id=approval.id,
                    entity_label=document.file_name,
                    description=f"Circulated to {approver_name} for client sign-off",
                )

                return Response({
                    "success": True,
                    "approval_id": str(approval.id),
                    "document_id": str(document.id),
                    "status": "Pending",
                    "approver_name": approver_name,
                })

            elif action_type == "generate_link":
                document_id = request.data.get("document_id") or request.data.get("documentId")
                document = Document.objects.filter(client_id=client_id, id=document_id, deleted_at__isnull=True).first()
                if not document:
                    raise NotFound("Document not found.")

                recipient_name = (request.data.get("recipient_name") or request.data.get("recipientName") or project.customer_name or "Client").strip()
                recipient_email = (request.data.get("recipient_email") or request.data.get("recipientEmail") or "").strip()
                expiry_days = int(request.data.get("expiry_days") or request.data.get("expiryDays") or 14)

                token = secrets.token_urlsafe(32)
                share = ProofShare.objects.create(
                    client_id=client_id,
                    document=document,
                    project=project,
                    token_hash=hash_token(token),
                    recipient_name=recipient_name,
                    recipient_email=recipient_email,
                    expires_at=timezone.now() + timezone.timedelta(days=expiry_days),
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                record_audit(
                    client=client_id,
                    actor=request.user if getattr(request.user, "is_authenticated", False) else None,
                    action="PROOF_SHARED",
                    entity_type="PmsProofShare",
                    entity_id=share.id,
                    entity_label=document.file_name,
                    description=f"Generated shareable approval link for {recipient_name}",
                )

                return Response({
                    "success": True,
                    "token": token,
                    "url": f"/pms/approve/{token}",
                    "expiresAt": str(share.expires_at),
                    "recipientName": recipient_name,
                })

            elif action_type == "record_client_decision":
                document_id = request.data.get("document_id") or request.data.get("documentId")
                document = Document.objects.filter(client_id=client_id, id=document_id, deleted_at__isnull=True).first()
                if not document:
                    raise NotFound("Document not found.")

                decision = request.data.get("decision") or "Approved"
                if decision not in ("Approved", "Need Improvement", "Rejected"):
                    decision = "Approved"
                comments = request.data.get("comments") or ""
                revision_reason = request.data.get("revision_reason") or request.data.get("revisionReason") or ""
                decided_by = request.data.get("decided_by") or request.data.get("decidedBy") or project.customer_name or "Client"

                apply_document_decision(
                    document=document,
                    decision=decision if decision != "Rejected" else "Need Improvement",
                    comments=comments,
                    revision_reason=revision_reason,
                    decided_by_label=decided_by,
                    user=request.user if getattr(request.user, "is_authenticated", False) else None,
                )

                document.refresh_from_db()
                return Response({
                    "success": True,
                    "document_id": str(document.id),
                    "approval_status": document.approval_status,
                    "stage_status": document.stage.status,
                    "decision": decision,
                })

            raise ValidationFailed(f"Unknown action_type: {action_type}")

        # -------------------------------------------------------------
        # GET: Compile comprehensive Inventory & Documents payload
        # -------------------------------------------------------------
        # 1. Project info
        target_req = float(project.requirement_meters or project.quantity or 3000.0)
        proj_data = {
            "id": str(project.id),
            "code": project.code,
            "name": getattr(project, "product_name", None) or project.code,
            "customerName": project.customer_name or (project.party.name if project.party else "Customer"),
            "requirementMeters": target_req,
            "productName": project.product_name or "Fabric",
            "colorShade": (project.specifications.get("color_shade") if isinstance(project.specifications, dict) else None) or "Standard",
            "fabricQuality": (project.specifications.get("fabric_quality") if isinstance(project.specifications, dict) else None) or project.product_name or "Cotton Cambric 60x60",
            "designNo": (project.specifications.get("design_no") if isinstance(project.specifications, dict) else None) or "—",
            "status": project.status,
        }

        # 2. Stages
        stages_qs = project.stages.filter(deleted_at__isnull=True).order_by("sequence")
        stages_list = [
            {
                "id": str(s.id),
                "name": s.name,
                "sequence": s.sequence,
                "status": s.status,
                "department": s.department.name if s.department else "Operations",
                "documentCount": s.documents.filter(deleted_at__isnull=True).count(),
            }
            for s in stages_qs
        ]

        # 3. Documents & Proofs
        docs_qs = Document.objects.filter(project=project, deleted_at__isnull=True).select_related(
            "stage", "uploaded_by", "file"
        ).prefetch_related("shares", "approvals").order_by("-uploaded_at")

        docs_list = []
        for d in docs_qs:
            raw_comments = d.comments or ""
            doc_type = "Design Proof" if d.is_proof else "Document"
            clean_comments = raw_comments
            if raw_comments.startswith("[") and "]" in raw_comments:
                doc_type = raw_comments[1:raw_comments.index("]")]
                clean_comments = raw_comments[raw_comments.index("]") + 1:].strip()

            active_shares = [s for s in d.shares.all() if not s.deleted_at]
            latest_share = sorted(active_shares, key=lambda s: s.created_at, reverse=True)[0] if active_shares else None
            if latest_share:
                if latest_share.decision:
                    sharing_status = f"Decided: {latest_share.decision}"
                elif latest_share.status == "Revoked":
                    sharing_status = "Link Revoked"
                elif latest_share.expires_at and latest_share.expires_at < timezone.now():
                    sharing_status = "Link Expired"
                else:
                    sharing_status = "Link Active"
            elif any(a.status == "Pending" for a in d.approvals.all()):
                sharing_status = "Sent to Client"
            else:
                sharing_status = "Not Sent"

            preview_url = d.preview_url
            if not preview_url and d.file_id:
                try:
                    preview_url = public_url(d.file, request)
                except Exception:
                    preview_url = None

            docs_list.append({
                "id": str(d.id),
                "docKey": d.doc_key,
                "fileName": d.file_name,
                "fileSize": d.file_size or 0,
                "fileType": getattr(d.file, "content_type", None) or ("application/pdf" if d.file_name.lower().endswith(".pdf") else "image/png"),
                "previewUrl": preview_url,
                "version": d.version,
                "docType": doc_type,
                "stageId": str(d.stage_id),
                "stageName": d.stage.name if d.stage else "\u2014",
                "stageSeq": d.stage.sequence if d.stage else 0,
                "uploadedBy": {
                    "id": str(d.uploaded_by_id) if d.uploaded_by_id else None,
                    "name": d.uploaded_by.name if d.uploaded_by else "Admin",
                },
                "uploadedAt": str(d.uploaded_at),
                "comments": clean_comments,
                "revisionReason": d.revision_reason or "",
                "approvalStatus": d.approval_status or "Pending",
                "isCurrent": d.is_current,
                "isProof": d.is_proof,
                "sharingStatus": sharing_status,
                "shares": [
                    {
                        "id": str(s.id),
                        "recipientName": s.recipient_name,
                        "recipientEmail": s.recipient_email,
                        "status": s.status,
                        "decision": s.decision,
                        "decidedAt": str(s.decided_at) if s.decided_at else "",
                        "decidedBy": s.decided_by or "",
                        "expiresAt": str(s.expires_at) if s.expires_at else "",
                    }
                    for s in active_shares
                ],
            })

        # 4. Automatically Linked ERP Transaction Documents
        linked_tx = []
        pos = PurchaseOrder.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(project=project) | Q(reference_number=project.code) | Q(notes__icontains=project.code)
        ).exclude(purchase_type="Final Job Work").order_by("-created_at")
        for po in pos:
            linked_tx.append({
                "id": str(po.id),
                "number": po.po_number,
                "docDate": str(po.order_date or po.created_at.date()),
                "docType": "Purchase Order",
                "stageName": "Stage 2 \u2014 Supplier PO",
                "party": po.party.name if po.party else "Supplier",
                "quantity": float(getattr(po, "total_quantity", 0) or 0),
                "status": po.status,
            })

        grns = GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(purchase_order__project=project) | Q(purchase_order__notes__icontains=project.code)
        ).order_by("-created_at")
        for g in grns:
            linked_tx.append({
                "id": str(g.id),
                "number": g.grn_number,
                "docDate": str(g.receipt_date or g.created_at.date()),
                "docType": "Goods Receipt Note (GRN)",
                "stageName": "Stage 3 \u2014 GRN Inward",
                "party": g.party.name if g.party else "Supplier",
                "quantity": 0.0,
                "status": g.qc_status or "Received",
            })

        qcs = QCInspection.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(notes__icontains=project.code) | Q(source_document_number__icontains=project.code) | Q(purchase_order__project=project)
        ).order_by("-inspection_date", "-created_at")
        for q in qcs:
            is_fg = q.source_document_type in ["JWO_INWARD", "JobWorkInward"] or (q.inspection_type and "Finished" in q.inspection_type)
            linked_tx.append({
                "id": str(q.id),
                "number": q.inspection_number,
                "docDate": str(q.inspection_date or q.created_at.date()),
                "docType": "Finished Goods QC Report" if is_fg else "Grey QC Inspection",
                "stageName": "Stage 15 \u2014 FG QC & Allocation" if is_fg else "Stage 5 \u2014 Grey QC",
                "party": "Quality Assurance",
                "quantity": float(q.passed_qty or q.total_inspected_qty or 0),
                "status": q.status or "Completed",
            })

        bills = PurchaseBill.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(purchase_order__project=project) | Q(notes__icontains=project.code)
        ).order_by("-created_at")
        for b in bills:
            linked_tx.append({
                "id": str(b.id),
                "number": b.bill_number,
                "docDate": str(b.bill_date or b.created_at.date()),
                "docType": "Vendor Purchase Bill",
                "stageName": "Stage 7 \u2014 Bill Matching",
                "party": b.party.name if b.party else "Supplier",
                "quantity": float(b.total_amount or 0),
                "status": b.status or "Matched",
            })

        plans = ProcessPlan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(project=project) | Q(remarks__icontains=project.code) | Q(plan_no__icontains=project.code)
        ).order_by("-created_at")
        for pl in plans:
            linked_tx.append({
                "id": str(pl.id),
                "number": pl.plan_no,
                "docDate": str(getattr(pl, "date", None) or pl.created_at.date()),
                "docType": "Processing Plan",
                "stageName": "Stage 11 \u2014 Processing Plan",
                "party": getattr(pl, "vendor_name", None) or "Processor",
                "quantity": float(getattr(pl, "expected_qty", 0) or 0),
                "status": pl.status or "Active",
            })

        jwos = list(JobWorkOrder.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(project=project) | Q(remarks__icontains=project.code) | Q(jwo_no__icontains=project.code)
        ))
        outwards = list(JWOOutward.objects.filter(client_id=client_id, job_work_order__in=jwos, deleted_at__isnull=True).select_related("job_work_order", "job_work_order__vendor"))
        for o in outwards:
            linked_tx.append({
                "id": str(o.id),
                "number": o.no or o.challan_no or f"OUT-{str(o.id)[:6]}",
                "docDate": str(o.date or o.created_at.date()),
                "docType": "Outward Delivery Challan",
                "stageName": "Stage 12 \u2014 Job Work Outward",
                "party": o.job_work_order.vendor.name if o.job_work_order and o.job_work_order.vendor else "Processor",
                "quantity": float(o.qty or 0),
                "status": o.status or "Dispatched",
            })

        inwards = list(JWOInward.objects.filter(client_id=client_id, job_work_order__in=jwos, deleted_at__isnull=True).select_related("job_work_order", "job_work_order__vendor"))
        for i in inwards:
            linked_tx.append({
                "id": str(i.id),
                "number": i.no or i.challan_no or f"INW-{str(i.id)[:6]}",
                "docDate": str(i.date or i.created_at.date()),
                "docType": "Inward Goods Receipt",
                "stageName": "Stage 14 \u2014 Job Work Inward",
                "party": i.job_work_order.vendor.name if i.job_work_order and i.job_work_order.vendor else "Processor",
                "quantity": float(i.qty or 0),
                "status": i.status or "Received",
            })

        challans = list(DeliveryChallan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=project.sales_order) | Q(notes__icontains=project.code)
        ).order_by("-dispatch_date", "-created_at"))
        for ch in challans:
            c_qty = float(getattr(ch, "total_quantity", 0) or (ch.line_items.aggregate(s=Sum("qty"))["s"] or 0))
            linked_tx.append({
                "id": str(ch.id),
                "number": ch.challan_number,
                "docDate": str(ch.dispatch_date or ch.doc_date),
                "docType": "Customer Delivery Challan",
                "stageName": "Stage 16 \u2014 Customer Dispatch",
                "party": ch.party.name if ch.party else (project.customer_name or "Customer"),
                "quantity": c_qty,
                "status": ch.status or "Dispatched",
            })

        proformas = list(ProformaInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=project.sales_order) | Q(notes__icontains=project.code)
        ).order_by("-doc_date", "-created_at"))
        for pi in proformas:
            linked_tx.append({
                "id": str(pi.id),
                "number": pi.proforma_number,
                "docDate": str(pi.doc_date),
                "docType": "Proforma Invoice",
                "stageName": "Stage 17 \u2014 Invoice & Payment",
                "party": pi.party_name or (project.customer_name or "Customer"),
                "quantity": float(pi.total or 0),
                "status": pi.status,
            })

        invoices = list(SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(sales_order=project.sales_order) | Q(notes__icontains=project.code)
        ).order_by("-doc_date", "-created_at"))
        for inv in invoices:
            linked_tx.append({
                "id": str(inv.id),
                "number": inv.invoice_number,
                "docDate": str(inv.doc_date),
                "docType": "GST Tax Invoice",
                "stageName": "Stage 17 \u2014 Invoice & Payment",
                "party": inv.party_name or (project.customer_name or "Customer"),
                "quantity": float(inv.total or 0),
                "status": inv.status,
            })

        # 5. Inventory & Stock Traceability
        grey_alloc = float(getattr(project, "grey_allocated_meters", None) or (project.specifications.get("grey_stock_allocated_meters") if isinstance(project.specifications, dict) else None) or 0)
        wip_sent = sum((float(getattr(o, "qty", 0) or 0) for o in outwards), 0.0)
        wip_rcvd = sum((float(getattr(i, "qty", 0) or 0) for i in inwards), 0.0)
        wip_pending = max(0.0, wip_sent - wip_rcvd)
        dispatched_total = sum((float(getattr(ch, "total_quantity", 0) or (ch.line_items.aggregate(s=Sum("qty"))["s"] or 0)) for ch in challans if ch.status in ("Dispatched", "In Transit", "Delivered")), 0.0)
        cust_alloc = float(getattr(project, "customer_project_allocated_meters", None) or getattr(project, "customer_project_total_meters", None) or getattr(project, "customer_project_received_meters", None) or wip_rcvd)
        own_alloc = float(getattr(project, "own_project_allocated_meters", None) or getattr(project, "own_project_total_meters", None) or getattr(project, "own_project_received_meters", None) or 0)
        qc_passed = float(getattr(project, "customer_project_received_meters", None) or wip_rcvd)
        avail_unalloc = max(0.0, qc_passed - dispatched_total)

        inv_summary = {
            "targetRequirementMeters": target_req,
            "greyAllocatedMeters": grey_alloc,
            "wipAtProcessorsMeters": wip_pending,
            "fgReceivedMeters": wip_rcvd,
            "qcPassedMeters": qc_passed,
            "dispatchedMeters": dispatched_total,
            "customerAllocatedMeters": cust_alloc,
            "ownAllocatedMeters": own_alloc,
            "availableUnallocatedMeters": avail_unalloc,
            "uom": "Meters",
        }

        # Traceable Lots & Takas
        jwo_no_list = [j.jwo_no for j in jwos if j.jwo_no]
        inw_no_list = [i.no for i in inwards if i.no]
        primary_takas = list(LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
            Q(source_document_number__in=inw_no_list) |
            Q(source_document_number__in=jwo_no_list) |
            Q(source_document_number__icontains=project.code) |
            Q(lot_number__icontains=project.code) |
            Q(lot_number__in=[f"LOT-{n}" for n in inw_no_list]) |
            Q(remarks__icontains=project.code)
        ).select_related("item", "location", "supplier"))

        if not primary_takas:
            primary_takas = list(LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related("item", "location", "supplier")[:15])

        first_plan_no = plans[0].plan_no if plans else "\u2014"
        first_jwo_no = jwos[0].jwo_no if jwos else "\u2014"
        first_out_no = outwards[0].no or outwards[0].challan_no if outwards else "\u2014"
        first_inw_no = inwards[0].no or inwards[0].challan_no if inwards else "\u2014"
        first_po_no = pos[0].po_number if pos else "\u2014"
        first_grn_no = grns[0].grn_number if grns else "\u2014"

        traceable_lots = []
        for t in primary_takas:
            t_qty = float(t.available_quantity or t.received_quantity or 0)
            traceable_lots.append({
                "id": str(t.id),
                "lotNumber": t.lot_number,
                "takaNumber": t.taka_number,
                "fabricItem": t.item.name if t.item else (t.fabric_quality or project.product_name or "Fabric"),
                "quality": t.fabric_quality or project.product_name or "Cotton Cambric 60x60",
                "design": t.design or "—",
                "shade": t.shade or "Standard",
                "sourcePO": getattr(t, "source_document_number", None) or first_po_no,
                "grnRef": first_grn_no,
                "physicalQty": t_qty,
                "allocatedQty": t_qty if t.status in ("Allocated", "Reserved") else 0.0,
                "customerProjectAllocated": t_qty if cust_alloc > 0 else 0.0,
                "ownProjectAllocated": 0.0,
                "processingPlanRef": first_plan_no,
                "jwoRef": first_jwo_no,
                "outwardChallanRef": first_out_no,
                "inwardReceiptRef": first_inw_no,
                "qcStatus": t.qc_status or "Passed",
                "location": t.location.name if t.location else "Main Godown",
                "stockStatus": t.status or "Available",
                "uom": t.uom or "Meters",
            })

        return Response({
            "project": proj_data,
            "stages": stages_list,
            "documents": docs_list,
            "linkedTransactions": linked_tx,
            "inventory": {
                "summary": inv_summary,
                "traceableLots": traceable_lots,
            },
        })

    @action(detail=True, methods=["get", "post"], url_path="stages")
    @transaction.atomic
    def stages(self, request, pk=None):
        project = self.get_object()
        if request.method == "GET":
            context = self._detail_context(project)
            return Response(
                envelope(
                    ProjectStageSerializer(
                        context["stages"], many=True, context=context
                    ).data
                )
            )

        # Permission check: Only project creator can add stages
        if (
            getattr(request.user, "is_authenticated", False)
            and not getattr(request.user, "is_superuser", False)
            and project.created_by_id is not None
            and project.created_by_id != request.user.id
        ):
            raise PermissionDenied("Only the project creator can add stages to this project.")

        serializer = ProjectStageSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        last = project.stages.order_by("-sequence").values_list("sequence", flat=True).first()
        weight_val = (
            request.data.get("percentage")
            or request.data.get("weightPct")
            or request.data.get("weight")
            or 0
        )
        stage = serializer.save(
            client_id=request.client_id,
            project=project,
            sequence=(last or 0) + 1,
            weight_pct=Decimal(str(weight_val)),
            created_by=request.user,
        )

        stage_weights = request.data.get("stagePercentages") or request.data.get("stageWeights")
        if stage_weights:
            for s in project.stages.filter(deleted_at__isnull=True).exclude(pk=stage.id):
                sid = str(s.id)
                if sid in stage_weights:
                    s.weight_pct = Decimal(str(stage_weights[sid]))
                    s.save(update_fields=["weight_pct", "updated_at"])

        if project.current_stage_id is None:
            project.current_stage = stage
            project.current_department = stage.department
            project.status = "In Progress" if project.status == "Draft" else project.status
            project.save(update_fields=["current_stage", "current_department", "status", "updated_at"])

        services.recalculate_project(project)
        record_audit(
            client=project.client_id,
            actor=request.user,
            action="STAGES_CONFIGURED",
            entity_type="PmsProject",
            entity_id=project.id,
            entity_label=project.code,
            description=f"Stage '{stage.name}' added with {stage.weight_pct}% weight",
        )
        return Response(
            ProjectStageSerializer(stage, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["patch", "delete"], url_path=r"stages/(?P<stage_id>[^/.]+)")
    @transaction.atomic
    def stage_detail(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)

        if request.method == "DELETE":
            stage_name = stage.name
            stage.delete()
            services.recalculate_project(project)
            record_audit(
                client=request.client_id, actor=request.user, action="STAGE_DELETED",
                entity_type="PmsStage", entity_id=stage.id, entity_label=stage_name,
                description=f"Stage '{stage_name}' removed from project",
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

        # If modifying percentage, gate on project creator
        if any(k in request.data for k in ("percentage", "weightPct", "weight", "weight_pct")):
            if (
                getattr(request.user, "is_authenticated", False)
                and not getattr(request.user, "is_superuser", False)
                and project.created_by_id is not None
                and project.created_by_id != request.user.id
            ):
                raise PermissionDenied("Only the project creator can modify stage percentages.")

        # Completing goes through the handoff gate so documents, approvals and
        # open delays still block it (api.md §10.3).
        if isinstance(request.data, dict) and request.data.get("status") == "Completed":
            result = services.handoff_stage(stage, user=request.user, force=False)
            project.refresh_from_db()
            return Response(
                ProjectStageSerializer(
                    result["stage"], context=self.get_serializer_context()
                ).data
            )

        previous_status = stage.status
        serializer = ProjectStageSerializer(
            stage, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if any(k in request.data for k in ("percentage", "weightPct", "weight", "weight_pct")):
            services.recalculate_project(project)

        if previous_status != stage.status:
            record_audit(
                client=request.client_id, actor=request.user, action="STAGE_STATUS_CHANGED",
                entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
                description=f"Status changed to {stage.status}",
                from_value=previous_status, to_value=stage.status,
            )

        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/assign")
    @transaction.atomic
    def assign_stage(self, request, pk=None, stage_id=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "assign_stage")

        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        serializer = AssignStageSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("departmentId"):
            stage.department_id = data["departmentId"]
        if "assignedTeam" in data:
            stage.assigned_team = data["assignedTeam"]
        if data.get("assignedUserId"):
            stage.assigned_user_id = data["assignedUserId"]
        if data.get("plannedDuration") is not None:
            stage.planned_duration = data["plannedDuration"]
        if data.get("durationUnit"):
            stage.duration_unit = data["durationUnit"]
        if data.get("startDateTime"):
            stage.start_datetime = data["startDateTime"]

        stage.expected_completion_datetime = services.stage_expected_completion(
            stage.start_datetime, stage.planned_duration, stage.duration_unit
        )
        if stage.status == "Not Started" and stage.assigned_user_id:
            stage.status = "Assigned"
        stage.save()

        record_audit(
            client=request.client_id,
            actor=request.user,
            action="STAGE_ASSIGNED",
            entity_type="PmsStage",
            entity_id=stage.id,
            entity_label=stage.name,
            description=f"Assigned to {stage.assigned_user.name if stage.assigned_user_id else stage.assigned_team}",
        )
        if stage.assigned_user_id:
            notify(
                client=request.client_id,
                recipients=[stage.assigned_user_id],
                type="pms.stage_assigned",
                category="pms",
                title=f"You have been assigned {stage.name}",
                body=f"Project {project.code}.",
                entity_type="PmsStage",
                entity_id=stage.id,
                actor=request.user,
            )
        return Response(
            ProjectStageSerializer(stage, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/start")
    @transaction.atomic
    def start_stage(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        if stage.status not in ("Not Started", "Assigned"):
            raise Conflict(
                f"A {stage.status} stage cannot be started.", code=Codes.BAD_TARGET
            )

        previous = stage.status
        stage.status = "In Progress"
        stage.actual_start_datetime = timezone.now()
        if stage.start_datetime is None:
            stage.start_datetime = stage.actual_start_datetime
            stage.expected_completion_datetime = services.stage_expected_completion(
                stage.start_datetime, stage.planned_duration, stage.duration_unit
            )
        stage.save()

        if project.status == "Draft":
            project.status = "In Progress"
            project.save(update_fields=["status", "updated_at"])

        record_audit(
            client=request.client_id, actor=request.user, action="STAGE_STARTED",
            entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
            description=f"{stage.name} started", from_value=previous, to_value="In Progress",
        )
        return Response(
            ProjectStageSerializer(stage, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/progress")
    @transaction.atomic
    def stage_progress(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        serializer = ProgressSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        previous = stage.completion_pct
        stage.completion_pct = serializer.validated_data["pct"]
        stage.save(update_fields=["completion_pct", "updated_at"])
        services.recalculate_project(project)

        record_audit(
            client=request.client_id, actor=request.user, action="PROGRESS_UPDATED",
            entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
            description=f"Progress set to {stage.completion_pct}%",
            from_value=previous, to_value=stage.completion_pct,
        )
        project.refresh_from_db()
        return Response(
            {
                "stage": ProjectStageSerializer(
                    stage, context=self.get_serializer_context()
                ).data,
                "project": ProjectListSerializer(project).data,
            }
        )

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/status")
    @transaction.atomic
    def stage_status(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        serializer = StageStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Completing goes through the handoff gate so documents, approvals and
        # open delays still block it (api.md §10.3).
        if serializer.validated_data["status"] == "Completed":
            result = services.handoff_stage(stage, user=request.user, force=False)
            project.refresh_from_db()
            return Response(
                ProjectStageSerializer(
                    result["stage"], context=self.get_serializer_context()
                ).data
            )

        previous = stage.status
        stage.status = serializer.validated_data["status"]
        stage.save(update_fields=["status", "updated_at"])
        services.recalculate_project(project)

        record_audit(
            client=request.client_id, actor=request.user, action="STAGE_STATUS_CHANGED",
            entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
            description=f"Status changed to {stage.status}",
            from_value=previous, to_value=stage.status,
        )
        return Response(
            ProjectStageSerializer(stage, context=self.get_serializer_context()).data
        )

    @action(
        detail=True, methods=["get"], url_path=r"stages/(?P<stage_id>[^/.]+)/handoff-check"
    )
    def handoff_check(self, request, pk=None, stage_id=None):
        """Returns the blockers **as data**, without mutating (api.md §10.3).

        The same function backs ``/handoff/``, which is what stops the two
        drifting apart.
        """
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        blockers = services.handoff_blockers(stage)
        return Response(
            {
                "stageId": str(stage.id),
                "blockers": blockers,
                "canHandoff": not services.hard_blockers(blockers),
            }
        )

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/handoff")
    def handoff(self, request, pk=None, stage_id=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "handoff_stage")

        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        serializer = HandoffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = services.handoff_stage(
            stage,
            user=request.user,
            force=serializer.validated_data["force"],
            comments=serializer.validated_data.get("comments"),
        )
        project.refresh_from_db()
        return Response(
            {
                "stage": ProjectStageSerializer(
                    result["stage"], context=self.get_serializer_context()
                ).data,
                "nextStage": (
                    ProjectStageSerializer(
                        result["nextStage"], context=self.get_serializer_context()
                    ).data
                    if result["nextStage"]
                    else None
                ),
                "project": ProjectListSerializer(project).data,
                "blockers": result["blockers"],
            }
        )

    @action(detail=True, methods=["post"], url_path=r"stages/(?P<stage_id>[^/.]+)/complete")
    def complete_stage(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        result = services.handoff_stage(stage, user=request.user, force=False)
        project.refresh_from_db()
        return Response(
            {
                "stage": ProjectStageSerializer(
                    result["stage"], context=self.get_serializer_context()
                ).data,
                "nextStage": (
                    ProjectStageSerializer(
                        result["nextStage"], context=self.get_serializer_context()
                    ).data
                    if result["nextStage"]
                    else None
                ),
                "project": ProjectListSerializer(project).data,
            }
        )

    # -- tasks (api.md §10.4) ----------------------------------------------
    @action(
        detail=True, methods=["get", "post"], url_path=r"stages/(?P<stage_id>[^/.]+)/tasks"
    )
    @transaction.atomic
    def stage_tasks(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)

        if request.method == "GET":
            rows = stage.tasks.filter(deleted_at__isnull=True).select_related(
                "assigned_user", "department"
            )
            return Response(envelope(TaskSerializer(rows, many=True).data))

        serializer = TaskSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        task = serializer.save(
            client_id=request.client_id,
            project=project,
            stage=stage,
            created_by=request.user,
        )
        # api.md §10.4 -- return the recalculated parents so the UI need not refetch.
        stage, project = services.recalculate_all(stage)
        if task.assigned_user_id:
            notify(
                client=request.client_id,
                recipients=[task.assigned_user_id],
                type="pms.task_assigned",
                category="pms",
                title=f"New task: {task.task_name}",
                body=f"{project.code} / {stage.name}",
                entity_type="PmsTask",
                entity_id=task.id,
                actor=request.user,
            )
        return Response(
            {
                "task": TaskSerializer(task).data,
                "stage": ProjectStageSerializer(
                    stage, context=self.get_serializer_context()
                ).data,
                "project": ProjectListSerializer(project).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/tasks/(?P<task_id>[^/.]+)",
    )
    @transaction.atomic
    def stage_task_detail(self, request, pk=None, stage_id=None, task_id=None):
        from django.core.exceptions import ValidationError as DjangoValidationError

        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        try:
            task = stage.tasks.filter(pk=task_id, deleted_at__isnull=True).first()
        except (DjangoValidationError, ValueError, TypeError):
            task = None
        if task is None:
            raise NotFound("That task no longer exists.")

        if request.method == "DELETE":
            task.soft_delete(request.user)
        else:
            serializer = TaskSerializer(
                task, data=request.data, partial=True,
                context=self.get_serializer_context(),
            )
            serializer.is_valid(raise_exception=True)
            task = serializer.save()

        stage, project = services.recalculate_all(stage)
        return Response(
            {
                "task": None if request.method == "DELETE" else TaskSerializer(task).data,
                "stage": ProjectStageSerializer(
                    stage, context=self.get_serializer_context()
                ).data,
                "project": ProjectListSerializer(project).data,
            }
        )

    # -- documents and approvals (api.md §10.5) -----------------------------
    @action(
        detail=True,
        methods=["get", "post"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/documents",
    )
    @transaction.atomic
    def stage_documents(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)

        if request.method == "GET":
            rows = stage.documents.filter(deleted_at__isnull=True).select_related(
                "file", "uploaded_by"
            )
            return Response(
                envelope(
                    DocumentSerializer(
                        rows, many=True, context=self.get_serializer_context()
                    ).data
                )
            )

        serializer = DocumentSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        file_row = serializer.validated_data["file"]
        doc_key = serializer.validated_data.get("doc_key") or file_row.file_name

        # Versioning: the next upload on the same (stage, doc_key) becomes
        # version + 1 and carries the revision reason forward (api.md §10.5).
        previous = (
            Document.objects.filter(stage=stage, doc_key=doc_key)
            .order_by("-version")
            .first()
        )
        version = (previous.version + 1) if previous else 1
        revision_reason = previous.revision_reason if previous else None
        if previous is not None:
            Document.objects.filter(stage=stage, doc_key=doc_key).update(is_current=False)

        document = Document.objects.create(
            client_id=request.client_id,
            project=project,
            stage=stage,
            doc_key=doc_key,
            version=version,
            file=file_row,
            file_name=file_row.file_name,
            file_size=file_row.file_size,
            uploaded_by=request.user,
            comments=serializer.validated_data.get("comments"),
            revision_reason=revision_reason,
            is_proof=serializer.validated_data.get("is_proof", False),
            is_current=True,
            created_by=request.user,
        )

        record_audit(
            client=request.client_id, actor=request.user, action="DOCUMENT_UPLOADED",
            entity_type="PmsDocument", entity_id=document.id,
            entity_label=document.file_name,
            description=f"Uploaded v{version} to {stage.name}",
        )
        return Response(
            DocumentSerializer(document, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/documents/(?P<doc_id>[^/.]+)",
    )
    def document_detail(self, request, pk=None, stage_id=None, doc_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        document = self._get_document(stage, doc_id)

        versions = Document.objects.filter(
            stage=stage, doc_key=document.doc_key, deleted_at__isnull=True
        ).order_by("-version")
        context = self.get_serializer_context()
        data = DocumentSerializer(document, context=context).data
        data["versions"] = DocumentSerializer(versions, many=True, context=context).data
        data["approvals"] = ApprovalSerializer(
            document.approvals.filter(deleted_at__isnull=True), many=True
        ).data
        return Response(data)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/documents/(?P<doc_id>[^/.]+)/request-approval",
    )
    @transaction.atomic
    def request_approval(self, request, pk=None, stage_id=None, doc_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        document = self._get_document(stage, doc_id)

        serializer = RequestApprovalSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        approval = Approval.objects.create(
            client_id=request.client_id,
            project=project,
            stage=stage,
            document=document,
            approver_type=data["approverType"],
            approver_name=data.get("approverName"),
            approver_user_id=data.get("approverUserId"),
            requested_by=request.user,
            status="Pending",
            created_by=request.user,
        )
        document.approval_status = "Pending"
        document.save(update_fields=["approval_status", "updated_at"])

        if stage.status == "In Progress":
            stage.status = "Under Review"
            stage.save(update_fields=["status", "updated_at"])

        record_audit(
            client=request.client_id, actor=request.user, action="APPROVAL_REQUESTED",
            entity_type="PmsApproval", entity_id=approval.id,
            entity_label=document.file_name,
            description=f"{data['approverType']} approval requested",
        )
        if data.get("approverUserId"):
            notify(
                client=request.client_id,
                recipients=[data["approverUserId"]],
                type="pms.approval_requested",
                category="pms",
                title=f"Approval requested: {document.file_name}",
                body=f"{project.code} / {stage.name}",
                entity_type="PmsApproval",
                entity_id=approval.id,
                actor=request.user,
            )
        return Response(ApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/documents/(?P<doc_id>[^/.]+)/decide",
    )
    @transaction.atomic
    def decide_document(self, request, pk=None, stage_id=None, doc_id=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "approve_document")

        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        document = self._get_document(stage, doc_id)

        serializer = DecideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        apply_document_decision(
            document=document,
            decision=data["decision"],
            comments=data.get("comments"),
            revision_reason=data.get("revisionReason"),
            decided_by_label=request.user.name,
            user=request.user,
        )
        document.refresh_from_db()
        return Response(
            DocumentSerializer(document, context=self.get_serializer_context()).data
        )

    # -- delays (api.md §10.7) ---------------------------------------------
    @action(
        detail=True,
        methods=["post", "patch"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/delay",
    )
    @transaction.atomic
    def stage_delay(self, request, pk=None, stage_id=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "log_delay")

        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        open_delay = stage.delays.filter(
            resolved_at__isnull=True, deleted_at__isnull=True
        ).first()

        if request.method == "PATCH":
            # Revise the recovery plan without clearing the delay.
            if open_delay is None:
                raise NotFound("There is no open delay on this stage.")
            open_delay.recovery_plan = request.data.get("recoveryPlan", open_delay.recovery_plan)
            if request.data.get("expectedRecoveryDate"):
                open_delay.expected_recovery_date = request.data["expectedRecoveryDate"]
            open_delay.save()
            record_audit(
                client=request.client_id, actor=request.user,
                action="RECOVERY_PLAN_UPDATED", entity_type="PmsStage",
                entity_id=stage.id, entity_label=stage.name,
                description="Recovery plan updated",
            )
            return Response(DelaySerializer(open_delay).data)

        if open_delay is not None:
            raise Conflict(
                "This stage already has an open delay.",
                code=Codes.ALREADY_DONE,
                detail="Resolve it before logging another.",
            )

        serializer = LogDelaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        delay = Delay.objects.create(
            client_id=request.client_id,
            project=project,
            stage=stage,
            reason=data["reason"],
            category=data["category"],
            responsible_user_id=data.get("responsibleUserId"),
            expected_recovery_date=data["expectedRecoveryDate"],
            recovery_plan=data.get("recoveryPlan"),
            created_by=request.user,
        )
        stage.status = "Delayed"
        stage.save(update_fields=["status", "updated_at"])
        services.recalculate_project(project)

        record_audit(
            client=request.client_id, actor=request.user, action="DELAY_LOGGED",
            entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
            description=f"Delay logged: {data['category']}", comments=data["reason"],
        )
        if project.project_manager_id:
            notify(
                client=request.client_id,
                recipients=[project.project_manager_id],
                type="pms.delay_logged",
                category="pms",
                title=f"Delay on {stage.name}",
                body=f"{project.code}: {data['reason']}",
                entity_type="PmsStage",
                entity_id=stage.id,
                actor=request.user,
            )
        return Response(DelaySerializer(delay).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"stages/(?P<stage_id>[^/.]+)/delay/resolve",
    )
    @transaction.atomic
    def resolve_delay(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)
        delay = stage.delays.filter(
            resolved_at__isnull=True, deleted_at__isnull=True
        ).first()
        if delay is None:
            raise NotFound("There is no open delay on this stage.")

        delay.resolved_at = timezone.now()
        delay.resolved_by = request.user
        delay.resolution_notes = request.data.get("resolutionNotes")
        delay.delay_days = round(
            (delay.resolved_at - delay.created_at).total_seconds() / 86400, 2
        )
        delay.save()

        if stage.status == "Delayed":
            stage.status = "In Progress"
            stage.save(update_fields=["status", "updated_at"])
        services.recalculate_project(project)

        record_audit(
            client=request.client_id, actor=request.user, action="DELAY_RESOLVED",
            entity_type="PmsStage", entity_id=stage.id, entity_label=stage.name,
            description=f"Delay resolved after {delay.delay_days} day(s)",
        )
        return Response(DelaySerializer(delay).data)

    # -- proof sharing (api.md §10.6) --------------------------------------
    @action(
        detail=True,
        methods=["post"],
        url_path=r"documents/(?P<doc_id>[^/.]+)/share",
    )
    def share_document(self, request, pk=None, doc_id=None):
        from apps.core.permissions import require_permission
        from django.core.exceptions import ValidationError as DjangoValidationError

        require_permission(request.user, "share_client_proof")

        project = self.get_object()
        try:
            document = project.documents.filter(pk=doc_id, deleted_at__isnull=True).first()
        except (DjangoValidationError, ValueError, TypeError):
            document = None
        if document is None:
            raise NotFound("That document no longer exists.")

        serializer = ShareProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        token = secrets.token_urlsafe(32)
        share = ProofShare.objects.create(
            client_id=request.client_id,
            document=document,
            project=project,
            token_hash=hash_token(token),
            recipient_name=data.get("recipientName"),
            recipient_email=data.get("recipientEmail"),
            expires_at=timezone.now() + timedelta(days=data["expiryDays"]),
            created_by=request.user,
        )
        return Response(
            {
                "token": token,
                "url": f"/pms/approve/{token}",
                "expiresAt": share.expires_at,
                "share": ProofShareSerializer(share).data,
            },
            status=status.HTTP_201_CREATED,
        )

    # -- daily production entry link ----------------------------------------
    @action(detail=True, methods=["get", "post", "delete"], url_path="production-link")
    def production_link(self, request, pk=None):
        """The project's shareable daily-production link.

        ``GET`` returns the active link (or ``null``), ``POST`` issues one --
        ``{ regenerate: true }`` replaces the current token -- and ``DELETE``
        revokes it. Anyone holding the link can record production against this
        project's instructions, so issuing and revoking need ``verify_production``.
        """
        from apps.core.permissions import require_permission

        project = self.get_object()
        active = ProductionEntryLink.objects.filter(
            client_id=request.client_id, project=project, status="Active",
            deleted_at__isnull=True,
        ).first()

        if request.method == "GET":
            require_permission(request.user, "view_production")
            return Response({"link": _production_link_payload(active)})

        require_permission(request.user, "verify_production")
        with transaction.atomic():
            if active is not None and (
                request.method == "DELETE" or request.data.get("regenerate")
            ):
                active.status = "Revoked"
                active.revoked_at = timezone.now()
                active.save(update_fields=["status", "revoked_at", "updated_at"])
                record_audit(
                    client=request.client_id, actor=request.user,
                    action="PRODUCTION_LINK_REVOKED", entity_type="PmsProject",
                    entity_id=project.id, entity_label=project.code,
                    description="Daily production entry link revoked",
                )
                active = None

            if request.method == "DELETE":
                return Response({"link": None})

            created = active is None
            if created:
                active = ProductionEntryLink.objects.create(
                    client_id=request.client_id,
                    project=project,
                    token=secrets.token_urlsafe(24),
                    created_by=request.user,
                )
                record_audit(
                    client=request.client_id, actor=request.user,
                    action="PRODUCTION_LINK_ISSUED", entity_type="PmsProject",
                    entity_id=project.id, entity_label=project.code,
                    description="Daily production entry link issued",
                )
        return Response(
            {"link": _production_link_payload(active)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


def _production_link_payload(link):
    if link is None:
        return None
    return {
        "id": str(link.id),
        "token": link.token,
        "url": f"/production-entry/{link.token}",
        "status": link.status,
        "createdAt": link.created_at,
        "lastUsedAt": link.last_used_at,
    }


@transaction.atomic
def apply_document_decision(*, document, decision, comments=None, revision_reason=None,
                            decided_by_label=None, user=None):
    """The one path a decision takes, whether it came from the app or a
    public proof link (api.md §10.5, §10.6).

    A decision writes through to the stage approval and therefore to the
    handoff gate, in one transaction.
    """
    stage = document.stage
    project = document.project

    document.approval_status = decision
    document.comments = comments or document.comments
    if decision == "Need Improvement":
        # The document is never edited: the reason is carried to the *next*
        # upload, which becomes version + 1 (api.md §10.5).
        document.revision_reason = revision_reason
    document.save(update_fields=["approval_status", "comments", "revision_reason", "updated_at"])

    pending = Approval.objects.filter(
        document=document, status="Pending", deleted_at__isnull=True
    )
    if pending.exists():
        pending.update(
            status=decision,
            decision_at=timezone.now(),
            comments=comments,
            revision_reason=revision_reason,
            approver_name=decided_by_label,
        )
    else:
        Approval.objects.create(
            client_id=document.client_id,
            project=project,
            stage=stage,
            document=document,
            approver_type="Client" if user is None else "PM",
            approver_name=decided_by_label,
            requested_by=user if getattr(user, "is_authenticated", False) else None,
            status=decision,
            decision_at=timezone.now(),
            comments=comments,
            revision_reason=revision_reason,
        )

    if decision == "Approved":
        if stage.status in ("Under Review", "Submitted", "Need Improvement"):
            stage.status = "Approved"
            stage.save(update_fields=["status", "updated_at"])
        action_code = "DOCUMENT_APPROVED"
    else:
        stage.status = "Need Improvement"
        stage.save(update_fields=["status", "updated_at"])
        action_code = "REVISION_REQUESTED"

    record_audit(
        client=document.client_id,
        actor=user,
        action=action_code,
        entity_type="PmsDocument",
        entity_id=document.id,
        entity_label=document.file_name,
        description=f"{decision} by {decided_by_label or 'client'}",
        comments=comments,
        to_value=decision,
    )
    if project.project_manager_id:
        notify(
            client=document.client_id,
            recipients=[project.project_manager_id],
            type="pms.approval_decided",
            category="pms",
            title=f"{document.file_name} was {decision.lower()}",
            body=f"{project.code} / {stage.name}",
            entity_type="PmsDocument",
            entity_id=document.id,
            actor=user,
        )
    return document


# ---------------------------------------------------------------------------
# Cross-project reads (api.md §10.4, §10.7, §10.8)
# ---------------------------------------------------------------------------
class MyTasksView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        queryset = Task.objects.filter(
            client_id=request.client_id,
            assigned_user=request.user,
            deleted_at__isnull=True,
        ).select_related("project", "stage", "department")

        statuses = request.query_params.getlist("status")
        if statuses:
            queryset = queryset.filter(status__in=statuses)
        priority = request.query_params.get("priority")
        if priority:
            queryset = queryset.filter(priority=priority)
        due_before = request.query_params.get("dueBefore")
        if due_before:
            queryset = queryset.filter(due_date__lte=due_before)

        # Evaluate once so each row joins to its own task (never re-query).
        tasks = list(queryset.order_by("due_date"))
        rows = TaskSerializer(tasks, many=True).data
        for row, task in zip(rows, tasks):
            row["projectCode"] = task.project.code
            row["stageName"] = task.stage.name
        return Response(
            envelope(
                rows,
                aggregates={
                    "open": queryset.exclude(status="Completed").count(),
                    "blocked": queryset.filter(status="Blocked").count(),
                    "overdue": queryset.filter(
                        due_date__lt=timezone.localdate()
                    ).exclude(status="Completed").count(),
                },
            )
        )


class AllTasksView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        queryset = Task.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).select_related("project", "stage", "assigned_user", "department")

        for param, field in (
            ("assigneeId", "assigned_user_id"),
            ("projectId", "project_id"),
            ("department", "department__name"),
        ):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})

        return Response(
            envelope(TaskSerializer(queryset.order_by("due_date")[:500], many=True).data)
        )


class MyProjectsView(APIView):
    """Projects where the caller is PM or a stage assignee (api.md §10.2)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        queryset = Project.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).filter(
            Q(project_manager=request.user) | Q(stages__assigned_user=request.user)
        ).distinct().select_related("project_manager", "current_stage", "current_department")
        return Response(envelope(ProjectListSerializer(queryset, many=True).data))


class DelayViewSet(TenantModelViewSet):
    queryset = Delay.objects.select_related(
        "project", "stage", "responsible_user", "stage__department"
    )
    serializer_class = DelaySerializer
    audit_entity_type = "PmsDelay"
    status_field = None
    required_permissions = ["view_pms"]
    ordering = ["-created_at"]
    filter_map = {
        "projectId": "project_id",
        "stageName": "stage__name",
        "category": "category",
        "department": "stage__department__name",
    }
    default_date_field = "created_at"
    http_method_names = ["get", "head", "options"]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is None:
            rows = DelaySerializer(queryset, many=True).data
            return Response(envelope(rows))
        rows = DelaySerializer(page, many=True).data
        for row, delay in zip(rows, list(page)):
            row["projectId"] = str(delay.project_id)
            row["projectCode"] = delay.project.code
            row["stageId"] = str(delay.stage_id)
            row["stageName"] = delay.stage.name
            row["department"] = (
                delay.stage.department.name if delay.stage.department_id else None
            )
        return self.get_paginated_response(rows)

    def get_aggregates(self, queryset):
        return {
            "total": queryset.count(),
            "open": queryset.filter(resolved_at__isnull=True).count(),
            "resolved": queryset.filter(resolved_at__isnull=False).count(),
        }

    @action(detail=False, methods=["get"])
    def filters(self, request):
        """Distinct values present in the data, for the filter bar (api.md §10.7)."""
        queryset = self.get_queryset()
        return Response(
            {
                "projects": list(
                    queryset.values_list("project__code", flat=True).distinct()
                ),
                "departments": [
                    value
                    for value in queryset.values_list(
                        "stage__department__name", flat=True
                    ).distinct()
                    if value
                ],
                "stageNames": list(
                    queryset.values_list("stage__name", flat=True).distinct()
                ),
                "categories": [
                    value
                    for value in queryset.values_list("category", flat=True).distinct()
                    if value
                ],
            }
        )

    @action(detail=False, methods=["get"])
    def watchlist(self, request):
        return Response(envelope(services.delay_watchlist(request.client_id)))


class PmsDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request, section=None):
        client_id = request.client_id

        if section == "overview":
            from .dashboard_services import dashboard_overview

            return Response(dashboard_overview(client_id, request.query_params))
        if section == "kpis":
            return Response(services.dashboard_kpis(client_id))
        if section == "pipeline":
            return Response(envelope(services.pipeline_by_department(client_id)))
        if section == "workload":
            return Response(envelope(services.department_workload(client_id)))
        if section == "deadlines":
            days = request.query_params.get("days") or 7
            return Response(envelope(services.upcoming_deadlines(client_id, days)))

        return Response(
            {
                "kpis": services.dashboard_kpis(client_id),
                "pipelineByDepartment": services.pipeline_by_department(client_id),
                "departmentWorkload": services.department_workload(client_id),
                "upcomingDeadlines": services.upcoming_deadlines(client_id, 7),
                "delayWatchlist": services.delay_watchlist(client_id)[:10],
                "navBadges": services.nav_badges(client_id, request.user),
            }
        )


class NavBadgesView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        return Response(services.nav_badges(request.client_id, request.user))


class PmsActivityView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        from apps.core.models import AuditLog
        from apps.core.serializers_platform import AuditLogSerializer

        try:
            limit = int(request.query_params.get("limit") or 20)
        except (TypeError, ValueError):
            raise ValidationFailed(
                "Limit must be a number.",
                field_errors={"limit": ["Expected a number between 1 and 200."]},
            )
        limit = max(1, min(limit, 200))
        rows = AuditLog.objects.filter(
            client_id=request.client_id,
            entity_type__in=[
                "PmsProject", "PmsStage", "PmsTask", "PmsDocument", "PmsApproval", "PmsDelay",
            ],
        )[:limit]
        return Response(envelope(AuditLogSerializer(rows, many=True).data))


class PmsTimelineView(APIView):
    """Gantt across projects with the §10.2 filter set."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        stages = ProjectStage.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).select_related("project", "department", "assigned_user").order_by(
            "project__code", "sequence"
        )

        department = request.query_params.get("department")
        if department:
            stages = stages.filter(department__name=department)
        project_status = request.query_params.getlist("status")
        if project_status:
            stages = stages.filter(project__status__in=project_status)

        return Response(
            envelope(
                [
                    {
                        "projectId": str(stage.project_id),
                        "projectCode": stage.project.code,
                        "customerName": stage.project.customer_name,
                        "stageId": str(stage.id),
                        "stageName": stage.name,
                        "department": stage.department.name if stage.department_id else None,
                        "color": stage.department.color if stage.department_id else None,
                        "assignee": stage.assigned_user.name if stage.assigned_user_id else None,
                        "start": stage.start_datetime,
                        "end": stage.expected_completion_datetime,
                        "completionPct": stage.completion_pct,
                        "status": stage.status,
                        "isOverdue": services.is_stage_overdue(stage),
                    }
                    for stage in stages[:1000]
                ]
            )
        )


class PmsReportView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request, report_key):
        handler = services.PMS_REPORTS.get(report_key)
        if handler is None:
            raise NotFound(f"Unknown report '{report_key}'.")

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        if report_key in ("on-time-velocity", "delay-reason-pareto"):
            result = handler(request.client_id, date_from, date_to)
        else:
            result = handler(request.client_id)

        if isinstance(result, list):
            return Response(envelope(result))
        return Response(result)


class DocumentSharesView(APIView):
    """``GET /pms/documents/{docId}/shares/`` (api.md §10.6)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request, doc_id):
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            rows = ProofShare.objects.filter(
                client_id=request.client_id, document_id=doc_id, deleted_at__isnull=True
            )
            # Force evaluation inside the guard so a malformed id 404s here.
            rows = list(rows)
        except (DjangoValidationError, ValueError, TypeError):
            raise NotFound("That document no longer exists.")
        return Response(envelope(ProofShareSerializer(rows, many=True).data))


class RevokeShareView(APIView):
    """``POST /pms/shares/{token}/revoke/``.

    The token is accepted so the UI can revoke straight from the share dialog,
    and is matched by hash -- the plaintext is never stored.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["share_client_proof"]

    def post(self, request, token):
        from django.core.exceptions import ValidationError as DjangoValidationError

        share = ProofShare.objects.filter(
            client_id=request.client_id, token_hash=hash_token(token)
        ).first()
        if share is None:
            try:
                share = ProofShare.objects.filter(
                    client_id=request.client_id, pk=token
                ).first()
            except (DjangoValidationError, ValueError, TypeError):
                share = None
        if share is None:
            raise NotFound("That share link no longer exists.")

        share.status = "Revoked"
        share.revoked_at = timezone.now()
        share.revoked_reason = request.data.get("reason")
        share.save(update_fields=["status", "revoked_at", "revoked_reason", "updated_at"])
        return Response(ProofShareSerializer(share).data)
