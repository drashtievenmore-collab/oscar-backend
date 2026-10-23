from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import Account, JournalEntry
from apps.accounting.services import seed_chart_of_accounts
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.models import File
from apps.inventory.logistics_services import allocate_trip_freight, confirm_trip_delivery, create_trip
from apps.inventory.models import LotCostEntry, LotTaka, Trip, TripMaterialLine
from apps.masters.models import Driver, Item, ItemCategory, Party, Transporter, Unit, Vehicle
from apps.sales.models import SalesOrder


class LogisticsGapsTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(
            slug=f"logistics-{uuid.uuid4().hex[:6]}", name="Logistics Tenant"
        )
        self.user = User.objects.create_user(
            email=f"logistics-{uuid.uuid4().hex[:6]}@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        # Seed chart of accounts for GL testing
        seed_chart_of_accounts(self.client_obj)

        self.uom = Unit.objects.create(client=self.client_obj, code="MTR", label="Meter", is_base_unit=True)
        self.cat = ItemCategory.objects.create(client=self.client_obj, name="Fabric")
        self.item = Item.objects.create(
            client=self.client_obj,
            sku=f"FAB-{uuid.uuid4().hex[:4]}",
            name="Test Cotton Fabric",
            category=self.cat,
            uom="MTR",
        )
        self.transporter_party = Party.objects.create(
            client=self.client_obj,
            name="FastTrack Logistics Party",
            code="PTY-TRP-01",
            type="Vendor",
        )
        self.transporter = Transporter.objects.create(
            client=self.client_obj,
            name="FastTrack Logistics",
            code="TRP-FTL",
            phone="9876543210",
        )
        self.customer = Party.objects.create(
            client=self.client_obj,
            name="Global Retailers",
            code="CUST-001",
            type="Customer",
        )

    def test_01_vehicle_and_driver_masters_crud(self):
        """Vehicle and Driver CRUD under /api/v1/masters/."""
        # 1. Create Vehicle
        res_v = self.api.post(
            "/api/v1/masters/vehicles/",
            {
                "vehicleNumber": "GJ-01-AB-1234",
                "vehicleType": "Truck",
                "capacity": "10 MT",
                "transporterId": str(self.transporter.id),
                "remarks": "Heavy commercial vehicle",
            },
            format="json",
        )
        self.assertEqual(res_v.status_code, 201, res_v.data)
        v_id = res_v.data["id"]
        self.assertEqual(res_v.data["vehicleNumber"], "GJ-01-AB-1234")
        self.assertEqual(str(res_v.data["transporterId"]), str(self.transporter.id))

        # List Vehicles
        list_v = self.api.get("/api/v1/masters/vehicles/")
        self.assertEqual(list_v.status_code, 200)
        self.assertTrue(any(v["id"] == v_id for v in list_v.data["results"] if "results" in list_v.data))

        # 2. Create Driver
        res_d = self.api.post(
            "/api/v1/masters/drivers/",
            {
                "name": "Ramesh Kumar",
                "mobile": "9988776655",
                "licence": "DL-0420110012345",
                "transporterId": str(self.transporter.id),
                "remarks": "Senior driver",
            },
            format="json",
        )
        self.assertEqual(res_d.status_code, 201, res_d.data)
        d_id = res_d.data["id"]
        self.assertEqual(res_d.data["name"], "Ramesh Kumar")
        self.assertEqual(res_d.data["mobile"], "9988776655")

        # List Drivers
        list_d = self.api.get("/api/v1/masters/drivers/")
        self.assertEqual(list_d.status_code, 200)
        self.assertTrue(any(d["id"] == d_id for d in list_d.data["results"] if "results" in list_d.data))

    def test_02_create_trip_with_vehicle_driver_fks(self):
        """Trip accepts vehicle/driver FKs and auto-populates free-text fields."""
        vehicle = Vehicle.objects.create(
            client=self.client_obj,
            vehicle_number="GJ-05-XY-9999",
            vehicle_type="Tempo",
            capacity="3 MT",
            transporter=self.transporter,
        )
        driver = Driver.objects.create(
            client=self.client_obj,
            name="Suresh Patel",
            mobile="9123456780",
            licence="GJ-05-2015-8888",
            transporter=self.transporter,
        )

        res = self.api.post(
            "/api/v1/inventory/trips/",
            {
                "vehicleId": str(vehicle.id),
                "driverId": str(driver.id),
                "fromLocation": "Mill Godown",
                "toLocation": "City Distribution Center",
                "transporterId": str(self.transporter.id),
                "freightAmount": 3500,
                "materials": [
                    {"lotNumber": "LOT-101", "takaNumber": "T-1", "quantity": 500},
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        trip_id = res.data["id"]
        trip = Trip.objects.get(id=trip_id)
        self.assertEqual(trip.vehicle_id, vehicle.id)
        self.assertEqual(trip.driver_id, driver.id)
        self.assertEqual(trip.vehicle_number, "GJ-05-XY-9999")
        self.assertEqual(trip.driver_name, "Suresh Patel")
        self.assertEqual(trip.driver_mobile, "9123456780")
        self.assertEqual(trip.status, "Draft")

    def test_03_freight_allocation_lot_target(self):
        """Allocate freight to LOT posts to lot cost ledger without changing status to Delivered."""
        trip = create_trip(
            client_id=self.client_obj.id,
            from_location="Origin",
            to_location="Destination",
            vehicle_number="MH-12-CD-5555",
            freight_amount=1000,
            loading_charges=200,
            paid_by="Company",
            cost_allocation_target="LOT",
            material_lines=[
                {"lotNumber": "LOT-ALPHA", "quantity": 600},
                {"lotNumber": "LOT-ALPHA", "quantity": 400},
            ],
        )
        self.assertEqual(trip.status, "Draft")
        trip.status = "In Transit"
        trip.save(update_fields=["status"])

        # Allocate freight
        res = self.api.post(f"/api/v1/inventory/trips/{trip.id}/allocate-freight/")
        self.assertEqual(res.status_code, 200, res.data)

        trip.refresh_from_db()
        self.assertTrue(trip.is_cost_allocated)
        # Status MUST NOT be Delivered just from allocating freight
        self.assertEqual(trip.status, "In Transit")

        # Verify lot cost entry was created
        entries = LotCostEntry.objects.filter(client=self.client_obj, lot_number="LOT-ALPHA")
        self.assertEqual(entries.count(), 2)
        total_lot_cost = sum(e.amount for e in entries)
        self.assertEqual(total_lot_cost, Decimal("1200.00"))

        # Verify GL journal entry for freight payable (paid_by == Company)
        je = JournalEntry.objects.filter(
            client=self.client_obj, source_document_type="TRIP", source_document_id=trip.id
        ).first()
        self.assertIsNotNone(je)
        lines = list(je.lines.all())
        debit_line = next(l for l in lines if l.debit > 0)
        credit_line = next(l for l in lines if l.credit > 0)
        self.assertEqual(debit_line.account.system_key, "freight")
        self.assertEqual(debit_line.debit, Decimal("1200.00"))
        self.assertEqual(credit_line.account.system_key, "creditors")
        self.assertEqual(credit_line.credit, Decimal("1200.00"))

    def test_04_freight_allocation_sales_order_target(self):
        """Allocate freight to SALES_ORDER target stores cost against SalesOrder for party profitability."""
        order = SalesOrder.objects.create(
            client=self.client_obj,
            party=self.customer,
            order_number="SO-2026-901",
            doc_date=timezone.localdate(),
            subtotal=Decimal("50000.00"),
            total=Decimal("50000.00"),
        )
        self.assertEqual(order.allocated_freight_cost, Decimal("0.00"))

        trip = create_trip(
            client_id=self.client_obj.id,
            from_location="Warehouse",
            to_location="Customer Site",
            vehicle_number="GJ-01-TR-1111",
            freight_amount=2500,
            paid_by="Company",
            cost_allocation_target="SALES_ORDER",
            material_lines=[
                {
                    "document_reference_type": "SALES_ORDER",
                    "document_reference_id": str(order.id),
                    "document_reference_number": order.order_number,
                    "quantity": 1000,
                }
            ],
        )

        res = self.api.post(f"/api/v1/inventory/trips/{trip.id}/allocate-freight/")
        self.assertEqual(res.status_code, 200, res.data)

        order.refresh_from_db()
        self.assertEqual(order.allocated_freight_cost, Decimal("2500.00"))

    def test_05_freight_allocation_expense_target(self):
        """Allocate freight to EXPENSE target posts an expense journal."""
        trip = create_trip(
            client_id=self.client_obj.id,
            from_location="Loc A",
            to_location="Loc B",
            vehicle_number="MH-04-EX-7777",
            freight_amount=1500,
            other_charges=300,
            paid_by="Company",
            cost_allocation_target="EXPENSE",
            material_lines=[{"quantity": 100}],
        )

        res = self.api.post(f"/api/v1/inventory/trips/{trip.id}/allocate-freight/")
        self.assertEqual(res.status_code, 200, res.data)

        trip.refresh_from_db()
        self.assertTrue(trip.is_cost_allocated)

        je = JournalEntry.objects.filter(
            client=self.client_obj, source_document_type="TRIP", source_document_id=trip.id
        ).first()
        self.assertIsNotNone(je)
        lines = list(je.lines.all())
        self.assertEqual(len(lines), 2)
        debit = next(l for l in lines if l.debit > 0)
        self.assertEqual(debit.account.system_key, "freight")
        self.assertEqual(debit.debit, Decimal("1800.00"))

    def test_06_confirm_delivery(self):
        """POST /inventory/trips/{id}/confirm-delivery/ sets status Delivered, proof_file, and remarks."""
        trip = create_trip(
            client_id=self.client_obj.id,
            from_location="Dock A",
            to_location="Dock B",
            vehicle_number="GJ-01-CD-0001",
            freight_amount=500,
            material_lines=[{"quantity": 200}],
        )
        trip.status = "In Transit"
        trip.save(update_fields=["status"])

        proof_file = File.objects.create(
            client=self.client_obj,
            file_name="delivery_proof.pdf",
            file_size=2048,
            content_type="application/pdf",
            storage_key="proofs/delivery_proof.pdf",
            status="committed",
        )

        deliv_time = timezone.now().isoformat()
        res = self.api.post(
            f"/api/v1/inventory/trips/{trip.id}/confirm-delivery/",
            {
                "deliveryTime": deliv_time,
                "proofFileId": str(proof_file.id),
                "remarks": "Received by Store Manager John in good condition.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "Delivered")
        self.assertEqual(res.data["proofFileId"], str(proof_file.id))

        trip.refresh_from_db()
        self.assertEqual(trip.status, "Delivered")
        self.assertEqual(trip.proof_file_id, proof_file.id)
        self.assertIn("Received by Store Manager John", trip.remarks)
