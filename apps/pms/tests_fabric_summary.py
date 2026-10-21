import uuid
from decimal import Decimal
from django.test import TestCase
from apps.accounts.models import Client, User
from apps.masters.models import Item, Location
from apps.inventory.models import LotTaka, GreyStockAllocation
from apps.inventory import allocation_services as allocations_svc
from apps.pms.models import Project


class FabricStockSummaryTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-fabric-summary", defaults={"name": "Test Fabric Summary Tenant"}
        )
        self.user = User.objects.filter(email="user@testsummary.com").first()
        if not self.user:
            self.user = User.objects.create_user(
                email="user@testsummary.com",
                password="pass",
                client=self.client_obj,
            )
        self.user.is_superuser = True
        self.user.save()

        self.godown, _ = Location.objects.get_or_create(
            client=self.client_obj,
            code="MAIN",
            defaults={"type": "Warehouse", "name": "Main Godown"},
        )
        self.qc_hold_loc, _ = Location.objects.get_or_create(
            client=self.client_obj,
            code="QC_HOLD",
            defaults={"type": "QC", "name": "QC Hold Area"},
        )

        self.fabric_item_1 = Item.objects.create(
            client=self.client_obj,
            sku=f"SKU-TEST-CHIFFON-{uuid.uuid4().hex[:6]}",
            name="Test Chiffon Fabric",
            item_kind="Fabric",
            fabric_quality="Chiffon 60g",
            fabric_design="Plain",
            fabric_color="Natural Grey",
            uom="Meters",
        )
        self.fabric_item_2 = Item.objects.create(
            client=self.client_obj,
            sku=f"SKU-TEST-SILK-{uuid.uuid4().hex[:6]}",
            name="Test Silk Fabric",
            item_kind="Fabric",
            fabric_quality="Mulberry Silk",
            fabric_design="Stripes",
            fabric_color="Off-White",
            uom="Meters",
        )

        self.project_a = Project.objects.create(
            client=self.client_obj,
            code="PRJ-FAB-001",
            product_name="Project A",
            created_by=self.user,
            status="In Progress",
        )
        self.project_b = Project.objects.create(
            client=self.client_obj,
            code="PRJ-FAB-002",
            product_name="Project B",
            created_by=self.user,
            status="In Progress",
        )

    def test_fabric_grouping_and_reconciliation(self):
        """Test that multiple takas sharing lot numbers group correctly with zero duplicates."""
        # Create 3 takas under LOT-A1 for fabric 1 (Passed)
        taka1 = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_1,
            lot_number="LOT-A1",
            taka_number="1",
            taka_id="TAK-001",
            received_quantity=Decimal("100.00"),
            available_quantity=Decimal("100.00"),
            qc_status="Passed",
            location=self.godown,
            uom="Meters",
        )
        taka2 = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_1,
            lot_number="LOT-A1",
            taka_number="2",
            taka_id="TAK-002",
            received_quantity=Decimal("150.00"),
            available_quantity=Decimal("150.00"),
            qc_status="Passed",
            location=self.godown,
            uom="Meters",
        )
        # Create 1 taka under LOT-A2 for fabric 1 (QC Hold)
        taka3 = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_1,
            lot_number="LOT-A2",
            taka_number="1",
            taka_id="TAK-003",
            received_quantity=Decimal("50.00"),
            available_quantity=Decimal("50.00"),
            qc_status="Pending",
            location=self.qc_hold_loc,
            uom="Meters",
        )
        # Create 1 taka under LOT-B1 for fabric 2 (Passed)
        taka4 = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_2,
            lot_number="LOT-B1",
            taka_number="1",
            taka_id="TAK-004",
            received_quantity=Decimal("200.00"),
            available_quantity=Decimal("200.00"),
            qc_status="Passed",
            location=self.godown,
            uom="Meters",
        )

        fabrics, lots = allocations_svc.fabric_stock_summary(self.client_obj.id)
        self.assertEqual(len(fabrics), 2)
        self.assertEqual(len(lots), 4)

        f1 = next(f for f in fabrics if f["itemId"] == str(self.fabric_item_1.id))
        self.assertEqual(f1["physicalStock"], 300.0)
        self.assertEqual(f1["qcPassedStock"], 250.0)
        self.assertEqual(f1["qcHoldStock"], 50.0)
        self.assertEqual(f1["rejectedStock"], 0.0)
        self.assertEqual(f1["reservedStock"], 0.0)
        self.assertEqual(f1["usableStock"], 250.0)
        self.assertEqual(f1["takasCount"], 3)
        self.assertEqual(f1["lotsCount"], 2)

        # Check reconciliation:
        self.assertEqual(f1["physicalStock"], f1["qcPassedStock"] + f1["qcHoldStock"] + f1["rejectedStock"])
        self.assertEqual(f1["usableStock"], f1["qcPassedStock"] - f1["reservedStock"])

        f2 = next(f for f in fabrics if f["itemId"] == str(self.fabric_item_2.id))
        self.assertEqual(f2["physicalStock"], 200.0)
        self.assertEqual(f2["qcPassedStock"], 200.0)
        self.assertEqual(f2["qcHoldStock"], 0.0)
        self.assertEqual(f2["usableStock"], 200.0)
        self.assertEqual(f2["takasCount"], 1)
        self.assertEqual(f2["lotsCount"], 1)

    def test_reservation_and_drill_down_allocation(self):
        """Test allocating from drilldown, reservation exclusion, and balance reconciliation."""
        taka1 = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_1,
            lot_number="LOT-A1",
            taka_number="1",
            taka_id="TAK-001",
            received_quantity=Decimal("100.00"),
            available_quantity=Decimal("100.00"),
            qc_status="Passed",
            location=self.godown,
            uom="Meters",
        )

        # Allocate 40m of taka1 to Project B (external reservation for Project A)
        allocations_svc.allocate_to_project(
            client_id=self.client_obj.id,
            project=self.project_b,
            lot_taka_id=taka1.id,
            quantity=Decimal("40.00"),
            user=self.user,
        )

        # From Project A perspective: 40m is reserved, 60m is usable
        fabrics_a, lots_a = allocations_svc.fabric_stock_summary(
            self.client_obj.id, project=self.project_a
        )
        f1_a = fabrics_a[0]
        self.assertEqual(f1_a["physicalStock"], 100.0)
        self.assertEqual(f1_a["qcPassedStock"], 100.0)
        self.assertEqual(f1_a["reservedStock"], 40.0)
        self.assertEqual(f1_a["usableStock"], 60.0)
        self.assertEqual(f1_a["allocatedToProject"], 0.0)
        self.assertEqual(f1_a["balanceStock"], 60.0)

        # Now allocate 30m of taka1 to Project A using set_project_lot_allocation
        allocations_svc.set_project_lot_allocation(
            client_id=self.client_obj.id,
            project=self.project_a,
            lot_taka_id=taka1.id,
            target_qty=Decimal("30.00"),
            user=self.user,
        )

        fabrics_a2, lots_a2 = allocations_svc.fabric_stock_summary(
            self.client_obj.id, project=self.project_a
        )
        f1_a2 = fabrics_a2[0]
        self.assertEqual(f1_a2["physicalStock"], 100.0)
        self.assertEqual(f1_a2["reservedStock"], 40.0)  # Other project reservation
        self.assertEqual(f1_a2["usableStock"], 60.0)    # Project A can use up to 60m
        self.assertEqual(f1_a2["allocatedToProject"], 30.0)
        self.assertEqual(f1_a2["balanceStock"], 30.0)

        lot_item = lots_a2[0]
        self.assertEqual(lot_item["status"], "Selected")
        self.assertEqual(lot_item["allocateQty"], 30.0)
        self.assertEqual(lot_item["balance"], 30.0)
        self.assertTrue(lot_item["isAllocatable"])

    def test_grey_stock_allocation_view_response(self):
        """Test that grey_stock_allocation API endpoint returns both fabrics summary and enriched lots."""
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.pms.views import ProjectViewSet

        taka = LotTaka.objects.create(
            client=self.client_obj,
            item=self.fabric_item_1,
            lot_number="LOT-VIEW-01",
            taka_number="1",
            taka_id="TAK-V01",
            received_quantity=Decimal("500.00"),
            available_quantity=Decimal("500.00"),
            qc_status="Passed",
            location=self.godown,
            uom="Meters",
        )

        factory = APIRequestFactory()
        request = factory.get(f"/api/pms/projects/{self.project_a.id}/grey-stock-allocation/")
        force_authenticate(request, user=self.user)
        request.client_id = self.client_obj.id

        view = ProjectViewSet.as_view({"get": "grey_stock_allocation"})
        response = view(request, pk=str(self.project_a.id))
        self.assertEqual(response.status_code, 200)

        data = response.data
        self.assertIn("fabrics", data)
        self.assertIn("lots", data)
        self.assertIn("requiredMeters", data)
        self.assertIn("allocatedMeters", data)
        self.assertIn("shortageMeters", data)

        # Check fabric row contents
        fabrics = data["fabrics"]
        self.assertTrue(len(fabrics) >= 1)
        f = next(item for item in fabrics if item["itemId"] == str(self.fabric_item_1.id))
        self.assertEqual(f["fabricName"], "Test Chiffon Fabric")
        self.assertEqual(f["physicalStock"], 500.0)
        self.assertEqual(f["qcPassedStock"], 500.0)
        self.assertEqual(f["usableStock"], 500.0)

        # Check lot row contents
        lot_entry = next(l for l in data["lots"] if l["takaId"] == "TAK-V01")
        self.assertEqual(lot_entry["lotNumber"], "LOT-VIEW-01")
        self.assertEqual(lot_entry["totalStock"], 500.0)
        self.assertEqual(lot_entry["usable"], 500.0)
        self.assertTrue(lot_entry["isAllocatable"])
