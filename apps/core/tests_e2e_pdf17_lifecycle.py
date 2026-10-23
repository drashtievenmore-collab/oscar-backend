"""End-to-End integration test covering the entire PDF §17 textile manufacturing lifecycle:

1. Production instruction ->
2. Daily entries (25,000 M example: 4,000 + 5,500 + 4,800 = cumulative 14,300, balance 10,700) ->
3. Verification (accepted 24,640) ->
4. Final Job Work PO = Rs 1,35,520 at Rs 5.50/M ->
5. Grey PO / GRN / lot taka register ->
6. JWO outward / partial inward ->
7. QC inspection with reprocess ->
8. Finished lot landed cost (PDF §9 example: Rs 6,15,000 / 9,500 M = Rs 64.74/M) ->
9. Sales order with lot reservation ->
10. Partial dispatch via Delivery Challan ->
11. Sales invoice posting and COGS at landed cost ->
12. Customer payment in and invoice allocation ->
13. Sales return / Credit Note and COGS reversal ->
14. Broker commission accrual/adjustment and Salesperson incentive calculation.

Run with::

    python manage.py test apps.core.tests_e2e_pdf17_lifecycle --keepdb
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting import services as ledger
from apps.accounting.models import Account, BankAccount, JournalEntry, JournalLine
from apps.core.models import CompanyProfile
from apps.hrms.models import Employee
from apps.inventory import costing_services, services as stock
from apps.inventory.models import LotCostEntry, LotTaka, QCInspection, QCInspectionLine, StockMovement
from apps.jobwork.models import JobWorkOrder, JWOInward, JWOInwardTaka, JWOOutward, JWOOutwardTaka, JWOReprocess
from apps.masters.models import Agency, Broker, FabricQuality, Item, JobWorkRateMaster, Location, Party, RateMaster
from apps.pms import production_services as pms_services
from apps.pms.models import DailyProductionEntry, IncentiveCalculation, IncentiveScheme, ProductionInstruction
from apps.purchase import services as purchase_services
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseBill, PurchaseOrder, PurchaseOrderLine
from apps.sales import broker_services, services as sales_services
from apps.sales.models import (
    BrokerCommission,
    DeliveryChallan,
    DeliveryChallanLine,
    PaymentIn,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
)


class PDF17LifecycleE2ETests(TestCase):
    """Full lifecycle verification with exact numerical checks matching PDF specifications."""

    def setUp(self):
        self.tenant = Client.objects.create(
            name="Surat Fine Weaving Mills Pvt Ltd",
            slug="surat-mills-pdf17",
        )
        self.admin = User.objects.create_user(
            email="admin@suratmills.example.com",
            password="securepassword123",
            client=self.tenant,
            is_superuser=True,
            name="Admin Executive",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        # Company statutory profile (Surat, Gujarat - State 24)
        CompanyProfile.objects.create(
            client=self.tenant,
            legal_name="Surat Fine Weaving Mills Pvt Ltd",
            trade_name="Surat Fine Mills",
            gstin="24AAACS1234D1Z5",
            state="Gujarat",
            state_code="24",
            address={"city": "Surat", "line1": "Pandesara GIDC", "pincode": 394221},
        )

        # Seed double-entry Chart of Accounts
        ledger.seed_chart_of_accounts(self.tenant)

        # Locations
        self.main_godown = Location.objects.create(
            client=self.tenant,
            name="Main Godown",
            code="WH-MAIN",
            type="Warehouse",
            is_active=True,
        )
        self.qc_hold = Location.objects.create(
            client=self.tenant,
            name="QC Hold Area",
            code="SYS-QC-HOLD",
            type="Warehouse",
            is_active=True,
        )

        # Bank Account
        bank_ledger = Account.objects.filter(client=self.tenant, system_key="bank").first()
        if not bank_ledger:
            bank_ledger = Account.objects.create(
                client=self.tenant,
                code="1210",
                name="Bank Accounts",
                type="Asset",
                subtype="Bank",
                system_key="bank",
            )
        self.bank_account = BankAccount.objects.create(
            client=self.tenant,
            account=bank_ledger,
            name="HDFC Current Account",
            account_number="50200012345678",
            bank_name="HDFC Bank Ltd",
            ifsc="HDFC0001234",
            branch="Ring Road, Surat",
            is_active=True,
        )

        # Fabric & Item Masters
        self.quality = FabricQuality.objects.create(
            client=self.tenant,
            name="Velvet Cotton 60s",
            code="VC60",
        )
        self.grey_item = Item.objects.create(
            client=self.tenant,
            name="Velvet Cotton 60s Grey",
            sku="GREY-VC60",
            item_kind="Fabric",
            uom="Meter",
            fabric_quality="Velvet Cotton 60s",
            cost_price=Decimal("45.00"),
        )
        self.finished_item = Item.objects.create(
            client=self.tenant,
            name="Velvet Cotton 60s Finished Dyed",
            sku="DYED-VC60",
            item_kind="Fabric",
            uom="Meter",
            fabric_quality="Velvet Cotton 60s",
            cost_price=Decimal("65.00"),
        )

        # Parties
        self.agency_party = Party.objects.create(
            client=self.tenant,
            name="Apex External Weaving Agency",
            code="AG-APEX",
            type="Vendor",
            gstin="24AAACA9876M1Z2",
            place_of_supply="24-Gujarat",
        )
        self.agency_master = Agency.objects.create(
            client=self.tenant,
            name="Apex External Weaving Agency",
            code="AG-APEX",
        )
        self.processor_party = Party.objects.create(
            client=self.tenant,
            name="Surat Dyeing & Printing Works",
            code="VEND-SURAT-DYE",
            type="Vendor",
            gstin="24AAACS5555L1Z1",
            place_of_supply="24-Gujarat",
        )
        self.customer = Party.objects.create(
            client=self.tenant,
            name="Royal Fashion Textiles",
            code="CUST-ROYAL",
            type="Customer",
            gstin="27AAACR1111K1Z9",
            place_of_supply="27-Maharashtra",
        )
        self.broker = Broker.objects.create(
            client=self.tenant,
            name="Surat Textile Brokers",
            code="BRK-STB",
            commission_type="percent",
            default_value=Decimal("2.00"),
            phone="9825000000",
        )

        # Employee (Supervisor / Salesperson)
        self.employee = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-101",
            name="Ramesh Kumar",
            email="ramesh@suratmills.example.com",
            joining_date=date(2026, 1, 1),
            employment_type="Regular",
            status="Active",
        )

        # Rate Master for pricing & margin control
        RateMaster.objects.create(
            client=self.tenant,
            item=self.finished_item,
            rate=Decimal("85.00"),
            min_selling_rate=Decimal("70.00"),
            uom="Meter",
            rate_type="Sales",
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
        )

    def test_01_complete_pdf17_e2e_lifecycle(self):
        """Execute and verify the complete 14-step textile manufacturing and commercial lifecycle."""

        # =====================================================================
        # Step 1: Production Instruction (25,000 M order)
        # =====================================================================
        pi = ProductionInstruction.objects.create(
            client=self.tenant,
            instruction_number="PROD-2026-0001",
            agency_name=self.agency_party.name,
            agency=self.agency_master,
            order_reference="PO-ORIG-001",
            order_meter=Decimal("25000.00"),
            fabric=self.grey_item.name,
            agreed_job_rate=Decimal("5.50"),
            agreed_rate=Decimal("5.50"),
            employee=self.employee,
            supervisor=self.employee,
            status="In Progress",
            pi_date=date(2026, 4, 1),
        )
        self.assertEqual(pi.order_meter, Decimal("25000.00"))
        self.assertEqual(pi.agreed_job_rate, Decimal("5.50"))

        # Initial progress check
        init_progress = pms_services.instruction_progress(pi)
        self.assertEqual(init_progress["cumulative"], Decimal("0.00"))
        self.assertEqual(init_progress["balance"], Decimal("25000.00"))

        # =====================================================================
        # Step 2: Daily Entries (4,000 + 5,500 + 4,800 = cumulative 14,300, balance 10,700)
        # =====================================================================
        # Day 1: 4,000 M
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=pi,
            entry_date=date(2026, 4, 2),
            meters=Decimal("4000.00"),
            good_meter=Decimal("4000.00"),
            entered_by_employee=self.employee,
        )
        # Day 2: 5,500 M
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=pi,
            entry_date=date(2026, 4, 3),
            meters=Decimal("5500.00"),
            good_meter=Decimal("5500.00"),
            entered_by_employee=self.employee,
        )
        # Day 3: 4,800 M
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=pi,
            entry_date=date(2026, 4, 4),
            meters=Decimal("4800.00"),
            good_meter=Decimal("4800.00"),
            entered_by_employee=self.employee,
        )

        # Check PDF §17 intermediate exact values
        prog3 = pms_services.instruction_progress(pi)
        self.assertEqual(prog3["cumulative"], Decimal("14300.00"), "Cumulative must be exactly 14,300 M after 3 days")
        self.assertEqual(prog3["balance"], Decimal("10700.00"), "Balance must be exactly 10,700 M after 3 days")

        # Day 4: Final run entry of 10,340 M (Total = 24,640 M, legitimate shortage = 360 M)
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=pi,
            entry_date=date(2026, 4, 5),
            meters=Decimal("10340.00"),
            good_meter=Decimal("10340.00"),
            entered_by_employee=self.employee,
        )

        completion = pms_services.instruction_completion(pi)
        self.assertEqual(completion["totalProduced"], Decimal("24640.00"))
        self.assertEqual(completion["acceptedQty"], Decimal("24640.00"))
        self.assertEqual(completion["shortageQty"], Decimal("360.00"))

        # =====================================================================
        # Step 3: Verification (accepted 24,640 M)
        # =====================================================================
        verified_pi = pms_services.verify_instruction(
            pi,
            user=self.admin,
            action="approve_complete",
            rejected_qty=Decimal("0.00"),
        )
        self.assertIn(verified_pi.status, ["Verified", "PO Pending"])
        self.assertEqual(verified_pi.rejected_qty, Decimal("0.00"))

        # =====================================================================
        # Step 4: Final Job Work PO = Rs 1,35,520 at Rs 5.50/M
        # =====================================================================
        updated_pi = pms_services.generate_final_job_work_po(
            verified_pi,
            user=self.admin,
            custom_data={"acceptedQty": Decimal("24640.00"), "rate": Decimal("5.50")},
        )
        self.assertIsNotNone(updated_pi.final_po)
        final_po = updated_pi.final_po
        final_line = final_po.line_items.first()
        self.assertIsNotNone(final_line)
        self.assertEqual(final_line.qty, Decimal("24640.00"))
        self.assertEqual(final_line.rate, Decimal("5.50"))

        # 24,640 M * Rs 5.50/M = Rs 1,35,520.00
        expected_jw_po_total = Decimal("135520.00")
        self.assertEqual(final_po.subtotal, expected_jw_po_total, "Final Job Work PO subtotal must be exactly Rs 1,35,520.00")

        # =====================================================================
        # Step 5: Grey Purchase PO / GRN / Lot Taka Register
        # =====================================================================
        grey_po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number="PO-GREY-2026-001",
            party=self.agency_party,
            doc_date=date(2026, 4, 6),
            status="Approved",
            purchase_type="Ready Grey Fabric Purchase",
            subtotal=Decimal("450000.00"),
            taxable_value=Decimal("450000.00"),
            total=Decimal("450000.00"),
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=grey_po,
            item=self.grey_item,
            qty=Decimal("10000.00"),
            rate=Decimal("45.00"),
            line_total=Decimal("450000.00"),
        )
        grey_grn = GoodsReceipt.objects.create(
            client=self.tenant,
            purchase_order=grey_po,
            grn_number="GRN-GREY-2026-001",
            party=self.agency_party,
            location=self.main_godown,
            receipt_date=date(2026, 4, 7),
            qc_status="Passed",
        )
        GoodsReceiptLine.objects.create(
            client=self.tenant,
            goods_receipt=grey_grn,
            item=self.grey_item,
            ordered_qty=Decimal("10000.00"),
            received_qty=Decimal("10000.00"),
            unit_cost=Decimal("45.00"),
        )

        # Create grey lot and takas in Main Godown
        grey_lot_no = "LOT-GREY-101"
        grey_taka_1 = LotTaka.objects.create(
            client=self.tenant,
            lot_number=grey_lot_no,
            taka_number="TK-G1",
            item=self.grey_item,
            location=self.main_godown,
            received_quantity=Decimal("5000.00"),
            available_quantity=Decimal("5000.00"),
            rate=Decimal("45.00"),
            status=LotTaka.STATUS_AVAILABLE,
            qc_status="Passed",
            source_document_number=grey_grn.grn_number,
        )
        grey_taka_2 = LotTaka.objects.create(
            client=self.tenant,
            lot_number=grey_lot_no,
            taka_number="TK-G2",
            item=self.grey_item,
            location=self.main_godown,
            received_quantity=Decimal("5000.00"),
            available_quantity=Decimal("5000.00"),
            rate=Decimal("45.00"),
            status=LotTaka.STATUS_AVAILABLE,
            qc_status="Passed",
            source_document_number=grey_grn.grn_number,
        )
        self.assertEqual(
            LotTaka.objects.filter(client=self.tenant, lot_number=grey_lot_no).aggregate(s=Sum("received_quantity"))["s"],
            Decimal("10000.00"),
        )

        # =====================================================================
        # Step 6: JWO Outward & Partial Inward
        # =====================================================================
        jwo = JobWorkOrder.objects.create(
            client=self.tenant,
            jwo_no="JWO-2026-001",
            vendor=self.processor_party,
            vendor_name=self.processor_party.name,
            process="Dyeing & Finishing",
            order_date=date(2026, 4, 8),
            planned_qty=Decimal("10000.00"),
            rate=Decimal("8.50"),
            status="In-Process",
        )

        # Outward: 10,000 M sent to processor
        outward = JWOOutward.objects.create(
            client=self.tenant,
            job_work_order=jwo,
            no="JWO-OUT-001",
            date=date(2026, 4, 9),
            qty=Decimal("10000.00"),
            status="Sent",
        )
        JWOOutwardTaka.objects.create(client=self.tenant, outward=outward, lot_taka=grey_taka_1, meters=Decimal("5000.00"))
        JWOOutwardTaka.objects.create(client=self.tenant, outward=outward, lot_taka=grey_taka_2, meters=Decimal("5000.00"))
        grey_taka_1.status = LotTaka.STATUS_EXTERNAL_WIP
        grey_taka_1.available_quantity = Decimal("0.00")
        grey_taka_1.save()
        grey_taka_2.status = LotTaka.STATUS_EXTERNAL_WIP
        grey_taka_2.available_quantity = Decimal("0.00")
        grey_taka_2.save()

        # Inward: partial receipt of 9,500 M finished fabric (500 M process shrinkage/loss)
        inward = JWOInward.objects.create(
            client=self.tenant,
            job_work_order=jwo,
            no="JWO-IN-001",
            date=date(2026, 4, 15),
            qty=Decimal("9500.00"),
            accepted=Decimal("9100.00"),
            rejected=Decimal("0.00"),
            status="Received",
        )

        finished_lot_no = "LOT-FIN-101"
        fin_taka_1 = LotTaka.objects.create(
            client=self.tenant,
            lot_number=finished_lot_no,
            taka_number="TK-F1",
            item=self.finished_item,
            location=self.main_godown,
            received_quantity=Decimal("5000.00"),
            available_quantity=Decimal("5000.00"),
            status=LotTaka.STATUS_AVAILABLE,
            qc_status="Passed",
            parent_taka=grey_taka_1,
        )
        fin_taka_2 = LotTaka.objects.create(
            client=self.tenant,
            lot_number=finished_lot_no,
            taka_number="TK-F2",
            item=self.finished_item,
            location=self.main_godown,
            received_quantity=Decimal("4500.00"),
            available_quantity=Decimal("4500.00"),
            status=LotTaka.STATUS_AVAILABLE,
            qc_status="Passed",
            parent_taka=grey_taka_2,
        )

        # =====================================================================
        # Step 7: QC with Reprocess Cycle
        # =====================================================================
        qc = QCInspection.objects.create(
            client=self.tenant,
            inspection_number="QC-2026-001",
            inspection_date=date(2026, 4, 16),
            fabric_item=self.finished_item,
            status="Completed",
            total_received_qty=Decimal("9500.00"),
            total_inspected_qty=Decimal("9500.00"),
            passed_qty=Decimal("9100.00"),
            reprocess_qty=Decimal("400.00"),
            rejected_qty=Decimal("0.00"),
        )
        QCInspectionLine.objects.create(
            client=self.tenant,
            inspection=qc,
            lot_taka=fin_taka_1,
            lot_number=finished_lot_no,
            taka_number="TK-F1",
            original_meter=Decimal("5000.00"),
            actual_meter=Decimal("5000.00"),
            disposition="Pass",
        )
        QCInspectionLine.objects.create(
            client=self.tenant,
            inspection=qc,
            lot_taka=fin_taka_2,
            lot_number=finished_lot_no,
            taka_number="TK-F2",
            original_meter=Decimal("4500.00"),
            actual_meter=Decimal("4100.00"),
            disposition="Pass",
        )
        QCInspectionLine.objects.create(
            client=self.tenant,
            inspection=qc,
            lot_taka=fin_taka_2,
            lot_number=finished_lot_no,
            taka_number="TK-F2-RP",
            original_meter=Decimal("400.00"),
            actual_meter=Decimal("400.00"),
            disposition="Reprocess",
            remarks="Minor shade difference at roll selvedge",
        )

        # Record Reprocess under JWO
        reprocess = JWOReprocess.objects.create(
            client=self.tenant,
            job_work_order=jwo,
            no="RP-2026-001",
            inward_no=inward.no,
            date=date(2026, 4, 17),
            qty=Decimal("400.00"),
            reason="Selvedge Shade Re-dyeing",
            status="Completed",
        )
        self.assertEqual(reprocess.qty, Decimal("400.00"))

        # =====================================================================
        # Step 8: Finished Lot Landed Cost (PDF §9 example: Rs 6,15,000 / 9,500 M = Rs 64.74/M)
        # =====================================================================
        # Post the cost ledger elements:
        # Grey purchase: Rs 5,20,000.00
        # Processing: Rs 80,750.00
        # Transport / Freight: Rs 12,250.00
        # Reprocess: Rs 2,000.00
        # Total: Rs 6,15,000.00 for 9,500 good finished meters
        costing_services.post_lot_cost(
            client_id=self.tenant.id,
            lot_number=finished_lot_no,
            cost_type="Grey purchase",
            amount=Decimal("520000.00"),
            quantity=Decimal("9500.00"),
            source_document_type="GoodsReceipt",
            source_document_number=grey_grn.grn_number,
            user=self.admin,
        )
        costing_services.post_lot_cost(
            client_id=self.tenant.id,
            lot_number=finished_lot_no,
            cost_type="Dyeing/Printing/Finishing",
            amount=Decimal("80750.00"),
            quantity=Decimal("9500.00"),
            source_document_type="JobWorkInward",
            source_document_number=inward.no,
            user=self.admin,
        )
        costing_services.post_lot_cost(
            client_id=self.tenant.id,
            lot_number=finished_lot_no,
            cost_type="Purchase freight/loading",
            amount=Decimal("12250.00"),
            quantity=Decimal("9500.00"),
            source_document_type="TransporterBill",
            source_document_number="TR-901",
            user=self.admin,
        )
        costing_services.post_lot_cost(
            client_id=self.tenant.id,
            lot_number=finished_lot_no,
            cost_type="Reprocess",
            amount=Decimal("2000.00"),
            quantity=Decimal("400.00"),
            source_document_type="JWOReprocess",
            source_document_number=reprocess.no,
            user=self.admin,
        )

        cost_info = costing_services.get_lot_landed_cost(self.tenant.id, finished_lot_no)
        self.assertEqual(Decimal(cost_info["totalCost"]), Decimal("615000.00"), "Total lot cost must be exactly Rs 6,15,000.00")
        self.assertEqual(Decimal(cost_info["goodFinishedMeters"]), Decimal("9500.00"), "Finished quantity must be 9,500 M")

        # 615,000 / 9,500 = 64.736842... -> rounds to exactly 64.74 / M
        exact_cost_per_meter = Decimal(cost_info["costPerMeter"])
        rounded_landed_cost = round(exact_cost_per_meter, 2)
        self.assertEqual(rounded_landed_cost, Decimal("64.74"), "Landed cost per meter must match PDF §9 example of Rs 64.74/M")

        # Margin check: selling at Rs 85.00 vs landed cost Rs 64.74
        below_cost, below_margin, landed_val, margin_pct = costing_services.check_item_margin(
            self.tenant.id, self.finished_item.id, Decimal("85.00"), lot_number=finished_lot_no
        )
        self.assertFalse(below_cost)
        self.assertFalse(below_margin)
        self.assertGreater(margin_pct, Decimal("20.00"))

        # =====================================================================
        # Step 9: Sales Order with Lot Reservation
        # =====================================================================
        so = SalesOrder.objects.create(
            client=self.tenant,
            order_number="SO-2026-0001",
            party=self.customer,
            party_name=self.customer.name,
            doc_date=date(2026, 4, 18),
            stage="Draft",
            salesperson_fk=self.employee,
            salesperson=self.employee.name,
            broker_fk=self.broker,
            broker=self.broker.name,
            subtotal=Decimal("425000.00"),
            taxable_value=Decimal("425000.00"),
            igst=Decimal("21250.00"),
            total_tax=Decimal("21250.00"),
            total=Decimal("446250.00"),
        )
        so_line = SalesOrderLine.objects.create(
            client=self.tenant,
            sales_order=so,
            line_no=1,
            item=self.finished_item,
            item_name=self.finished_item.name,
            qty=Decimal("5000.00"),
            rate=Decimal("85.00"),
            amount=Decimal("425000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("21250.00"),
            line_total=Decimal("446250.00"),
            reserved_takas=[
                {"lotTakaId": str(fin_taka_1.id), "meters": 5000},
            ],
        )

        # Confirming the order reserves the allocated takas
        so.stage = "Confirmed"
        so.save()
        sales_services.reserve_order_takas(so)

        fin_taka_1.refresh_from_db()
        self.assertEqual(fin_taka_1.status, LotTaka.STATUS_RESERVED, "Lot taka must be Reserved upon sales order confirmation")

        # =====================================================================
        # Step 10: Partial Dispatch (3,000 M of 5,000 M) via Delivery Challan
        # =====================================================================
        # Post initial stock movement into warehouse so on-hand stock is available for dispatch
        stock.post_movement(
            client_id=self.tenant.id,
            item=self.finished_item,
            location=self.main_godown,
            type="INWARD_FINISHING",
            quantity=Decimal("9500.00"),
            unit_cost=Decimal("64.74"),
            reference_id=inward.id,
            reference_type="JobWorkInward",
            reference_number=inward.no,
        )

        challan = DeliveryChallan.objects.create(
            client=self.tenant,
            challan_number="DC-2026-0001",
            sales_order=so,
            party=self.customer,
            location=self.main_godown,
            doc_date=date(2026, 4, 20),
            status="Draft",
        )
        dc_line = DeliveryChallanLine.objects.create(
            client=self.tenant,
            delivery_challan=challan,
            sales_order_line=so_line,
            line_no=1,
            item=self.finished_item,
            item_name=self.finished_item.name,
            qty=Decimal("3000.00"),
            rate=Decimal("85.00"),
            amount=Decimal("255000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("12750.00"),
            line_total=Decimal("267750.00"),
        )

        sales_services.dispatch_challan(challan, user=self.admin)
        challan.refresh_from_db()
        self.assertEqual(challan.status, "Dispatched")

        so.refresh_from_db()
        so_line.refresh_from_db()
        self.assertEqual(so_line.dispatched_qty, Decimal("3000.00"))
        self.assertEqual(so.stage, "Partially Dispatched", "Sales order stage must advance to Partially Dispatched")

        # =====================================================================
        # Step 11: Sales Invoice & COGS Posting
        # =====================================================================
        invoice = SalesInvoice.objects.create(
            client=self.tenant,
            invoice_number="INV-2026-0001",
            sales_order=so,
            delivery_challan=challan,
            party=self.customer,
            party_name=self.customer.name,
            party_gstin=self.customer.gstin,
            place_of_supply="27",
            doc_date=date(2026, 4, 20),
            location=self.main_godown,
            salesperson_fk=self.employee,
            salesperson=self.employee.name,
            broker_fk=self.broker,
            broker=self.broker.name,
            subtotal=Decimal("255000.00"),
            taxable_value=Decimal("255000.00"),
            igst=Decimal("12750.00"),
            total_tax=Decimal("12750.00"),
            total=Decimal("267750.00"),
            status="Draft",
        )
        inv_line = SalesInvoiceLine.objects.create(
            client=self.tenant,
            sales_invoice=invoice,
            delivery_challan_line=dc_line,
            line_no=1,
            item=self.finished_item,
            item_name=self.finished_item.name,
            qty=Decimal("3000.00"),
            rate=Decimal("85.00"),
            amount=Decimal("255000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("12750.00"),
            line_total=Decimal("267750.00"),
        )

        sales_services.finalize_invoice(invoice, user=self.admin)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Unpaid")

        # Verify balanced journal entries for the sales invoice (Revenue + COGS at landed cost)
        j_lines = JournalLine.objects.filter(
            client=self.tenant,
            journal_entry__source_document_type="SalesInvoice",
            journal_entry__source_document_id=invoice.id,
        )
        self.assertGreater(j_lines.count(), 0)
        tot_dr = sum((l.debit for l in j_lines), Decimal("0.00"))
        tot_cr = sum((l.credit for l in j_lines), Decimal("0.00"))
        self.assertEqual(tot_dr, tot_cr, "Sales invoice journal posting must balance (Debits == Credits)")
        # Total debits = Debtors Dr (267,750.00) + COGS Dr (194,220.00) = 461,970.00
        self.assertEqual(tot_dr, Decimal("461970.00"))
        debtor_line = j_lines.filter(account__system_key="debtors").first()
        self.assertIsNotNone(debtor_line)
        self.assertEqual(debtor_line.debit, Decimal("267750.00"), "Invoice total receivables must be Rs 2,67,750.00")
        cogs_line = j_lines.filter(account__system_key="cogs").first()
        self.assertIsNotNone(cogs_line)
        self.assertEqual(cogs_line.debit, Decimal("194220.00"), "COGS must be 3,000 M * Rs 64.74 = Rs 1,94,220.00")

        # =====================================================================
        # Step 12: Customer Payment In & Invoice Allocation
        # =====================================================================
        payment = sales_services.record_payment_in(
            client=self.tenant,
            party=self.customer,
            amount=Decimal("200000.00"),
            payment_date=date(2026, 4, 25),
            mode="Bank",
            bank_account=self.bank_account,
            reference_number="NEFT-ROYAL-789",
            user=self.admin,
            allocations=[{"invoiceId": invoice.id, "amount": Decimal("200000.00")}],
        )
        self.assertEqual(payment.amount, Decimal("200000.00"))

        outstanding_info = sales_services.invoice_outstanding(invoice)
        expected_outstanding = Decimal("267750.00") - Decimal("200000.00")
        self.assertEqual(outstanding_info["paid"], Decimal("200000.00"))
        self.assertEqual(outstanding_info["outstanding"], expected_outstanding, "Outstanding must be reduced to Rs 67,750.00")

        # =====================================================================
        # Step 13: Customer Return / Credit Note (200 M returned)
        # =====================================================================
        ret_resp = self.api.post("/api/v1/sales/returns/", {
            "salesInvoiceId": str(invoice.id),
            "partyId": str(self.customer.id),
            "salespersonId": str(self.employee.id),
            "date": "2026-04-28",
            "locationId": str(self.main_godown.id),
            "lineItems": [{
                "itemId": str(self.finished_item.id),
                "qty": 200,
                "returnedQty": 200,
                "rate": 85,
                "tax": 5,
                "salesInvoiceLineId": str(inv_line.id),
            }],
        }, format="json")
        self.assertEqual(ret_resp.status_code, 201, ret_resp.content[:600])
        sales_return = SalesReturn.objects.get(pk=ret_resp.json()["id"])
        self.assertEqual(sales_return.status, "Posted")
        self.assertEqual(sales_return.subtotal, Decimal("17000.00"))

        # =====================================================================
        # Step 14: Broker Commission & Salesperson Incentive
        # =====================================================================
        # 1. Broker Commission: 2% on taxable value of invoice (Rs 2,55,000.00) = Rs 5,100.00
        comm = broker_services.calculate_broker_commission(invoice, user=self.admin)
        self.assertIsNotNone(comm)
        self.assertEqual(comm.rate, Decimal("2.00"))
        self.assertEqual(comm.commission_amount, Decimal("5100.00"))

        # Reversal on 200 M return (Rs 17,000 taxable * 2% = Rs 340 reduction)
        broker_services.reverse_broker_commission_for_invoice(
            invoice,
            user=self.admin,
            return_val=Decimal("17000.00"),
            reason="Customer Return 200 M",
        )
        comm.refresh_from_db()
        # Adjusted commission: 5,100 - 340 = Rs 4,760.00
        self.assertEqual(comm.commission_amount, Decimal("4760.00"), "Net broker commission after return adjustment must be Rs 4,760.00")

        # 2. Salesperson Incentive: 1.5% on Net Sales (Gross Rs 2,55,000 - Return Rs 17,000 = Rs 2,38,000)
        scheme = IncentiveScheme.objects.create(
            client=self.tenant,
            name="1.5% Finished Fabric Sales Incentive",
            rate_pct=Decimal("1.50"),
            basis="INVOICE",
            exclude_gst=True,
            is_active=True,
            created_by=self.admin,
        )

        calc = pms_services.calculate_incentive(
            client=self.tenant,
            scheme=scheme,
            employee=self.employee,
            period_label="2026-APR",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            user=self.admin,
        )
        self.assertIsNotNone(calc)
        # Net sales = 2,55,000 - 17,000 = Rs 2,38,000.00. 1.5% of 2,38,000 = Rs 3,570.00.
        expected_incentive = Decimal("3570.00")
        self.assertEqual(calc.incentive_amount, expected_incentive, "Salesperson incentive must be exactly Rs 3,570.00")
        self.assertEqual(calc.status, "Posted")
        self.assertIsNotNone(calc.journal_entry)

        # Verify incentive journal posting balances
        inc_lines = calc.journal_entry.lines.all()
        tot_inc_dr = sum((l.debit for l in inc_lines), Decimal("0.00"))
        tot_inc_cr = sum((l.credit for l in inc_lines), Decimal("0.00"))
        self.assertEqual(tot_inc_dr, tot_inc_cr, "Incentive journal entry must balance")
        self.assertEqual(tot_inc_dr, expected_incentive)
