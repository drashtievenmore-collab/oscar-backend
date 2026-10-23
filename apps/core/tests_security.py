"""Permission fixes from the 2026-10-22 audit (P3).

1. /sales/party-discounts/ is a read-only register; it must not edit invoices.
2. Alert settings are admin-only; resolving/deleting incidents needs resolve_alerts.
3. The lot register never rewrites a taka's quantity in place.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.alert_services import ensure_alert_settings
from apps.core.models import AlertIncident, AlertSetting
from apps.inventory.models import LotTaka
from apps.masters.models import Item, Location, Party
from apps.sales.models import SalesInvoice


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class SecurityFixTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("sec"), name="Security Tenant")
        sync_permissions()
        seed_roles(self.tenant)
        ensure_alert_settings(self.tenant)
        self.today = timezone.localdate()

    def user_with(self, *permission_ids, role_code=None):
        if role_code:
            role = Role.objects.get(client=self.tenant, code=role_code)
        else:
            role = Role.objects.create(client=self.tenant, code=uid("ROLE"), name=uid("Role"))
            for permission_id in permission_ids:
                RolePermission.objects.create(role=role, permission_id=permission_id)
        return User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant, role=role
        )

    def api_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    # -- 1. party discounts ----------------------------------------------------
    def make_discounted_invoice(self):
        party = Party.objects.create(client=self.tenant, name="Radhe Textiles", code=uid("C"), type="Customer")
        return SalesInvoice.objects.create(
            client=self.tenant,
            invoice_number=uid("INV"),
            party=party,
            doc_date=self.today,
            due_date=self.today + timedelta(days=30),
            status="Unpaid",
            subtotal=Decimal("10000"),
            total_discount=Decimal("500"),
            total=Decimal("9500"),
        )

    def test_party_discounts_is_read_only(self):
        invoice = self.make_discounted_invoice()
        api = self.api_for(self.user_with("view_sales"))
        url = f"/api/v1/sales/party-discounts/{invoice.id}/"

        self.assertEqual(api.get("/api/v1/sales/party-discounts/").status_code, 200)
        self.assertEqual(api.get("/api/v1/sales/party-discounts/annual-report/").status_code, 200)
        for method in ("patch", "put", "delete"):
            resp = getattr(api, method)(url, {"total": "1"}, format="json")
            self.assertEqual(resp.status_code, 405, f"{method}: {resp.content}")
        self.assertEqual(
            api.post("/api/v1/sales/party-discounts/", {}, format="json").status_code, 405
        )

        invoice.refresh_from_db()
        self.assertIsNone(invoice.deleted_at)
        self.assertEqual(invoice.total, Decimal("9500"))

    # -- 2. alerts -----------------------------------------------------------------
    def make_incident(self):
        return AlertIncident.objects.create(
            client=self.tenant,
            alert_key="negative_stock",
            severity="Critical",
            title="Negative stock",
            message="On hand is -5",
            status="Active",
        )

    def test_alert_settings_need_manage_alerts(self):
        setting = AlertSetting.objects.get(client=self.tenant, alert_key="slow_moving_stock")
        url = f"/api/v1/alerts/settings/{setting.id}/"

        plain = self.api_for(self.user_with("view_alerts"))
        self.assertEqual(plain.get("/api/v1/alerts/settings/").status_code, 200)
        self.assertEqual(plain.patch(url, {"is_enabled": False}, format="json").status_code, 403)
        self.assertEqual(plain.delete(url).status_code, 403)
        setting.refresh_from_db()
        self.assertTrue(setting.is_enabled)

        admin = self.api_for(self.user_with(role_code="SYS_ADMIN"))
        resp = admin.patch(url, {"threshold_days": 120}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        setting.refresh_from_db()
        self.assertEqual(setting.threshold_days, 120)

    def test_any_user_can_read_and_acknowledge_incidents(self):
        incident = self.make_incident()
        api = self.api_for(self.user_with())
        self.assertEqual(api.get("/api/v1/alerts/incidents/").status_code, 200)
        self.assertEqual(api.get("/api/v1/alerts/incidents/badge/").status_code, 200)
        resp = api.post(f"/api/v1/alerts/incidents/{incident.id}/acknowledge/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_resolve_and_delete_need_resolve_alerts(self):
        incident = self.make_incident()
        plain = self.api_for(self.user_with("view_alerts"))
        self.assertEqual(
            plain.post(f"/api/v1/alerts/incidents/{incident.id}/resolve/", {}, format="json").status_code, 403
        )
        self.assertEqual(plain.delete(f"/api/v1/alerts/incidents/{incident.id}/").status_code, 403)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "Active")

        manager = self.api_for(self.user_with(role_code="SALES_MGR"))
        resp = manager.post(f"/api/v1/alerts/incidents/{incident.id}/resolve/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "Resolved")

    def test_incidents_cannot_be_edited_or_created_by_users(self):
        incident = self.make_incident()
        admin = self.api_for(self.user_with(role_code="SYS_ADMIN"))
        resp = admin.patch(f"/api/v1/alerts/incidents/{incident.id}/", {"status": "Resolved"}, format="json")
        self.assertEqual(resp.status_code, 405)

        plain = self.api_for(self.user_with("view_alerts"))
        resp = plain.post("/api/v1/alerts/incidents/", {"alert_key": "x", "title": "x", "message": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_run_alerts_needs_manage_alerts(self):
        plain = self.api_for(self.user_with("view_alerts"))
        self.assertEqual(plain.post("/api/v1/alerts/incidents/run/", {}, format="json").status_code, 403)
        admin = self.api_for(self.user_with(role_code="SYS_ADMIN"))
        resp = admin.post("/api/v1/alerts/incidents/run/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    # -- 3. lot register -----------------------------------------------------------
    def test_lot_register_rejects_quantity_edit_even_for_admins(self):
        location = Location.objects.create(
            client=self.tenant, code=uid("WH"), name="Main Godown", type="Godown", is_active=True
        )
        item = Item.objects.create(client=self.tenant, sku=uid("FAB"), name="Cotton", item_kind="Fabric", uom="Mtr")
        taka = LotTaka.objects.create(
            client=self.tenant,
            taka_id=uid("TAK"),
            lot_number=uid("LOT"),
            taka_number="1",
            item=item,
            location=location,
            received_quantity=Decimal("100"),
            available_quantity=Decimal("100"),
            status="Available",
            uom="Mtr",
        )
        url = f"/api/v1/inventory/lot-register/{taka.id}/"
        superuser = User.objects.create_user(
            email=f"{uid('root')}@example.com", password="pass", client=self.tenant, is_superuser=True, is_staff=True
        )
        for user in (superuser, self.user_with(role_code="INV_MGR")):
            api = self.api_for(user)
            for field in ("receivedQty", "qty"):
                resp = api.patch(url, {field: 150}, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
        taka.refresh_from_db()
        self.assertEqual(taka.received_quantity, Decimal("100"))

        # Descriptive fields stay editable.
        resp = self.api_for(superuser).patch(url, {"remarks": "Shade checked"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        taka.refresh_from_db()
        self.assertEqual(taka.remarks, "Shade checked")
