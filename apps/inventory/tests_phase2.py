from datetime import date, timedelta
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.inventory.models import LotTaka, QCInspection, QCInspectionLine, QCInspectionLineDefect, StockMovement
from apps.masters.models import DefectMaster, FabricQuality, Item, ItemCategory, Location, Unit


class Phase2InventoryQCTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug=f"inv-p2-{uuid.uuid4().hex[:6]}", defaults={"name": "Inventory QC Tenant"}
        )
        self.user = User.objects.create_user(
            email=f"qc-{uuid.uuid4().hex[:6]}@example.com", password="pass", client=self.client_obj, is_superuser=True
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.uom = Unit.objects.create(client=self.client_obj, code="Meter", label="Meter", is_base_unit=True)
        self.cat = ItemCategory.objects.create(client=self.client_obj, name="Fabric")
        self.quality = FabricQuality.objects.create(client=self.client_obj, name="Cotton 40s")

        self.item = Item.objects.create(
            client=self.client_obj,
            sku=f"COT-40S-{uuid.uuid4().hex[:4]}",
            name="Cotton 40s Cambric",
            category=self.cat,
            fabric_quality=self.quality.name,
            tracking_type="TAKA",
            tax_pct=Decimal("5.00"),
            uom="Meter",
        )
        self.godown = Location.objects.create(
            client=self.client_obj, code="WH-MAIN", name="Main Warehouse", type="Godown", is_active=True
        )
        self.qc_loc = Location.objects.create(
            client=self.client_obj, code="SYS-QC-HOLD", name="QC Hold Stock", type="Godown", is_active=True
        )
        self.defect1 = DefectMaster.objects.create(
            client=self.client_obj,
            code="DEF-HOLE",
            name="Hole",
            category="hole",
            severity="Major",
            is_active=True,
        )
        self.defect2 = DefectMaster.objects.create(
            client=self.client_obj,
            code="DEF-STAIN",
            name="Oil Stain",
            category="stain",
            severity="Minor",
            is_active=True,
        )

    def test_traceability_api(self):
        # 1. Create parent grey taka
        parent_taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-TRACE-1",
            taka_number="1",
            taka_id="TAK-TRACE-P1",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("500.00"),
            available_quantity=Decimal("500.00"),
            status="Available",
            qc_status="Pass",
            received_date=date(2026, 2, 1),
            uom="Meter",
        )
        # 2. Create child processed taka linked via parent_taka
        child_taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-TRACE-1-P",
            taka_number="1",
            taka_id="TAK-TRACE-C1",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("490.00"),
            available_quantity=Decimal("490.00"),
            status="Finished",
            qc_status="Pass",
            parent_taka=parent_taka,
            received_date=date(2026, 2, 10),
            uom="Meter",
        )
        child_taka.parent_lots.add(parent_taka)

        # 3. Test traceability query by taka ID
        res = self.api.get(f"/api/v1/inventory/traceability/?target={child_taka.taka_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["target"]["takaId"], "TAK-TRACE-C1")
        self.assertEqual(len(data["ancestors"]), 1)
        self.assertEqual(data["ancestors"][0]["takaId"], "TAK-TRACE-P1")

        # 4. Test traceability by parent lot ID detail route
        res_parent = self.api.get(f"/api/v1/inventory/lots/{parent_taka.id}/traceability/")
        self.assertEqual(res_parent.status_code, 200)
        data_parent = res_parent.json()
        self.assertEqual(data_parent["target"]["lotNo"], "LOT-TRACE-1")
        self.assertEqual(len(data_parent["descendants"]), 1)
        self.assertEqual(data_parent["descendants"][0]["takaId"], "TAK-TRACE-C1")

    def test_stock_ageing_and_slow_moving_api(self):
        today = timezone.localdate()
        # Fresh taka: 10 days old
        LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-AGE-1",
            taka_number="1",
            taka_id="TAK-AGE-10D",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("100.00"),
            available_quantity=Decimal("100.00"),
            status="Available",
            received_date=today - timedelta(days=10),
            uom="Meter",
        )
        # Moderate taka: 45 days old
        LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-AGE-2",
            taka_number="1",
            taka_id="TAK-AGE-45D",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("200.00"),
            available_quantity=Decimal("200.00"),
            status="Available",
            received_date=today - timedelta(days=45),
            uom="Meter",
        )
        # Old taka: 120 days old
        old_taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-AGE-3",
            taka_number="1",
            taka_id="TAK-AGE-120D",
            item=self.item,
            location=self.godown,
            received_quantity=Decimal("300.00"),
            available_quantity=Decimal("300.00"),
            status="Available",
            received_date=today - timedelta(days=120),
            uom="Meter",
        )

        # 1. Stock ageing
        age_res = self.api.get("/api/v1/inventory/stock/ageing/")
        self.assertEqual(age_res.status_code, 200)
        age_data = age_res.json()
        self.assertIn("buckets", age_data)
        buckets = {b["key"]: b for b in age_data["buckets"]}
        self.assertEqual(buckets["0-30"]["qty"], 100.0)
        self.assertEqual(buckets["31-60"]["qty"], 200.0)
        self.assertEqual(buckets["91-180"]["qty"], 300.0)

        # 2. Slow moving stock (threshold = 60 days)
        slow_res = self.api.get("/api/v1/inventory/stock/slow-moving/?thresholdDays=60")
        self.assertEqual(slow_res.status_code, 200)
        slow_data = slow_res.json()
        self.assertEqual(slow_data["count"], 1)
        self.assertEqual(slow_data["results"][0]["takaId"], "TAK-AGE-120D")

    def test_qc_inspection_4point_score_and_disposition(self):
        # Create roll in QC Hold
        qc_taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-QC-001",
            taka_number="1",
            taka_id="TAK-QC-001",
            item=self.item,
            location=self.qc_loc,
            received_quantity=Decimal("100.00"),
            available_quantity=Decimal("100.00"),
            width=Decimal("44.00"),
            status="QC Hold",
            qc_status="Pending",
            received_date=timezone.localdate(),
            uom="Meter",
        )

        # Post QC Inspection with 2 penalty points on 100m roll
        # 4-point ASTM calculation: points_per_100_sq_m = (points * 100) / (meters * width_meters)
        # width = 44 in * 0.0254 = 1.1176 m -> area = 111.76 sq m -> points_per_100_sq_m = 200 / 111.76 = 1.79 <= 20 -> Grade A
        create_res = self.api.post(
            "/api/v1/inventory/qc-inspections/",
            {
                "inspectionDate": timezone.localdate().isoformat(),
                "shift": "Morning",
                "notes": "Piece-by-piece inspection test",
                "lines": [
                    {
                        "lotTakaId": str(qc_taka.id),
                        "takaNumber": "1",
                        "lotNumber": "LOT-QC-001",
                        "actualMeter": 100.0,
                        "width": 44.0,
                        "rollWeight": 15.0,
                        "defects": [
                            {
                                "defectId": str(self.defect2.id),
                                "defectCount": 2,
                                "points": 1,
                                "distanceMeter": 12.5,
                                "remarks": "Minor oil stain",
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(create_res.status_code, 201, create_res.content)
        insp_id = create_res.json()["id"]
        insp_data = create_res.json()
        self.assertEqual(len(insp_data["lines"]), 1)
        line = insp_data["lines"][0]
        self.assertEqual(line["grade"], "A")
        self.assertEqual(line["disposition"], "Pass")
        self.assertEqual(line["penaltyPoints"], 2)

        # Approve inspection (posts real movement QC_PASS and relocates to WH-MAIN)
        appr_res = self.api.post(f"/api/v1/inventory/qc-inspections/{insp_id}/approve/", {}, format="json")
        self.assertEqual(appr_res.status_code, 200, appr_res.content)
        self.assertEqual(appr_res.json()["status"], "Approved")

        # Verify LotTaka location and status updated
        qc_taka.refresh_from_db()
        self.assertEqual(qc_taka.location.code, "WH-MAIN")
        self.assertEqual(qc_taka.status, "Available")
        self.assertEqual(qc_taka.qc_status, "Pass")

        # Verify StockMovement records created
        movs = StockMovement.objects.filter(
            client_id=self.client_obj.id,
            reference_type="QCInspection",
            reference_id=insp_id,
            type="QC_PASS",
        )
        self.assertEqual(movs.count(), 2)  # 1 negative at SYS-QC-HOLD, 1 positive at WH-MAIN
