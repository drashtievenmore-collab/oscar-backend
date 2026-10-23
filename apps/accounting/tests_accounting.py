"""Accounting & Financial Reporting Tests (PDF §08, §12).

Tests:
1. Auto-posting balance integrity (total debits == total credits) for:
   - Sales invoice
   - Payment in (Customer receipt)
   - Purchase bill
   - Payment out (Vendor payment)
   - Direct expense
   - Sample consumption (Dr Sample Expense / Cr Stock)
   - Stock adjustment
2. Financial statements integrity:
   - Profit and Loss (Revenue - COGS = Gross Profit)
   - Balance Sheet (Assets = Liabilities + Equity)
   - Day Book (Debit total == Credit total)
   - Trial Balance (Sum of debits == Sum of credits)
   - AR / AP Ageing buckets (0-30, 31-60, 61-90, 91-120, 120+)
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.models import Account, BankAccount, JournalEntry, JournalLine
from apps.accounting import reports, services
from apps.core.models import CompanyProfile
from apps.inventory.models import LotTaka, Sample, StockMovement
from apps.inventory import sampling_services
from apps.masters.models import Item, Location, Party
from apps.purchase.models import Expense, PaymentOut, PurchaseBill, PurchaseBillLine
from apps.sales.models import PaymentIn, SalesInvoice, SalesInvoiceLine


class AccountingPostingAndReportsTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="acct-test-tenant", defaults={"name": "Accounting Test Tenant"}
        )
        self.user = User.objects.create_user(
            email="acct-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="Accounting Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        CompanyProfile.objects.get_or_create(
            client=self.client_obj,
            defaults={
                "legal_name": "Apex Fine Cottons Pvt Ltd",
                "trade_name": "Apex Fine Cottons",
                "gstin": "24AAACA1234B1Z2",
                "state": "Gujarat",
                "state_code": "24",
                "address": {"city": "Surat", "line1": "Ring Road", "pincode": 395002},
            },
        )

        services.seed_chart_of_accounts(self.client_obj)

        self.customer = Party.objects.create(
            client=self.client_obj,
            name="Radhika Silk Palace",
            code="CUST-RSP",
            type="Customer",
            gstin="24AAACR5555A1Z3",
            place_of_supply="24-Gujarat",
        )

        self.supplier = Party.objects.create(
            client=self.client_obj,
            name="Gujarat Grey Yarn Mills",
            code="SUPP-GGYM",
            type="Supplier",
            gstin="24AAACG8888F1Z9",
            place_of_supply="24-Gujarat",
        )

        self.location = Location.objects.create(
            client=self.client_obj,
            name="Central Warehouse",
            code="WH-CENTRAL",
            type="Godown",
            is_active=True,
        )

        self.item = Item.objects.create(
            client=self.client_obj,
            name="Cotton Satin Printed 60s",
            sku="CSP-60S",
            item_kind="Fabric",
            uom="Meter",
            cost_price=Decimal("80.00"),
        )

        # Bank Account
        bank_acc = Account.objects.create(
            client=self.client_obj,
            code="1210",
            name="HDFC Current Account",
            type="Asset",
            subtype="Bank",
        )
        self.bank_account = BankAccount.objects.create(
            client=self.client_obj,
            account=bank_acc,
            name="HDFC Current Account",
            bank_name="HDFC Bank",
            account_number="50200012345678",
            ifsc="HDFC0001234",
            branch="Ring Road Surat",
        )

    def _assert_entry_balanced(self, entry):
        """Assert a JournalEntry exists, has postings, and sum(debit) == sum(credit)."""
        self.assertIsNotNone(entry, "JournalEntry must not be None")
        lines = entry.lines.filter(deleted_at__isnull=True)
        self.assertTrue(lines.exists(), f"Entry {entry.entry_number} has no lines")
        total_debit = lines.aggregate(Sum("debit"))["debit__sum"] or Decimal("0.00")
        total_credit = lines.aggregate(Sum("credit"))["credit__sum"] or Decimal("0.00")
        self.assertEqual(
            total_debit,
            total_credit,
            f"Unbalanced entry {entry.entry_number}: Debits ₹{total_debit} != Credits ₹{total_credit}",
        )
        return total_debit

    # -----------------------------------------------------------------------
    # 1. Posting Balance Tests
    # -----------------------------------------------------------------------

    def test_01_sales_invoice_posting_balanced(self):
        """Sales invoice posting debits Debtors and credits Sales & GST output with perfect balance."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            party_name=self.customer.name,
            party_gstin=self.customer.gstin,
            place_of_supply="24",
            doc_date=date.today(),
            invoice_number="INV-ACCT-001",
            status="Posted",
            location=self.location,
            subtotal=Decimal("100000.00"),
            taxable_value=Decimal("100000.00"),
            cgst=Decimal("2500.00"),
            sgst=Decimal("2500.00"),
            total_tax=Decimal("5000.00"),
            round_off=Decimal("0.00"),
            total=Decimal("105000.00"),
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("100.00"),
            amount=Decimal("100000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("5000.00"),
            line_total=Decimal("105000.00"),
        )
        entry = services.post_sales_invoice(invoice, user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertGreaterEqual(total, Decimal("105000.00"))

    def test_02_payment_in_posting_balanced(self):
        """Payment received from customer debits Bank and credits Debtors."""
        payment = PaymentIn.objects.create(
            client=self.client_obj,
            party=self.customer,
            payment_number="PAYIN-001",
            payment_date=date.today(),
            amount=Decimal("50000.00"),
            mode="Bank",
            bank_account=self.bank_account,
            status="Active",
        )
        entry = services.post_payment_in(payment, user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertEqual(total, Decimal("50000.00"))

    def test_03_purchase_bill_posting_balanced(self):
        """Purchase bill debits Purchases/GST Input and credits Creditors."""
        bill = PurchaseBill.objects.create(
            client=self.client_obj,
            party=self.supplier,
            party_name=self.supplier.name,
            party_gstin=self.supplier.gstin,
            place_of_supply="24",
            bill_number="BILL-ACCT-001",
            doc_date=date.today(),
            status="Posted",
            location=self.location,
            subtotal=Decimal("60000.00"),
            taxable_value=Decimal("60000.00"),
            cgst=Decimal("1500.00"),
            sgst=Decimal("1500.00"),
            total_tax=Decimal("3000.00"),
            total=Decimal("63000.00"),
        )
        PurchaseBillLine.objects.create(
            client=self.client_obj,
            purchase_bill=bill,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("60.00"),
            amount=Decimal("60000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("3000.00"),
            line_total=Decimal("63000.00"),
        )
        entry = services.post_purchase_bill(bill, user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertEqual(total, Decimal("63000.00"))

    def test_04_payment_out_posting_balanced(self):
        """Payment out to vendor debits Creditors and credits Bank."""
        payment = PaymentOut.objects.create(
            client=self.client_obj,
            party=self.supplier,
            payment_number="PAYOUT-001",
            payment_date=date.today(),
            amount=Decimal("30000.00"),
            mode="Bank",
            bank_account=self.bank_account,
            status="Posted",
        )
        entry = services.post_payment_out(payment, user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertEqual(total, Decimal("30000.00"))

    def test_05_direct_expense_posting_balanced(self):
        """Direct operating expense debits Expense head / GST and credits Bank."""
        expense = Expense.objects.create(
            client=self.client_obj,
            expense_number="EXP-001",
            expense_date=date.today(),
            amount=Decimal("10000.00"),
            tax_amount=Decimal("1800.00"),
            total=Decimal("11800.00"),
            payment_mode="Bank",
            bank_account=self.bank_account,
            notes="Factory Office Electricity",
        )
        entry = services.post_expense(expense, user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertEqual(total, Decimal("11800.00"))

    def test_06_sample_consumption_posting_balanced(self):
        """Sample consumption debits Sample Expense and credits Stock."""
        taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-SMPL-01",
            taka_number="TK-01",
            item=self.item,
            location=self.location,
            received_quantity=Decimal("100.00"),
            available_quantity=Decimal("100.00"),
            rate=Decimal("80.00"),
        )
        sample = Sample.objects.create(
            client=self.client_obj,
            sample_number="SMPL-ACCT-01",
            sample_date=date.today(),
            status="Issued",
            quantity=Decimal("100.00"),
            consumed_quantity=Decimal("0.00"),
            source_lot_taka=taka,
            party=self.customer,
        )
        consumed = sampling_services.consume_sample(
            sample,
            consume_qty=Decimal("25.00"),
            user=self.user,
            remarks="Client swatch approval",
        )
        self.assertEqual(consumed.status, "Partially Returned")
        self.assertIsNotNone(consumed.journal_entry)
        total = self._assert_entry_balanced(consumed.journal_entry)
        # 25m * Rs 80 = Rs 2000
        self.assertEqual(total, Decimal("2000.00"))

    def test_07_stock_adjustment_posting_balanced(self):
        """Stock adjustment debits Stock/Adjustment account and credits opposite side with balance."""
        movement = StockMovement.objects.create(
            client=self.client_obj,
            item=self.item,
            location=self.location,
            type="ADJUSTMENT",
            quantity=Decimal("50.00"),
            unit_cost=Decimal("80.00"),
            movement_date=date.today(),
            notes="Physical inventory recount surplus",
        )
        entry = services.post_stock_adjustment(movement, value=Decimal("4000.00"), user=self.user)
        total = self._assert_entry_balanced(entry)
        self.assertEqual(total, Decimal("4000.00"))

    # -----------------------------------------------------------------------
    # 2. Financial Statements & MIS Totals Tests
    # -----------------------------------------------------------------------

    def test_08_profit_and_loss_totals(self):
        """P&L correctly computes Revenue, COGS, Gross Profit, Operating Expenses, and Net."""
        self.test_01_sales_invoice_posting_balanced()
        self.test_05_direct_expense_posting_balanced()

        pl = reports.profit_and_loss(self.client_obj.id)
        self.assertIn("revenue", pl)
        self.assertIn("grossProfit", pl)
        self.assertIn("operatingExpenses", pl)
        self.assertIn("netOperatingProfit", pl)

        self.assertEqual(pl["revenue"], Decimal("100000.00"))
        self.assertEqual(pl["operatingExpenses"], Decimal("10000.00"))
        # grossProfit = revenue - cogs
        self.assertEqual(pl["grossProfit"], pl["revenue"] - pl["cogs"])
        # netOperatingProfit = grossProfit - operatingExpenses
        self.assertEqual(pl["netOperatingProfit"], pl["grossProfit"] - pl["operatingExpenses"])

    def test_09_balance_sheet_structure_and_equation(self):
        """Balance sheet returns assets, liabilities, and equity with balanced equation."""
        self.test_01_sales_invoice_posting_balanced()
        self.test_02_payment_in_posting_balanced()

        bs = reports.balance_sheet(self.client_obj.id)
        self.assertIn("assets", bs)
        self.assertIn("liabilities", bs)
        self.assertIn("equity", bs)
        self.assertIn("totalAssets", bs)
        self.assertIn("totalLiabilities", bs)
        self.assertIn("totalEquity", bs)
        self.assertEqual(bs["difference"], Decimal("0.00"))

    def test_10_day_book_daily_totals(self):
        """Day book sums all daily transactions and position numbers match vouchers."""
        self.test_01_sales_invoice_posting_balanced()
        self.test_02_payment_in_posting_balanced()
        self.test_03_purchase_bill_posting_balanced()

        db = reports.day_book(self.client_obj.id, as_of_date=date.today())
        self.assertIn("summary", db)
        summary = db["summary"]
        self.assertEqual(summary["salesTotal"], Decimal("105000.00"))
        self.assertEqual(summary["collectionTotal"], Decimal("50000.00"))
        self.assertEqual(summary["purchaseTotal"], Decimal("63000.00"))
        self.assertGreater(len(db["salesVouchers"]), 0)

    def test_11_trial_balance_totals(self):
        """Trial balance extracts all active accounts and confirms aggregate debits == credits."""
        self.test_01_sales_invoice_posting_balanced()
        self.test_03_purchase_bill_posting_balanced()

        rows = services.trial_balance(self.client_obj.id)
        total_debit = sum(r["debit"] for r in rows)
        total_credit = sum(r["credit"] for r in rows)
        self.assertEqual(total_debit, total_credit)

    def test_12_ar_ap_ageing_buckets(self):
        """AR / AP ageing partitions documents into 0-30, 31-60, 61-90, 91-120, and 120+ day buckets."""
        today = date.today()
        # Create invoices with different dates to test bucket placement
        inv1 = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            party_name=self.customer.name,
            invoice_number="INV-AGE-10",
            doc_date=today - timedelta(days=10),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("10000.00"),
            total=Decimal("10000.00"),
        )
        inv2 = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            party_name=self.customer.name,
            invoice_number="INV-AGE-45",
            doc_date=today - timedelta(days=45),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("20000.00"),
            total=Decimal("20000.00"),
        )
        inv3 = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            party_name=self.customer.name,
            invoice_number="INV-AGE-130",
            doc_date=today - timedelta(days=130),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("30000.00"),
            total=Decimal("30000.00"),
        )

        ar = reports.ageing(self.client_obj.id, kind="receivable", as_of=today)
        self.assertIn("buckets", ar)
        buckets = ar["buckets"]
        self.assertIn("0-30", buckets)
        self.assertIn("31-60", buckets)
        self.assertIn("61-90", buckets)
        self.assertIn("91-120", buckets)
        self.assertIn("120+", buckets)

        self.assertGreaterEqual(buckets["0-30"], Decimal("10000.00"))
        self.assertGreaterEqual(buckets["31-60"], Decimal("20000.00"))
        self.assertGreaterEqual(buckets["120+"], Decimal("30000.00"))
