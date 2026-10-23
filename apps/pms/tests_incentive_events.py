"""Incentive follows the sale: deal owner -> salesperson, payments -> hold release.

Run from ``oscar-backend``::

    python manage.py test apps.pms.tests_incentive_events
"""
from datetime import date
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounting import services as ledger
from apps.accounting.models import JournalLine
from apps.pms import production_services as services
from apps.pms.models import IncentiveCalculation, IncentiveScheme
from apps.pms.tests_production import ProductionTestBase, unique
from apps.sales import services as sales_services


class IncentiveEventTestBase(ProductionTestBase):
    def setUp(self):
        super().setUp()
        from apps.masters.models import Party

        self.party, _ = Party.objects.get_or_create(
            client=self.client_obj, code="CUST-INC", defaults={"name": "Incentive Customer"}
        )

    def make_invoice(self, employee, total="1000"):
        from apps.sales.models import SalesInvoice

        return SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.party,
            party_name=self.party.name,
            doc_date=date(2026, 8, 10),
            invoice_number=unique("INV"),
            status="Unpaid",
            subtotal=Decimal(total),
            total=Decimal(total),
            posted_at=timezone.now(),
            salesperson_fk=employee,
            salesperson=employee.name,
            created_by=self.user,
        )

    def receive(self, invoice, amount):
        from apps.sales.models import PaymentIn

        payment = PaymentIn.objects.create(
            client=self.client_obj,
            party=self.party,
            payment_number=unique("PAY"),
            payment_date=date(2026, 9, 1),
            amount=Decimal(amount),
            status="Active",
        )
        sales_services.allocate_payment_in(
            payment, [{"invoiceId": str(invoice.id), "amount": amount}], user=self.user
        )
        return payment

    def age_payments(self, days):
        """Move every receipt ``days`` into the past (payment and allocation)."""
        from datetime import timedelta

        from apps.sales.models import PaymentAllocation, PaymentIn

        before = timezone.localdate() - timedelta(days=days)
        PaymentIn.objects.filter(client=self.client_obj).update(payment_date=before)
        PaymentAllocation.objects.filter(client=self.client_obj).update(
            allocated_at=timezone.now() - timedelta(days=days)
        )

    def scheme(self, **kwargs):
        params = {
            "client": self.client_obj,
            "name": unique("Scheme"),
            "rate_pct": Decimal("10"),
            "period_type": "Quarterly",
            "is_active": True,
            "created_by": self.user,
        }
        params.update(kwargs)
        return IncentiveScheme.objects.create(**params)

    def calculate(self, scheme, employee, **kwargs):
        return services.calculate_incentive(
            client=self.client_obj,
            scheme=scheme,
            employee=employee,
            period_label=unique("Q3"),
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            user=self.user,
            **kwargs,
        )

    def payable_balance(self):
        payable = ledger.system_account(self.client_obj.id, "incentive_payable")
        return sum(
            (line.credit - line.debit for line in JournalLine.objects.filter(
                account=payable, journal_entry__client=self.client_obj
            )),
            Decimal("0"),
        )


class PaymentDrivenIncentiveTests(IncentiveEventTestBase):
    def test_payment_releases_held_incentive(self):
        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        scheme = self.scheme(hold_unpaid_invoices=True)

        calc = self.calculate(scheme, employee, auto_post=False)
        self.assertEqual(calc.incentive_amount, Decimal("100.00"))
        self.assertEqual(calc.hold_amount, Decimal("100.00"))

        # Half paid before posting: only the figures move.
        self.receive(invoice, "500")
        calc.refresh_from_db()
        self.assertEqual(calc.hold_amount, Decimal("50.00"))
        self.assertIsNone(calc.journal_entry_id)

        services.post_incentive(calc, user=self.user)
        self.assertEqual(self.payable_balance(), Decimal("50.00"))

        # Rest paid after posting: the released half is booked as a true-up.
        second = self.receive(invoice, "500")
        calc.refresh_from_db()
        self.assertEqual(calc.hold_amount, Decimal("0.00"))
        self.assertEqual(calc.status, "Posted")
        self.assertEqual(self.payable_balance(), Decimal("100.00"))

        # The receipt bounces: that share goes back on hold.
        sales_services.cancel_payment_in(second, reason="Cheque bounced", user=self.user)
        calc.refresh_from_db()
        self.assertEqual(calc.hold_amount, Decimal("50.00"))
        self.assertEqual(self.payable_balance(), Decimal("50.00"))

    def test_collection_basis_grows_with_payments(self):
        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        scheme = self.scheme(basis="COLLECTION")

        calc = self.calculate(scheme, employee)
        self.assertEqual(calc.incentive_amount, Decimal("0"))

        self.receive(invoice, "400")
        calc.refresh_from_db()
        self.assertEqual(calc.incentive_amount, Decimal("40.00"))

    def test_reversal_sweeps_true_up_entries(self):
        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        scheme = self.scheme(hold_unpaid_invoices=True)
        calc = self.calculate(scheme, employee, auto_post=False)
        services.post_incentive(calc, user=self.user)  # nothing payable yet
        self.receive(invoice, "1000")
        self.assertEqual(self.payable_balance(), Decimal("100.00"))

        services.reverse_incentive(calc, user=self.user, reason="Credit note")
        self.assertEqual(self.payable_balance(), Decimal("0"))

    def test_hold_blocks_approval_until_released(self):
        from apps.core.exceptions import BusinessRuleViolation

        employee = self.make_employee()
        self.make_invoice(employee)
        calc = self.calculate(self.scheme(), employee, auto_post=False)
        services.hold_incentive(calc, reason="Customer dispute", user=self.user)
        with self.assertRaises(BusinessRuleViolation):
            services.approve_incentive(calc, user=self.user)
        services.release_incentive_hold(calc, user=self.user)
        calc = services.approve_incentive(calc, user=self.user)
        self.assertEqual(calc.status, "Approved")


def SalesInvoiceModel():
    from apps.sales.models import SalesInvoice

    return SalesInvoice


class PayoutAfterDaysTests(IncentiveEventTestBase):
    """Incentive is paid N days (default 90) after the customer's payment."""

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace

        self.bank = SimpleNamespace(account=None)

    def test_default_is_90_days(self):
        self.assertEqual(self.scheme().payout_after_days, 90)

    def test_not_payable_until_payment_is_90_days_old(self):
        from datetime import timedelta

        from apps.core.exceptions import BusinessRuleViolation

        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        calc = self.calculate(self.scheme(), employee)  # posted: 100 payable

        with self.assertRaises(BusinessRuleViolation):  # customer hasn't paid
            services.pay_incentive(calc, bank_account=self.bank, user=self.user)

        self.receive(invoice, "1000")
        schedule = services.incentive_payout_schedule(calc)
        self.assertEqual(schedule["eligible"], Decimal("0"))
        self.assertEqual(schedule["nextDate"], timezone.localdate() + timedelta(days=90))
        with self.assertRaises(BusinessRuleViolation):  # paid, but not 90 days ago
            services.pay_incentive(calc, bank_account=self.bank, user=self.user)

        self.age_payments(90)
        calc = services.pay_incentive(calc, bank_account=self.bank, user=self.user)
        self.assertEqual(calc.status, "Paid")
        self.assertEqual(calc.paid_amount, Decimal("100.00"))

    def test_each_payment_releases_its_own_share(self):
        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        calc = self.calculate(self.scheme(), employee)
        self.receive(invoice, "400")
        self.age_payments(95)
        self.receive(invoice, "600")  # today: matures in 90 days

        calc = services.pay_incentive(calc, bank_account=self.bank, user=self.user)
        self.assertEqual(calc.paid_amount, Decimal("40.00"))  # 40% matured
        self.assertEqual(calc.status, "Posted")

    def test_days_are_changeable_per_scheme(self):
        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        calc = self.calculate(self.scheme(payout_after_days=30), employee)
        self.receive(invoice, "1000")
        self.age_payments(31)
        calc = services.pay_incentive(calc, bank_account=self.bank, user=self.user)
        self.assertEqual(calc.status, "Paid")

    def test_hrms_row_shows_when_it_becomes_payable(self):
        from apps.hrms.serializers import HrmsIncentiveSerializer

        employee = self.make_employee()
        invoice = self.make_invoice(employee, total="1000")
        calc = self.calculate(self.scheme(), employee)
        self.receive(invoice, "1000")
        data = HrmsIncentiveSerializer(IncentiveCalculation.objects.get(pk=calc.pk)).data
        self.assertEqual(data["payableNow"], 0.0)
        self.assertEqual(data["payoutAfterDays"], 90)
        self.assertIsNotNone(data["nextPayoutDate"])
        self.assertTrue(data["holdReason"].startswith("Payable from"))


class SalespersonFromDealTests(IncentiveEventTestBase):
    def test_quotation_takes_deal_owner(self):
        from apps.crm.models import Deal
        from apps.sales.models import Quotation

        employee = self.make_employee()
        self.user.employee = employee
        self.user.save(update_fields=["employee"])
        deal = Deal.objects.create(
            client=self.client_obj, deal_number=unique("DL"), title="Grey fabric", owner=self.user
        )
        quotation = Quotation.objects.create(
            client=self.client_obj,
            quotation_number=unique("QT"),
            doc_date=date(2026, 8, 1),
            crm_deal=deal,
            created_by=self.user,
        )
        self.assertTrue(sales_services.fill_salesperson(quotation))
        quotation.refresh_from_db()
        self.assertEqual(quotation.salesperson_fk_id, employee.id)

    def test_existing_salesperson_is_kept_and_party_default_used(self):
        picked = self.make_employee()
        default = self.make_employee()
        self.party.default_salesperson = default
        self.party.save(update_fields=["default_salesperson"])

        invoice = self.make_invoice(picked)
        self.assertFalse(sales_services.fill_salesperson(invoice))

        invoice.salesperson_fk = None
        invoice.salesperson = ""
        invoice.save(update_fields=["salesperson_fk", "salesperson"])
        self.assertTrue(sales_services.fill_salesperson(invoice))
        self.assertEqual(invoice.salesperson_fk_id, default.id)


class HrmsIncentiveRouteTests(IncentiveEventTestBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        self.factory = APIRequestFactory()

    def call(self, action, method, path, data=None, pk=None):
        from apps.hrms.views import SalespersonIncentiveViewSet

        request = getattr(self.factory, method)(path, data or {}, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        view = SalespersonIncentiveViewSet.as_view({method: action})
        return view(request, pk=pk) if pk else view(request)

    def test_routes_exist(self):
        self.assertEqual(
            reverse("hrms-incentives-calculate-quarterly"),
            "/api/v1/hrms/incentives/calculate-quarterly/",
        )
        pk = "00000000-0000-0000-0000-000000000000"
        # The exact paths the HRMS Incentives page posts to (hrmsSync.js).
        for name, path in (("hold-payout", "hold"), ("release-payout", "release"),
                           ("mark-paid", "mark-paid"), ("approve", "approve")):
            self.assertEqual(
                reverse(f"hrms-incentives-{name}", kwargs={"pk": pk}),
                f"/api/v1/hrms/incentives/{pk}/{path}/",
            )

    def test_quarter_flow_accrue_approve_pay(self):
        tagged = self.make_employee()
        untagged = self.make_employee()
        self.make_invoice(tagged, total="2000")
        self.scheme()

        # Invoices are dated Aug 2026: Q2 of FY 2026-27.
        response = self.call("calculate_quarterly", "post", "/", {"quarter": 2, "year": 2026})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["period_name"], "Q2 FY2026-27")
        row = response.data["accruals"][0]
        self.assertEqual(str(row["employeeId"]), str(tagged.id))
        self.assertEqual(row["status"], "ACCRUED")
        self.assertEqual(row["calculatedIncentive"], 200.0)
        self.assertFalse(
            IncentiveCalculation.objects.filter(employee=untagged).exists()
        )

        pk = row["id"]
        response = self.call("approve", "post", "/", pk=pk)
        self.assertEqual(response.data["status"], "APPROVED")

        # Not payable until the customer has paid and 90 days have passed.
        invoice = SalesInvoiceModel().objects.get(client=self.client_obj)
        self.receive(invoice, "2000")
        response = self.call("mark_paid", "post", "/", pk=pk)
        self.assertEqual(response.status_code, 422, response.data)
        self.age_payments(91)

        response = self.call("mark_paid", "post", "/", pk=pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "PAID")
        self.assertEqual(self.payable_balance(), Decimal("0"))

        # Recalculating leaves the paid quarter alone.
        response = self.call("calculate_quarterly", "post", "/", {"quarter": 2, "year": 2026})
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(len(response.data["skipped"]), 1)

    def test_quarters_follow_the_financial_year(self):
        employee = self.make_employee()
        self.make_invoice(employee)  # Aug 2026
        self.scheme()
        # Calendar Q3 (Jul-Sep) would catch August; FY Q3 is Oct-Dec.
        response = self.call("calculate_quarterly", "post", "/", {"quarter": 3, "year": 2026})
        self.assertEqual(response.data["count"], 0)
        # Q4 of FY 2025-26 is Jan-Mar 2026.
        response = self.call("calculate_quarterly", "post", "/", {"quarter": 4, "year": 2025})
        self.assertEqual(response.data["period_name"], "Q4 FY2025-26")
        self.assertEqual(response.data["count"], 0)

    def test_scheme_saved_from_hrms(self):
        from apps.hrms.views import SalespersonIncentiveSchemeViewSet

        request = self.factory.post("/", {
            "name": "Tiered Q", "periodType": "Quarterly", "basis": "INVOICE",
            "slabType": "TIERED", "ratePct": 0, "isActive": True,
            "slabs": [{"min": 0, "max": 1000, "rate_pct": 1}, {"min": 1000, "max": None, "rate_pct": 2}],
            "holdUnpaidInvoices": True, "excludeGst": False,
        }, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        response = SalespersonIncentiveSchemeViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 201, response.data)
        scheme = IncentiveScheme.objects.get(pk=response.data["id"])
        self.assertTrue(scheme.hold_unpaid_invoices)
        # 1% of the first 1,000 + 2% of the next 1,000.
        self.assertEqual(services.compute_scheme_incentive(scheme, 2000), Decimal("30.00"))

    def test_hold_and_release(self):
        employee = self.make_employee()
        self.make_invoice(employee)
        self.scheme()
        row = self.call("calculate_quarterly", "post", "/", {"quarter": 2, "year": 2026}).data["accruals"][0]

        held = self.call("hold_payout", "post", "/", {"reason": "Bad debt"}, pk=row["id"]).data
        self.assertEqual(held["status"], "HELD")
        self.assertEqual(held["holdReason"], "Bad debt")
        released = self.call("release_payout", "post", "/", pk=row["id"]).data
        self.assertEqual(released["status"], "ACCRUED")
