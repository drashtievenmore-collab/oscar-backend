from decimal import Decimal
from datetime import date
from django.test import TestCase

from apps.accounts.models import Client, User
from apps.hrms.models import Employee
from apps.masters.models import Agency, FabricQuality, Item, ItemCategory, Location
from apps.inventory.models import LotTaka, StockMovement
from apps.pms.models import ProductionInstruction, DailyProductionEntry
from apps.pms import production_services


class Phase2ProductionTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="pms-phase2-tenant", defaults={"name": "PMS Phase 2 Tenant"}
        )
        self.user = User.objects.create_user(
            email="pms-p2@example.com", password="pass", client=self.client_obj, is_superuser=True
        )
        self.employee = Employee.objects.create(
            client=self.client_obj,
            employee_code="EMP-001",
            name="Vipul Sharma",
            status="Active",
            joining_date=date(2025, 1, 1),
        )
        from apps.masters.models import Unit
        Unit.objects.create(client=self.client_obj, code="Meter", label="Meter", is_base_unit=True)
        self.cat = ItemCategory.objects.create(client=self.client_obj, name="Fabric")
        self.quality = FabricQuality.objects.create(client=self.client_obj, name="60s Cambric Cotton")
        self.grey_item = Item.objects.create(
            client=self.client_obj,
            sku="GREY-60S",
            name="60s Cambric Cotton Grey",
            category=self.cat,
            fabric_quality=self.quality.name,
            tracking_type="TAKA",
            tax_pct=Decimal("5.00"),
            uom="Meter",
        )
        self.loc = Location.objects.create(
            client=self.client_obj, code="WH-GREY", name="Grey Godown", type="Godown", is_active=True
        )
        from apps.masters.models import Party
        self.vendor = Party.objects.create(
            client=self.client_obj,
            name="Om Weaving Agency",
            code="VEND-OM",
            type="Vendor",
        )
        self.agency = Agency.objects.create(
            client=self.client_obj,
            code="AGY-OM",
            name="Om Weaving Agency",
            contact_person="Ramesh Patel",
            phone="9876543210",
            default_rate=Decimal("12.50"),
            rate_uom="Meter",
            is_active=True,
        )

    def test_production_lifecycle_and_verification(self):
        # 1. Create Instruction
        inst = production_services.create_instruction(
            client=self.client_obj,
            employee=self.employee,
            user=self.user,
            agency=self.agency,
            fabric=self.grey_item.name,
            order_reference="REF-001",
            order_meter=Decimal("5000.00"),
            taka_count=50,
            total_weight=Decimal("600.00"),
            width=Decimal("48.00"),
            gsm=Decimal("120.00"),
            tentative_rate=Decimal("12.50"),
            agreed_rate=Decimal("12.00"),
            rate_uom="Meter",
            special_terms="Zero tolerance for reed marks",
        )
        self.assertEqual(inst.status, "Draft")
        self.assertEqual(inst.order_meter, Decimal("5000.00"))

        # 2. Approve
        inst = production_services.approve_instruction(inst, user=self.user)
        self.assertEqual(inst.status, "Approved")

        # 3. Start
        inst = production_services.start_instruction(inst, user=self.user)
        self.assertEqual(inst.status, "Running")

        # 4. Record Daily Entry 1 (2000m)
        entry1 = production_services.record_daily_entry(
            client=self.client_obj,
            instruction=inst,
            entered_by_employee=self.employee,
            entered_by_user=self.user,
            entry_date=date.today(),
            meters=Decimal("2000.00"),
            produced_takas=20,
            produced_weight=Decimal("240.00"),
            good_meter=Decimal("1950.00"),
            reject_meter=Decimal("30.00"),
            reprocess_meter=Decimal("20.00"),
            remarks="Shift 1 production",
        )
        self.assertEqual(entry1.opening_pending, Decimal("5000.00"))
        self.assertEqual(entry1.cumulative_meter, Decimal("2000.00"))
        self.assertEqual(entry1.balance_meter, Decimal("3000.00"))
        inst.refresh_from_db()
        self.assertEqual(inst.status, "Partially Completed")

        # 5. Record Daily Entry 2 (3000m) - completes instruction
        entry2 = production_services.record_daily_entry(
            client=self.client_obj,
            instruction=inst,
            entered_by_employee=self.employee,
            entered_by_user=self.user,
            entry_date=date.today(),
            meters=Decimal("3000.00"),
            produced_takas=30,
            produced_weight=Decimal("360.00"),
            good_meter=Decimal("2980.00"),
            reject_meter=Decimal("20.00"),
            reprocess_meter=Decimal("0.00"),
            remarks="Shift 2 completion",
        )
        self.assertEqual(entry2.opening_pending, Decimal("3000.00"))
        self.assertEqual(entry2.cumulative_meter, Decimal("5000.00"))
        self.assertEqual(entry2.balance_meter, Decimal("0.00"))
        inst.refresh_from_db()
        self.assertEqual(inst.status, "Completed")

        # 6. Verify Instruction: moves to PO Pending, creates LotTaka in QC Hold & posts stock movement
        verified_inst = production_services.verify_instruction(
            inst,
            user=self.user,
            reason="Verified quality acceptable",
        )
        self.assertEqual(verified_inst.status, "PO Pending")
        self.assertIsNotNone(verified_inst.grey_lot)
        lot_no = verified_inst.grey_lot.lot_number
        self.assertTrue(lot_no.startswith("LOT-"))

        # Verify LotTaka created in QC Hold
        takas = LotTaka.objects.filter(client=self.client_obj, lot_number=lot_no)
        self.assertEqual(takas.count(), 1)
        taka = takas.first()
        self.assertEqual(taka.status, "QC Hold")
        self.assertEqual(taka.location.code, "SYS-QC-HOLD")

        # Verify stock movement posted
        mov = StockMovement.objects.filter(
            client_id=self.client_obj.id,
            lot_taka_id=taka.id,
            type="PURCHASE",
        ).first()
        self.assertIsNotNone(mov)

        # 7. Generate Final Job Work PO
        updated_inst = production_services.generate_final_job_work_po(verified_inst, user=self.user)
        self.assertIsNotNone(updated_inst.final_po)
        self.assertEqual(updated_inst.status, "PO Created")
        po = updated_inst.final_po
        self.assertEqual(po.line_items.count(), 1)
        po_line = po.line_items.first()
        self.assertEqual(po_line.rate, Decimal("12.00"))

        # 8. 3-way match
        completion = production_services.instruction_completion(updated_inst)
        accepted_qty = Decimal(str(completion["acceptedQty"]))
        match_result = production_services.agency_three_way_match(
            instruction=updated_inst,
            invoice_qty=accepted_qty,
            invoice_rate=Decimal("12.00"),
        )
        self.assertTrue(match_result["matched"])
        self.assertFalse(match_result["requiresApproval"])
