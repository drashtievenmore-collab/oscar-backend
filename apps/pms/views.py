"""PMS endpoints (api.md §10)."""
import hashlib
import secrets
from datetime import timedelta

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
        context = self._detail_context(project)
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

    # -- stages ------------------------------------------------------------
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

        # Permission check: Only project creator can add dynamic stages
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
            description=f"Dynamic stage '{stage.name}' added with {stage.weight_pct}% weight",
        )
        return Response(
            ProjectStageSerializer(stage, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["patch"], url_path=r"stages/(?P<stage_id>[^/.]+)")
    @transaction.atomic
    def stage_detail(self, request, pk=None, stage_id=None):
        project = self.get_object()
        stage = self._get_stage(project, stage_id)

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
