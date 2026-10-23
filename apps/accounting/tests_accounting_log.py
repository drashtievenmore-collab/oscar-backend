"""Ledger links for advances / deleted expenses, and the admin accounting log.

Run from ``oscar-backend``::

    python manage.py test apps.accounting.tests_accounting_log
"""
import uuid
from io import StringIO
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounting import services as ledger
from apps.accounting.models import JournalEntry, JournalLine
from apps.accounts.models import Client, User
from apps.hrms.models import Employee, SalaryAdvance


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class LedgerLinkTestBase(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name=unique("Ledger Tenant"))
        ledger.seed_chart_of_accounts(self.tenant)
        self.admin = User.objects.create(
            client=self.tenant, name="Admin", email=f"{unique('admin')}@test.invalid",
            status="Active", is_staff=True, is_superuser=True,
        )
        self.clerk = User.objects.create(
            client=self.tenant, name="Clerk", email=f"{unique('clerk')}@test.invalid", status="Active",
        )
        self.employee = Employee.objects.create(
            client=self.tenant, employee_code=unique("EMP"), name="Ravi Patel",
            joining_date=date(2026, 1, 5), status="Active", standard_salary=Decimal("30000"),
        )
        self.factory = APIRequestFactory()

    def call(self, viewset, actions, method, data=None, pk=None, user=None, path="/"):
        request = getattr(self.factory, method)(path, data, format="json") if data is not None \
            else getattr(self.factory, method)(path)
        request.client_id = self.tenant.id
        force_authenticate(request, user=user or self.admin)
        view = viewset.as_view(actions)
        return view(request, pk=pk) if pk else view(request)

    def balance(self, system_key):
        account = ledger.system_account(self.tenant.id, system_key)
        return sum(
            (line.debit - line.credit for line in JournalLine.objects.filter(account=account)),
            Decimal("0"),
        )


class SalaryAdvanceLedgerTests(LedgerLinkTestBase):
    def create_advance(self, amount="5000"):
        from apps.hrms.views import SalaryAdvanceViewSet

        response = self.call(SalaryAdvanceViewSet, {"post": "create"}, "post", {
            "employeeId": str(self.employee.id), "amount": amount, "issuedOn": "2026-09-01",
        })
        self.assertEqual(response.status_code, 201, response.data)
        return SalaryAdvance.objects.get(pk=response.data["id"])

    def test_issue_posts_disbursement(self):
        self.create_advance("5000")
        self.assertEqual(self.balance("employee_advances"), Decimal("5000"))
        self.assertEqual(self.balance("bank"), Decimal("-5000"))

    def test_amount_change_reposts(self):
        from apps.hrms.views import SalaryAdvanceViewSet

        advance = self.create_advance("5000")
        response = self.call(SalaryAdvanceViewSet, {"patch": "partial_update"}, "patch",
                             {"amount": "8000"}, pk=advance.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.balance("employee_advances"), Decimal("8000"))
        self.assertEqual(self.balance("bank"), Decimal("-8000"))

    def test_notes_change_posts_nothing(self):
        from apps.hrms.views import SalaryAdvanceViewSet

        advance = self.create_advance("5000")
        before = JournalEntry.objects.filter(client=self.tenant).count()
        self.call(SalaryAdvanceViewSet, {"patch": "partial_update"}, "patch",
                  {"notes": "Festival advance"}, pk=advance.id)
        self.assertEqual(JournalEntry.objects.filter(client=self.tenant).count(), before)

    def test_write_off_moves_outstanding_to_salary(self):
        # Status is read-only on the API today; the sync covers it for when a
        # write-off flow sets it.
        from apps.hrms.services import sync_salary_advance_ledger

        advance = self.create_advance("5000")
        SalaryAdvance.objects.filter(pk=advance.id).update(
            recovered_amount=Decimal("2000"), status="Written Off"
        )
        advance.refresh_from_db()
        sync_salary_advance_ledger(advance)
        # 5,000 issued; 3,000 never recovered goes to salary cost. The 2,000
        # recovered is credited by payroll itself, not here.
        self.assertEqual(self.balance("employee_advances"), Decimal("2000"))
        self.assertEqual(self.balance("salary_expense"), Decimal("3000"))

    def test_delete_reverses(self):
        from apps.hrms.views import SalaryAdvanceViewSet

        advance = self.create_advance("5000")
        response = self.call(SalaryAdvanceViewSet, {"delete": "destroy"}, "delete", pk=advance.id)
        self.assertIn(response.status_code, (200, 204), getattr(response, "data", None))
        self.assertEqual(self.balance("employee_advances"), Decimal("0"))
        self.assertEqual(self.balance("bank"), Decimal("0"))


class ExpenseDeleteTests(LedgerLinkTestBase):
    def test_deleting_expense_reverses_its_entry(self):
        from apps.purchase.models import Expense
        from apps.purchase.views import ExpenseViewSet

        expense = Expense.objects.create(
            client=self.tenant, expense_number=unique("EXP"), expense_date=date(2026, 9, 2),
            amount=Decimal("1200"), tax_amount=Decimal("0"), total=Decimal("1200"),
            payment_mode="Cash",
        )
        expense.journal_entry = ledger.post_expense(expense)
        expense.save(update_fields=["journal_entry"])
        self.assertEqual(self.balance("general_expense"), Decimal("1200"))

        response = self.call(ExpenseViewSet, {"delete": "destroy"}, "delete", pk=expense.id)
        self.assertIn(response.status_code, (200, 204))
        self.assertEqual(self.balance("general_expense"), Decimal("0"))
        self.assertEqual(self.balance("cash"), Decimal("0"))


class AccountingLogTests(LedgerLinkTestBase):
    def log(self, user=None, **query):
        from apps.accounting.views import AccountingLogView

        request = self.factory.get("/", query)
        request.client_id = self.tenant.id
        force_authenticate(request, user=user or self.admin)
        return AccountingLogView.as_view()(request)

    def test_only_admins_can_read(self):
        self.assertEqual(self.log(user=self.clerk).status_code, 403)
        self.assertEqual(self.log().status_code, 200)

    def test_rows_carry_module_document_and_lines(self):
        from apps.hrms.views import SalaryAdvanceViewSet

        self.call(SalaryAdvanceViewSet, {"post": "create"}, "post", {
            "employeeId": str(self.employee.id), "amount": "5000", "issuedOn": "2026-09-01",
        })
        response = self.log(module="payroll")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["module"], "payroll")
        self.assertEqual(row["documentLabel"], "Salary advance - Ravi Patel")
        self.assertEqual(row["amount"], Decimal("5000.00"))
        self.assertEqual({line["accountCode"] for line in row["lines"]}, {"1150", "1300"})

        modules = {m["module"]: m for m in response.data["aggregates"]["modules"]}
        self.assertEqual(modules["payroll"]["netAmount"], Decimal("5000.00"))
        self.assertEqual(modules["expense"]["entries"], 0)

    def test_reversed_pairs_net_to_zero_but_stay_listed(self):
        from apps.hrms.views import SalaryAdvanceViewSet

        response = self.call(SalaryAdvanceViewSet, {"post": "create"}, "post", {
            "employeeId": str(self.employee.id), "amount": "5000", "issuedOn": "2026-09-01",
        })
        self.call(SalaryAdvanceViewSet, {"delete": "destroy"}, "delete", pk=response.data["id"])
        data = self.log(module="payroll").data
        self.assertEqual(data["count"], 2)  # original + reversal
        self.assertTrue(any(row["isReversal"] for row in data["results"]))
        modules = {m["module"]: m for m in data["aggregates"]["modules"]}
        self.assertEqual(modules["payroll"]["netAmount"], Decimal("0.00"))

    def test_other_ledger_activity_is_not_listed(self):
        ledger.post_entry(
            client=self.tenant,
            postings=[
                ledger.debit(ledger.system_account(self.tenant.id, "cash"), Decimal("10")),
                ledger.credit(ledger.system_account(self.tenant.id, "bank"), Decimal("10")),
            ],
            source_document_type="BankTransfer",
        )
        self.assertEqual(self.log().data["count"], 0)


class BackfillCommandTests(LedgerLinkTestBase):
    def test_dry_run_writes_nothing_and_apply_links(self):
        SalaryAdvance.objects.create(
            client=self.tenant, employee=self.employee, amount=Decimal("4000"),
            issued_on=date(2026, 6, 1),
        )
        call_command("link_ledger_history", client=str(self.tenant.id), stdout=StringIO())
        self.assertEqual(self.balance("employee_advances"), Decimal("0"))

        call_command("link_ledger_history", "--apply", client=str(self.tenant.id), stdout=StringIO())
        self.assertEqual(self.balance("employee_advances"), Decimal("4000"))
        # Idempotent.
        call_command("link_ledger_history", "--apply", client=str(self.tenant.id), stdout=StringIO())
        self.assertEqual(self.balance("employee_advances"), Decimal("4000"))


class PayrollAccrualTests(LedgerLinkTestBase):
    """Approval accrues in the payroll month; payment only clears the payable."""

    def make_payslip(self, net="25000", pf="1800", status="Ready for Review"):
        from apps.hrms.models import PayrollRun, Payslip, PayslipComponent

        run, _ = PayrollRun.objects.get_or_create(client=self.tenant, period_month=date(2026, 9, 1))
        payslip = Payslip.objects.create(
            client=self.tenant, payroll_run=run, employee=self.employee,
            period_month=date(2026, 9, 1), standard_salary=Decimal("30000"),
            earned_salary=Decimal(net) + Decimal(pf), net_payable=Decimal(net), status=status,
        )
        if Decimal(pf):
            PayslipComponent.objects.create(
                client=self.tenant, payslip=payslip, name="PF", kind="deduction", amount=Decimal(pf)
            )
        return payslip

    def test_approval_accrues_in_payroll_month(self):
        from apps.hrms import services as hrms_services

        payslip = hrms_services.approve_payslip(self.make_payslip(), user=self.admin)
        self.assertEqual(payslip.status, "Approved")
        self.assertEqual(payslip.journal_entry.entry_date, date(2026, 9, 30))
        self.assertEqual(self.balance("salary_expense"), Decimal("26800"))
        self.assertEqual(self.balance("salary_payable"), Decimal("-25000"))
        self.assertEqual(self.balance("pf_payable"), Decimal("-1800"))
        self.assertEqual(self.balance("cash"), Decimal("0"))

        # Approving twice never accrues twice.
        hrms_services.approve_payslip(payslip, user=self.admin)
        self.assertEqual(self.balance("salary_expense"), Decimal("26800"))

    def test_payment_clears_payable_without_second_expense(self):
        from apps.hrms import services as hrms_services

        payslip = hrms_services.approve_payslip(self.make_payslip(), user=self.admin)
        accrual_id = payslip.journal_entry_id
        hrms_services.mark_payslip_paid(payslip, payment_date=date(2026, 10, 7), user=self.admin)

        self.assertEqual(self.balance("salary_expense"), Decimal("26800"))
        self.assertEqual(self.balance("salary_payable"), Decimal("0"))
        self.assertEqual(self.balance("pf_payable"), Decimal("-1800"))  # owed to PF until deposited
        self.assertEqual(self.balance("cash"), Decimal("-25000"))
        payslip.refresh_from_db()
        self.assertEqual(payslip.journal_entry_id, accrual_id)
        payment = JournalEntry.objects.filter(
            source_document_type="Payslip", source_document_id=payslip.id
        ).exclude(pk=accrual_id).get()
        self.assertEqual(payment.entry_date, date(2026, 10, 7))

    def test_payslip_approved_before_accrual_still_pays_in_one_entry(self):
        from apps.hrms import services as hrms_services

        legacy = self.make_payslip(status="Approved")  # no accrual entry
        hrms_services.mark_payslip_paid(legacy, payment_date=date(2026, 10, 7), user=self.admin)
        self.assertEqual(self.balance("salary_expense"), Decimal("26800"))
        self.assertEqual(self.balance("salary_payable"), Decimal("0"))
        self.assertEqual(self.balance("cash"), Decimal("-25000"))

    def test_rerunning_payroll_keeps_approved_payslips(self):
        from apps.hrms import services as hrms_services

        payslip = hrms_services.approve_payslip(self.make_payslip(), user=self.admin)
        result = hrms_services.process_payroll(
            client=self.tenant, period_month=date(2026, 9, 1), user=self.admin
        )
        payslip.refresh_from_db()
        self.assertEqual(payslip.status, "Approved")
        self.assertEqual(payslip.net_payable, Decimal("25000"))
        self.assertIn(payslip.id, [p.id for p in result["payslips"]])
        self.assertEqual(self.balance("salary_payable"), Decimal("-25000"))

    def test_backfill_accrues_old_approved_payslips(self):
        legacy = self.make_payslip(status="Approved")
        call_command("link_ledger_history", "--apply", client=str(self.tenant.id), stdout=StringIO())
        legacy.refresh_from_db()
        self.assertIsNotNone(legacy.journal_entry_id)
        self.assertEqual(self.balance("salary_payable"), Decimal("-25000"))
