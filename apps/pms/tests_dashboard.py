from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Client
from apps.core.exceptions import ValidationFailed
from apps.pms.dashboard_services import dashboard_overview
from apps.pms.models import Project, ProjectStage, StageConfig


class DashboardOverviewTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(
            slug="dash-tenant", defaults={"name": "Dash Tenant"}
        )
        self.weaving = StageConfig.objects.create(client=self.tenant, name="Weaving", sequence=1)
        self.dyeing = StageConfig.objects.create(client=self.tenant, name="Dyeing", sequence=2)

    def _project(self, code, qty, status, stage_statuses):
        project = Project.objects.create(
            client=self.tenant, code=code, quantity=Decimal(qty), status=status
        )
        for seq, (config, stage_status) in enumerate(stage_statuses, start=1):
            ProjectStage.objects.create(
                client=self.tenant, project=project, stage_config=config,
                name=config.name, sequence=seq, status=stage_status,
            )
        return project

    def test_stage_overview_and_quantities(self):
        self._project("P-1", "1000", "In Progress",
                      [(self.weaving, "Completed"), (self.dyeing, "In Progress")])
        self._project("P-2", "500", "In Progress",
                      [(self.weaving, "In Progress"), (self.dyeing, "Not Started")])
        self._project("P-3", "200", "Completed",
                      [(self.weaving, "Completed"), (self.dyeing, "Completed")])

        out = dashboard_overview(self.tenant.id, {})

        self.assertEqual(out["kpis"]["totalProjects"], 3)
        self.assertEqual(out["kpis"]["inProgressProjects"], 2)
        self.assertEqual(out["kpis"]["totalQuantity"], 1700.0)

        stages = {s["name"]: s for s in out["stageOverview"]}
        self.assertEqual(list(stages), ["Weaving", "Dyeing"])
        self.assertEqual((stages["Weaving"]["quantity"], stages["Weaving"]["projectCount"]), (500.0, 1))
        self.assertEqual((stages["Dyeing"]["quantity"], stages["Dyeing"]["projectCount"]), (1000.0, 1))

        chart = {s["name"]: s for s in out["stageQuantity"]}
        self.assertEqual(chart["Weaving"]["completed"], 1200.0)
        self.assertEqual(chart["Weaving"]["inProgress"], 500.0)
        self.assertEqual(chart["Dyeing"]["pending"], 500.0)

        statuses = {r["status"]: r["count"] for r in out["projectStatus"]}
        self.assertEqual(statuses, {"In Progress": 2, "Completed": 1})
        self.assertEqual(len(out["recentProjects"]), 3)

    def test_empty_tenant_returns_zeros(self):
        out = dashboard_overview(self.tenant.id, {})
        self.assertEqual(out["kpis"]["totalProjects"], 0)
        self.assertEqual(out["kpis"]["outstandingPayment"], 0.0)
        self.assertEqual(out["recentProjects"], [])

    def test_invalid_date_range_rejected(self):
        with self.assertRaises(ValidationFailed):
            dashboard_overview(self.tenant.id, {"dateFrom": "2026-05-01", "dateTo": "2026-04-01"})
