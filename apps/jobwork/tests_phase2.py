from decimal import Decimal
from datetime import date
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.masters.models import FabricQuality, Item, ItemCategory, JobWorkRateMaster, Location, LossReasonMaster, Party, ProcessMaster, Unit
from apps.inventory.models import LotTaka, StockMovement
from apps.jobwork.models import JobWorkOrder, JWOInward, JWOInwardTaka, JWOLossEntry, JWOMaterial, JWOOutward, JWOOutwardTaka


class Phase2JobWorkTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="jw-phase2-tenant", defaults={"name": "JW Phase 2 Tenant"}
        )
        self.user = User.objects.create_user(
            email="jw-p2@example.com", password="pass", client=self.client_obj, is_superuser=True
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.uom = Unit.objects.create(client=self.client_obj, code="Meter", label="Meter", is_base_unit=True)
        self.cat = ItemCategory.objects.create(client=self.client_obj, name="Fabric")
        self.quality = FabricQuality.objects.create(client=self.client_obj, name="Rayon 30s")

        self.item = Item.objects.create(
            client=self.client_obj,
            sku="RAY-30S",
            name="Rayon 30s Dyed",
            category=self.cat,
            fabric_quality=self.quality.name,
            tracking_type="TAKA",
            tax_pct=Decimal("5.00"),
            uom="Meter",
        )
        self.godown = Location.objects.create(
            client=self.client_obj, code="WH-GODOWN-1", name="Main Godown", type="Godown", is_active=True
        )
        self.processor = Party.objects.create(
            client=self.client_obj,
            name="Apex Processing House",
            code="PROC-APEX",
            type="Processor",
        )
        self.process = ProcessMaster.objects.create(
            client=self.client_obj,
            code="DYE-01",
            name="Dyeing",
            expected_loss_pct=Decimal("3.00"),
            tolerance_pct=Decimal("1.00"),
        )
        self.rate_master = JobWorkRateMaster.objects.create(
            client=self.client_obj,
            processor=self.processor,
            process=self.process,
            fabric_quality=self.quality,
            rate=Decimal("15.50"),
            rate_uom="Meter",
            effective_from=date(2026, 1, 1),
            is_active=True,
        )
        self.loss_reason = LossReasonMaster.objects.create(
            client=self.client_obj,
            code="LOSS-SHRINK",
            name="Normal shrinkage/process loss",
        )

        # Create source grey taka in Godown
        self.grey_taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-GREY-100",
            taka_number="1",
            taka_id="TAK-100-1",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("1000.00"),
            available_quantity=Decimal("1000.00"),
            status="Available",
            qc_status="Pass",
            received_date=date(2026, 1, 10),
            uom="Meter",
        )

    def test_outward_inward_taka_movements_and_reconciliation(self):
        # 1. Lookup rate endpoint
        rate_res = self.api.get(
            f"/api/v1/jobwork/orders/lookup-rate/?processorId={self.processor.id}&processId={self.process.id}&fabricQualityId={self.quality.id}"
        )
        self.assertEqual(rate_res.status_code, 200)
        self.assertEqual(float(rate_res.json()["rate"]), 15.5)

        # 2. Create Job Work Order
        order_res = self.api.post(
            "/api/v1/jobwork/orders/",
            {
                "vendorId": str(self.processor.id),
                "processMasterId": str(self.process.id),
                "process": "Dyeing",
                "vendor": self.processor.name,
                "orderDate": "2026-04-15",
                "expectedLossPct": 3.0,
                "materials": [
                    {
                        "fabricItem": self.item.name,
                        "fabricQuality": self.quality.name,
                        "qty": 1000.0,
                        "rate": 15.5,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(order_res.status_code, 201)
        jwo_id = order_res.json()["id"]

        # 3. Create Outward Challan with specific taka
        outward_res = self.api.post(
            f"/api/v1/jobwork/orders/{jwo_id}/outwards/",
            {
                "date": "2026-04-16",
                "qty": 1000.0,
                "vehicleNo": "MH-04-AB-1234",
                "takas": [
                    {
                        "lotTakaId": str(self.grey_taka.id),
                        "meters": 1000.0,
                        "weight_kg": 120.0,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(outward_res.status_code, 201, outward_res.content)

        # Verify grey taka status updated to 'External WIP'
        self.grey_taka.refresh_from_db()
        self.assertEqual(self.grey_taka.status, "External WIP")

        # Verify stock movement out of Godown and into Virtual WIP
        out_mov = StockMovement.objects.filter(
            client_id=self.client_obj.id,
            lot_taka_id=self.grey_taka.id,
            location_id=self.godown.id,
            type="JOBWORK_OUTWARD",
        ).first()
        self.assertIsNotNone(out_mov)
        self.assertEqual(out_mov.quantity, Decimal("-1000.00"))

        wip_mov = StockMovement.objects.filter(
            client_id=self.client_obj.id,
            location__code__startswith="SYS-WIP-",
            type="JOBWORK_OUTWARD",
        ).first()
        self.assertIsNotNone(wip_mov)
        self.assertEqual(wip_mov.quantity, Decimal("1000.00"))

        # 4. Receive Inward with takas (960m received, 40m loss)
        inward_res = self.api.post(
            f"/api/v1/jobwork/orders/{jwo_id}/inwards/",
            {
                "date": "2026-04-20",
                "qty": 960.0,
                "accepted": 960.0,
                "rejected": 0.0,
                "challanNo": "CH-APEX-99",
                "takas": [
                    {
                        "sourceTakaId": str(self.grey_taka.id),
                        "taka_number": "1",
                        "meters": 960.0,
                        "weight_kg": 115.0,
                        "width": 44.0,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(inward_res.status_code, 201)

        # Verify finished taka created in QC Hold
        qc_takas = LotTaka.objects.filter(
            client=self.client_obj,
            parent_taka=self.grey_taka,
        )
        self.assertEqual(qc_takas.count(), 1)
        qc_taka = qc_takas.first()
        self.assertEqual(qc_taka.status, "QC Hold")
        self.assertEqual(qc_taka.available_quantity, Decimal("960.00"))
        self.assertEqual(qc_taka.location.code, "SYS-QC-HOLD")

        # 5. Try to close order before recording approved loss (fails reconciliation gate)
        # Sent: 1000m, Received: 960m, Loss tolerance 3% -> max allowed variance = 30m. Difference = 40m.
        close_fail = self.api.post(f"/api/v1/jobwork/orders/{jwo_id}/close/", {}, format="json")
        self.assertEqual(close_fail.status_code, 409)
        self.assertIn("Reconciliation gate failed", close_fail.json()["message"])

        # 6. Record approved loss entry (40m for shrinkage)
        loss_res = self.api.post(
            f"/api/v1/jobwork/orders/{jwo_id}/losses/",
            {
                "lossReasonId": str(self.loss_reason.id),
                "qty": 40.0,
                "remarks": "Process shrinkage approved",
            },
            format="json",
        )
        self.assertEqual(loss_res.status_code, 201)
        loss_id = loss_res.json()["id"]

        # Approve the loss entry
        appr_loss = self.api.post(
            f"/api/v1/jobwork/orders/{jwo_id}/losses/{loss_id}/approve/", {}, format="json"
        )
        self.assertEqual(appr_loss.status_code, 200)
        self.assertEqual(appr_loss.json()["status"], "Approved")

        # 7. Now close order -> succeeds because Sent (1000) = Received (960) + Approved Loss (40)
        close_success = self.api.post(f"/api/v1/jobwork/orders/{jwo_id}/close/", {}, format="json")
        self.assertEqual(close_success.status_code, 200)
        self.assertEqual(close_success.json()["status"], "Closed")

        # 8. Check Processor Ledger endpoint
        ledger_res = self.api.get(
            f"/api/v1/jobwork/orders/processor-ledger/?processorId={self.processor.id}"
        )
        self.assertEqual(ledger_res.status_code, 200)
        ledger_data = ledger_res.json()["results"][0]
        self.assertEqual(ledger_data["totalSent"], 1000.0)
        self.assertEqual(ledger_data["totalReceived"], 960.0)
        self.assertEqual(ledger_data["totalLoss"], 40.0)
        self.assertEqual(ledger_data["closingBalance"], 0.0)
