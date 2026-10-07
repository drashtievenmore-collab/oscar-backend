"""Tests for the grey-fabric HRMS wiring (HRMS_ERP_CHANGES.md validation checks).

Run from ``oscar-backend``::

    python manage.py test apps.hrms apps.pms.tests_production
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Client, User
from apps.accounting import services as ledger
from apps.core.models import AuditLog
from apps.hrms import services as hrms_services
from apps.hrms.models import Employee

from apps.pms import production_reports as reports, production_services as services


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class ProductionTestBase(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="prod-test-tenant", defaults={"name": "Production Test Tenant"}
        )
        self.user = User.objects.filter(
            email="producer@example.com", client=self.client_obj
        ).first()
        if self.user is None:
            self.user = User.objects.create_user(
                email="producer@example.com",
                password="test-pass-123",
                client=self.client_obj,
                name="Producer",
                status="Active",
            )
        ledger.seed_chart_of_accounts(self.client_obj)

    def make_employee(self, status="Active"):
        return Employee.objects.create(
            client=self.client_obj,
            employee_code=unique("EMP"),
            name=f"{status} Worker {uuid.uuid4().hex[:4]}",
            joining_date=date(2026, 1, 5),
            status=status,
            standard_salary=Decimal("30000"),
        )

    def make_instruction(self, employee=None, **overrides):
        employee = employee or self.make_employee()
        params = {
            "client": self.client_obj,
            "employee": employee,
            "agency_name": "Shree Grey Fabrics",
            "order_reference": unique("ORD"),
            "order_meter": Decimal("100"),
            "user": self.user,
        }
        params.update(overrides)
        return services.create_instruction(**params)

    def make_scheme(self, rate="10"):
        from apps.pms.models import IncentiveScheme

        return IncentiveScheme.objects.create(
            client=self.client_obj,
            name=unique("Scheme"),
            rate_pct=Decimal(rate),
            is_active=True,
            created_by=self.user,
        )


class InstructionAssignmentTests(ProductionTestBase):
    def test_create_with_active_employee_passes(self):
        employee = self.make_employee(status="Active")
        instruction = self.make_instruction(employee=employee)
        self.assertIsNotNone(instruction.pk)
        self.assertTrue(instruction.instruction_number.startswith("PROD"))
        self.assertEqual(instruction.employee_id, employee.id)

    def test_assign_terminated_employee_fails(self):
        from apps.core.exceptions import ValidationFailed

        employee = self.make_employee(status="Terminated")
        with self.assertRaises(ValidationFailed):
            self.make_instruction(employee=employee)

    def test_assign_resigned_employee_fails(self):
        from apps.core.exceptions import ValidationFailed

        employee = self.make_employee(status="Resigned")
        with self.assertRaises(ValidationFailed):
            services.create_instruction(
                client=self.client_obj,
                employee=employee,
                agency_name="Agency",
                order_reference="ORD-1",
                order_meter=Decimal("10"),
                user=self.user,
            )

    def test_supervisor_must_also_be_active(self):
        from apps.core.exceptions import ValidationFailed

        supervisor = self.make_employee(status="Terminated")
        with self.assertRaises(ValidationFailed):
            self.make_instruction(supervisor=supervisor)

    def test_active_selector_covers_probation_and_leave_only(self):
        for status in ["Active", "On Leave", "Probation", "Resigned", "Terminated"]:
            self.make_employee(status=status)
        names = set(
            services.active_employee_queryset(self.client_obj.id).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(names, {"Active", "On Leave", "Probation"})


class DailyEntryTests(ProductionTestBase):
    def test_entry_shows_employee_code_and_timestamp(self):
        employee = self.make_employee()
        instruction = self.make_instruction(employee=employee)
        entry = services.record_daily_entry(
            client=self.client_obj,
            instruction=instruction,
            entry_date=date(2026, 9, 25),
            meters=Decimal("25"),
            entered_by_employee=employee,
            entered_by_user=self.user,
        )
        self.assertEqual(entry.entered_by_employee.employee_code, employee.employee_code)
        self.assertIsNotNone(entry.created_at)
        self.assertEqual(entry.entered_by_user_id, self.user.id)

    def test_balance_is_computed_never_stored(self):
        employee = self.make_employee()
        instruction = self.make_instruction(employee=employee)
        services.record_daily_entry(
            client=self.client_obj,
            instruction=instruction,
            entry_date=date(2026, 9, 25),
            meters=Decimal("30"),
            entered_by_employee=employee,
            entered_by_user=self.user,
        )
        services.record_daily_entry(
            client=self.client_obj,
            instruction=instruction,
            entry_date=date(2026, 9, 26),
            meters=Decimal("20"),
            entered_by_employee=employee,
            entered_by_user=self.user,
        )
        progress = services.instruction_progress(instruction)
        self.assertEqual(progress["cumulative"], Decimal("50"))
        self.assertEqual(progress["balance"], Decimal("50"))
        field_names = {
            field.name for field in instruction._meta.concrete_fields
        }
        self.assertNotIn("balance", field_names)


class PayrollSeparationTests(ProductionTestBase):
    def _make_invoice(self, total="1080", number=None):
        from apps.masters.models import Party
        from apps.sales.models import SalesInvoice

        party, _ = Party.objects.get_or_create(
            client=self.client_obj,
            code="CUST-TEST",
            defaults={"name": "Test Customer"},
        )
        return SalesInvoice.objects.create(
            client=self.client_obj,
            party=party,
            party_name=party.name,
            doc_date=date(2026, 9, 10),
            invoice_number=number or unique("INV"),
            status="Unpaid",
            subtotal=Decimal("1000"),
            total_discount=Decimal("100"),
            total_tax=Decimal("180"),
            total=Decimal(total),
            created_by=self.user,
        )

    def test_payroll_contains_salary_only(self):
        employee = self.make_employee()
        result = hrms_services.process_payroll(
            client=self.client_obj, period_month=date(2026, 9, 1), user=self.user
        )
        payslip = next(p for p in result["payslips"] if p.employee_id == employee.id)
        self.assertEqual(payslip.additional_earnings, Decimal("0"))
        self.assertEqual(payslip.net_payable, payslip.earned_salary)

    def test_incentive_lands_in_calc_not_payslip(self):
        from apps.pms.models import IncentiveCalculation

        employee = self.make_employee()
        self._make_invoice()
        scheme = self.make_scheme(rate="10")
        calc = services.calculate_incentive(
            client=self.client_obj,
            scheme=scheme,
            employee=employee,
            period_label="2026-Q3",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            user=self.user,
        )
        # 10% of (1080 invoiced - 0 returns) = 108, posted as its own entry.
        self.assertEqual(calc.incentive_amount, Decimal("108.00"))
        self.assertEqual(calc.status, "Posted")
        self.assertIsNotNone(calc.journal_entry_id)
        self.assertEqual(
            calc.journal_entry.source_document_type, "IncentiveCalc"
        )
        result = hrms_services.process_payroll(
            client=self.client_obj, period_month=date(2026, 9, 1), user=self.user
        )
        payslip = next(p for p in result["payslips"] if p.employee_id == employee.id)
        self.assertEqual(payslip.additional_earnings, Decimal("0"))
        self.assertEqual(
            IncentiveCalculation.objects.filter(
                client=self.client_obj, employee=employee
            ).count(),
            1,
        )

    def test_incentive_reverses_on_return(self):
        from apps.accounting.models import JournalEntry
        from apps.sales.models import SalesReturn

        employee = self.make_employee()
        invoice = self._make_invoice()
        scheme = self.make_scheme(rate="10")
        calc = services.calculate_incentive(
            client=self.client_obj,
            scheme=scheme,
            employee=employee,
            period_label="2026-Q3R",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            user=self.user,
        )
        original_entry_id = calc.journal_entry_id
        SalesReturn.objects.create(
            client=self.client_obj,
            party=invoice.party,
            party_name=invoice.party_name,
            sales_invoice=invoice,
            return_number=unique("SR"),
            status="Posted",
            doc_date=date(2026, 9, 20),
            subtotal=Decimal("200"),
            total_tax=Decimal("36"),
            total=Decimal("236"),
            created_by=self.user,
        )
        reversed_calcs = services.reverse_incentives_for_invoice(
            invoice, user=self.user
        )
        self.assertEqual(len(reversed_calcs), 1)
        calc.refresh_from_db()
        self.assertEqual(calc.status, "Reversed")
        original = JournalEntry.objects.get(pk=original_entry_id)
        self.assertEqual(original.status, "Reversed")
        self.assertTrue(
            JournalEntry.objects.filter(
                reversal_of_id=original_entry_id, source_document_type="IncentiveCalc"
            ).exists()
        )


class ProductionReportTests(ProductionTestBase):
    def test_employee_dashboard_meters_match_today_entries(self):
        employee = self.make_employee()
        instruction = self.make_instruction(employee=employee)
        for meters in [Decimal("10"), Decimal("15")]:
            services.record_daily_entry(
                client=self.client_obj,
                instruction=instruction,
                entry_date=date(2026, 9, 25),
                meters=meters,
                entered_by_employee=employee,
                entered_by_user=self.user,
            )
        rows = reports.employee_production_view(
            self.client_obj.id, day=date(2026, 9, 25)
        )
        row = next(r for r in rows if r["employeeId"] == str(employee.id))
        self.assertEqual(row["dailyEntries"], 2)
        self.assertEqual(row["metersMonitored"], Decimal("25"))
        agencies = reports.agency_production_view(
            self.client_obj.id, day=date(2026, 9, 25)
        )
        agency = next(a for a in agencies if a["agency"] == "Shree Grey Fabrics")
        self.assertEqual(agency["metersMonitored"], Decimal("25"))
        self.assertEqual(agency["dailyEntries"], 2)


class ApprovalAuditTests(ProductionTestBase):
    def test_verify_writes_audit_with_actor_time_values_reason(self):
        instruction = self.make_instruction()
        services.verify_instruction(
            instruction, user=self.user, reason="Meters cross-checked."
        )
        entry = AuditLog.objects.filter(
            client=self.client_obj,
            entity_type="ProductionInstruction",
            entity_id=instruction.id,
            action="production_instruction_verify",
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.user.id)
        self.assertIsNotNone(entry.created_at)
        self.assertEqual(entry.from_value, "In Progress")
        self.assertEqual(entry.to_value, "Verified")
        self.assertEqual(entry.comments, "Meters cross-checked.")


class ProductionPermissionTests(TestCase):
    def test_production_permissions_in_catalogue(self):
        from apps.accounts.permission_catalogue import all_permission_ids

        ids = set(all_permission_ids())
        self.assertTrue({"view_production", "enter_production", "verify_production"} <= ids)

    def test_role_split_payroll_vs_production(self):
        from apps.accounts.permission_catalogue import DEFAULT_ROLES

        roles = {code: set(perms) for code, _name, _desc, perms in DEFAULT_ROLES}
        coordinator = roles["PC"]
        self.assertTrue({"enter_production", "verify_production"} <= coordinator)
        self.assertTrue(
            {"generate_payroll", "approve_payroll"}.isdisjoint(coordinator)
        )
        hr_manager = roles["HR"]
        self.assertTrue({"generate_payroll", "approve_payroll"} <= hr_manager)
        self.assertTrue(
            {"enter_production", "verify_production"}.isdisjoint(hr_manager)
        )
