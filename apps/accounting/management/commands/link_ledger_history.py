"""
Bring records created before the ledger links existed into the books.

Three gaps are closed, all idempotent:

1. Salary advances issued without a disbursement entry. Payroll recovery
   already credits Employee Advances, so the matching debit is posted
   (Dr Employee Advances / Cr Bank, plus a write-off for a Written Off one).
2. Deleted expenses whose journal entry is still live -- reversed.
3. Payslips approved but not yet paid, from before approval accrued -- the
   accrual is posted in their payroll month, so marking them paid later only
   clears Salaries Payable. Paid payslips are left as they are.

Dry run by default; nothing is written without ``--apply``::

    python manage.py link_ledger_history            # report only
    python manage.py link_ledger_history --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounting import services as ledger
from apps.accounting.models import JournalEntry


def _live_entries(client_id, source_type, source_id):
    return JournalEntry.objects.filter(
        client_id=client_id,
        source_document_type=source_type,
        source_document_id=source_id,
        deleted_at__isnull=True,
        reversal_of__isnull=True,
    ).exclude(status="Reversed")


class Command(BaseCommand):
    help = "Post missing salary-advance entries and reverse deleted expenses' entries."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the entries.")
        parser.add_argument("--client", help="Limit to one tenant id.")

    def handle(self, *args, **options):
        from apps.hrms.models import Payslip, SalaryAdvance
        from apps.hrms.services import sync_salary_advance_ledger
        from apps.purchase.models import Expense

        apply = options["apply"]
        advances = SalaryAdvance.objects.filter(deleted_at__isnull=True).select_related("employee")
        expenses = Expense.objects.filter(deleted_at__isnull=False)
        unaccrued = Payslip.objects.filter(
            status="Approved", journal_entry__isnull=True, deleted_at__isnull=True
        ).select_related("employee")
        if options.get("client"):
            advances = advances.filter(client_id=options["client"])
            expenses = expenses.filter(client_id=options["client"])
            unaccrued = unaccrued.filter(client_id=options["client"])
        unaccrued = list(unaccrued)

        missing_advances = [
            a for a in advances
            if not JournalEntry.objects.filter(
                client_id=a.client_id, source_document_type="SalaryAdvance", source_document_id=a.id
            ).exists()
        ]
        stale_expenses = [
            e for e in expenses if _live_entries(e.client_id, "Expense", e.id).exists()
        ]

        for advance in missing_advances:
            self.stdout.write(
                f"advance  {advance.employee.name}: {advance.amount} issued {advance.issued_on} ({advance.status})"
            )
        for expense in stale_expenses:
            self.stdout.write(f"expense  {expense.expense_number}: deleted, entry still live")
        for payslip in unaccrued:
            self.stdout.write(
                f"payslip  {payslip.employee.name} {payslip.period_month:%b %Y}: approved, not accrued"
            )

        if not apply:
            self.stdout.write(self.style.WARNING(
                f"Dry run: {len(missing_advances)} advance(s) to post, "
                f"{len(stale_expenses)} expense(s) to reverse, "
                f"{len(unaccrued)} payslip(s) to accrue. Re-run with --apply."
            ))
            return

        with transaction.atomic():
            for advance in missing_advances:
                sync_salary_advance_ledger(advance)
            for expense in stale_expenses:
                ledger.reverse_document_entries(
                    client_id=expense.client_id,
                    source_document_type="Expense",
                    source_document_id=expense.id,
                )
            for payslip in unaccrued:
                entry = ledger.post_payroll_accrual(payslip)
                if entry is not None:
                    payslip.journal_entry = entry
                    payslip.save(update_fields=["journal_entry", "updated_at"])
        self.stdout.write(self.style.SUCCESS(
            f"Posted {len(missing_advances)} advance(s), reversed {len(stale_expenses)} expense(s), "
            f"accrued {len(unaccrued)} payslip(s)."
        ))
