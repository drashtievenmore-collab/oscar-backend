"""Accounts endpoints (api.md §8)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from django.http import HttpResponse

from apps.core.exceptions import Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import note_services, reports, services, tally_export
from .models import (
    Account, BankAccount, BankTransfer, Budget, ExpenseCategory, JournalEntry,
    JournalLine, StandaloneNote,
)
from .serializers import (
    AccountSerializer,
    BankAccountSerializer,
    BankTransferSerializer,
    BudgetSerializer,
    ExpenseCategorySerializer,
    JournalEntrySerializer,
    StandaloneNoteSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


class AccountViewSet(TenantModelViewSet):
    queryset = Account.objects.select_related("parent")
    serializer_class = AccountSerializer
    audit_entity_type = "Account"
    audit_label_field = "name"
    required_permissions = ["view_ledger"]
    status_field = None
    filter_map = {"type": "type", "subtype": "subtype", "isActive": "is_active"}
    search_fields = ["name", "code"]
    ordering = ["code"]

    def get_queryset(self):
        # Current balance = opening + debit - credit (same as the ledger view),
        # skipping reversed entries and their reversals.
        lines = JournalLine.objects.filter(
            account_id=OuterRef("pk"), deleted_at__isnull=True
        ).exclude(journal_entry__status="Reversed").exclude(
            journal_entry__reversal_of__isnull=False
        )
        money = DecimalField(max_digits=18, decimal_places=2)

        def total(column):
            return Coalesce(
                Subquery(
                    lines.values("account_id").annotate(t=Sum(column)).values("t")[:1],
                    output_field=money,
                ),
                Value(Decimal("0.00")),
                output_field=money,
            )

        return super().get_queryset().annotate(
            total_debit=total("debit"), total_credit=total("credit")
        )

    def perform_destroy(self, instance):
        if instance.is_system:
            raise Conflict(
                "System accounts cannot be deleted -- the posting matrix depends on them.",
                code="SYSTEM_ACCOUNT",
            )
        if JournalLine.objects.filter(account=instance, deleted_at__isnull=True).exists():
            raise Conflict(
                "This account has ledger entries and cannot be deleted.",
                code=Codes.IN_USE,
            )
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="tree")
    def tree(self, request):
        """``GET /accounts/chart-of-accounts/`` -- the account tree."""
        rows = list(self.filter_queryset(self.get_queryset()))
        nodes = {
            row.id: {**AccountSerializer(row).data, "children": []} for row in rows
        }
        roots = []
        for row in rows:
            node = nodes[row.id]
            parent = nodes.get(row.parent_id) if row.parent_id else None
            (parent["children"] if parent else roots).append(node)
        return Response({"accounts": roots})


class BankAccountViewSet(TenantModelViewSet):
    queryset = BankAccount.objects.select_related("account")
    serializer_class = BankAccountSerializer
    audit_entity_type = "BankAccount"
    audit_label_field = "name"
    required_permissions = ["view_bank_accounts"]
    status_field = None
    search_fields = ["name", "account_number", "bank_name"]
    ordering = ["-is_default", "name"]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        balances = {
            row["id"]: row["balance"]
            for row in services.account_balances(request.client_id)
            for _ in [0]
        }
        for bank in page:
            bank.computed_balance = balances.get(str(bank.id))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @transaction.atomic
    def perform_create(self, serializer):
        bank = super().perform_create(serializer)
        if bank.is_default:
            BankAccount.objects.filter(
                client_id=self.get_client_id(), is_default=True
            ).exclude(pk=bank.pk).update(is_default=False)
        return bank


class BalancesView(APIView):
    """``GET /accounts/balances/`` -- replaces ``getAccountBalances``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_bank_accounts"]

    def get(self, request):
        rows = services.account_balances(
            request.client_id, as_of=request.query_params.get("asOf")
        )
        return Response(
            envelope(
                rows,
                aggregates={"totalBalance": round2(sum((r["balance"] for r in rows), ZERO))},
            )
        )


class AccountingLogView(APIView):
    """``GET /accounts/accounting-log/`` -- admin view of every ledger entry
    raised by payroll, incentive, brokerage, expenses and payments in/out.

    Query: ``module`` (payroll | incentive | broker | expense | payment_in |
    payment_out), ``dateFrom``, ``dateTo``, ``search``, ``page``, ``pageSize``.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_audit_logs"]

    def get(self, request):
        from datetime import date

        from . import activity_log

        params = request.query_params

        def parse(name, alias):
            value = params.get(name) or params.get(alias)
            if not value:
                return None
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                raise ValidationFailed(
                    "Dates must be YYYY-MM-DD.", field_errors={name: ["Expected YYYY-MM-DD."]}
                )

        try:
            page = max(1, int(params.get("page") or 1))
            page_size = min(200, max(1, int(params.get("pageSize") or params.get("page_size") or 50)))
        except ValueError:
            raise ValidationFailed("page and pageSize must be numbers.")

        filters = {
            "date_from": parse("dateFrom", "date_from"),
            "date_to": parse("dateTo", "date_to"),
            "search": (params.get("search") or "").strip() or None,
        }
        rows, total = activity_log.accounting_log(
            request.client_id, module=params.get("module"), page=page, page_size=page_size, **filters
        )
        return Response(
            envelope(
                rows,
                count=total,
                page=page,
                page_size=page_size,
                aggregates={"modules": activity_log.module_summary(request.client_id, **filters)},
            )
        )


class BankTransferViewSet(TenantModelViewSet):
    queryset = BankTransfer.objects.select_related("from_account", "to_account")
    serializer_class = BankTransferSerializer
    audit_entity_type = "BankTransfer"
    required_permissions = ["view_bank_accounts"]
    status_field = None
    default_date_field = "transfer_date"
    ordering = ["-transfer_date"]

    @transaction.atomic
    def perform_create(self, serializer):
        transfer = super().perform_create(serializer)
        entry = services.post_bank_transfer(transfer, user=self.request.user)
        if entry is not None:
            transfer.journal_entry = entry
            transfer.save(update_fields=["journal_entry", "updated_at"])
        return transfer


class JournalEntryViewSet(TenantModelViewSet):
    queryset = JournalEntry.objects.prefetch_related("lines__account", "lines__party")
    serializer_class = JournalEntrySerializer
    audit_entity_type = "JournalEntry"
    audit_label_field = "entry_number"
    required_permissions = ["manage_journal_entries"]
    status_field = "status"
    default_date_field = "entry_date"
    search_fields = ["entry_number", "narration"]
    ordering = ["-entry_date", "-created_at"]
    filter_map = {
        "sourceDocumentType": "source_document_type",
        "sourceDocumentId": "source_document_id",
        "isSystem": "is_system",
    }

    def get_aggregates(self, queryset):
        rows = JournalLine.objects.filter(
            journal_entry__in=queryset.values("id"), deleted_at__isnull=True
        ).aggregate(
            debit=Coalesce(Sum("debit"), Value(Decimal("0.00")), output_field=MONEY),
            credit=Coalesce(Sum("credit"), Value(Decimal("0.00")), output_field=MONEY),
        )
        return {"count": queryset.count(), **rows}

    @transaction.atomic
    def perform_create(self, serializer):
        """Manual entries only -- system entries come from the posting matrix."""
        lines = serializer.validated_data.pop("lines")
        postings = [
            services.Posting(
                line["account"],
                debit=line.get("debit") or ZERO,
                credit=line.get("credit") or ZERO,
                party=line.get("party"),
                description=line.get("description"),
            )
            for line in lines
        ]
        entry = services.post_entry(
            client=self.request.user.client,
            postings=postings,
            entry_date=serializer.validated_data.get("entry_date"),
            narration=serializer.validated_data.get("narration"),
            is_system=False,
            user=self.request.user,
        )
        serializer.instance = entry
        self._created_instance = entry
        self._concurrency_instance = entry
        self.write_audit("create", entry, description="Manual journal entry posted")
        return entry

    def perform_update(self, serializer):
        if serializer.instance.is_system:
            raise Conflict(
                "System journal entries are immutable.",
                code="SYSTEM_ENTRY",
                detail="Cancel the source document instead, which posts a reversing entry.",
            )
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        raise Conflict(
            "Journal entries are reversed, never deleted.",
            code="SYSTEM_ENTRY",
            detail="Use /reverse/ to post a mirror entry.",
        )

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        entry = self.get_object()
        reversal = services.reverse_entry(
            entry, user=request.user, narration=request.data.get("narration")
        )
        if reversal is None:
            raise Conflict("This entry has already been reversed.", code=Codes.ALREADY_DONE)
        return Response(
            self.get_serializer(reversal).data, status=status.HTTP_201_CREATED
        )


class AccountLedgerView(APIView):
    """``GET /accounts/ledger/{accountId}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_ledger"]

    def get(self, request, account_id):
        if not Account.objects.filter(pk=account_id, client_id=request.client_id).exists():
            raise NotFound("That account no longer exists.")
        return Response(
            services.account_ledger(
                request.client_id,
                account_id,
                date_from=request.query_params.get("date_from"),
                date_to=request.query_params.get("date_to"),
            )
        )


class BudgetViewSet(TenantModelViewSet):
    queryset = Budget.objects.select_related("account")
    serializer_class = BudgetSerializer
    audit_entity_type = "Budget"
    required_permissions = ["view_financial_reports"]
    status_field = None
    default_date_field = "period_start"
    ordering = ["-period_start"]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        for budget in page:
            rows = JournalLine.objects.filter(
                client_id=request.client_id,
                account=budget.account,
                deleted_at__isnull=True,
                journal_entry__entry_date__gte=budget.period_start,
                journal_entry__entry_date__lte=budget.period_end,
            ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False).aggregate(
                debit=Coalesce(Sum("debit"), Value(Decimal("0.00")), output_field=MONEY),
                credit=Coalesce(Sum("credit"), Value(Decimal("0.00")), output_field=MONEY),
            )
            budget.actual_amount = round2(rows["debit"] - rows["credit"])
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class ExpenseCategoryViewSet(TenantModelViewSet):
    queryset = ExpenseCategory.objects.select_related("account")
    serializer_class = ExpenseCategorySerializer
    audit_entity_type = "ExpenseCategory"
    audit_label_field = "name"
    required_permissions = ["view_purchase"]
    status_field = None
    ordering = ["name"]

    def list(self, request, *args, **kwargs):
        services.seed_expense_categories(self.get_client_id())
        return super().list(request, *args, **kwargs)


class StandaloneNoteViewSet(TenantModelViewSet):
    queryset = StandaloneNote.objects.select_related("party", "return_reason", "journal_entry")
    serializer_class = StandaloneNoteSerializer
    audit_entity_type = "StandaloneNote"
    audit_label_field = "note_number"
    required_permissions = ["view_ledger"]
    # Reading needs view_ledger; raising, posting or cancelling a note needs
    # manage_journal_entries. Approval itself happens in the approvals inbox.
    permission_map = {"read": ["view_ledger"], "write": ["manage_journal_entries"]}
    draft_only_writes = True
    status_field = "status"
    ordering = ["-note_date", "-created_at"]
    filter_map = {
        "noteType": "note_type",
        "partyId": "party_id",
        "status": "status",
        "againstDocumentType": "against_document_type",
    }
    search_fields = ["note_number", "party__name", "against_document_number", "reason_notes"]

    def perform_create(self, serializer):
        data = serializer.validated_data
        note = note_services.create_standalone_note(
            client=self.request.user.client,
            party=data["party"],
            note_type=data.get("note_type", "CREDIT_NOTE"),
            taxable_amount=data.get("taxable_amount", 0),
            cgst=data.get("cgst", 0),
            sgst=data.get("sgst", 0),
            igst=data.get("igst", 0),
            against_document_type=data.get("against_document_type", "INVOICE"),
            against_document_id=data.get("against_document_id"),
            against_document_number=data.get("against_document_number"),
            return_reason=data.get("return_reason"),
            reason_notes=data.get("reason_notes"),
            notes=data.get("notes"),
            user=self.request.user,
            status=data.get("status", "Draft"),
        )
        serializer.instance = note
        return note

    def _submit(self, request):
        note, req = note_services.submit_standalone_note(self.get_object(), user=request.user)
        data = dict(self.get_serializer(note).data)
        data["approvalRequestId"] = str(req.id)
        return Response(data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Send the note to the approvals inbox (Draft → Pending Approval)."""
        return self._submit(request)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Kept for older clients: now the same as ``submit``. The decision
        itself is taken in /approvals/requests/."""
        return self._submit(request)

    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        note = note_services.post_standalone_note(self.get_object(), user=request.user)
        return Response(self.get_serializer(note).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        reason = request.data.get("reason")
        note = note_services.cancel_standalone_note(self.get_object(), user=request.user, reason=reason)
        return Response(self.get_serializer(note).data)


# ---------------------------------------------------------------------------
# Reports (api.md §8, PDF §14)
# ---------------------------------------------------------------------------
class FinancialReportView(APIView):
    """``GET /accounts/reports/{key}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    HANDLERS = {
        "profit-and-loss": "profit_and_loss",
        "balance-sheet": "balance_sheet",
        "cash-flow": "cash_flow",
        "trial-balance": "trial_balance",
        "gst-summary": "gst_summary",
        "ageing": "ageing",
        "day-book": "day_book",
        "overdue-summary": "overdue_summary",
        "sub-ledgers": "sub_ledgers",
    }

    def get(self, request, report_key):
        handler = self.HANDLERS.get(report_key)
        if handler is None:
            raise NotFound(f"Unknown report '{report_key}'.")

        params = request.query_params
        date_from = params.get("date_from") or params.get("dateFrom")
        date_to = params.get("date_to") or params.get("dateTo")
        as_of = params.get("asOf") or params.get("as_of")

        if report_key == "profit-and-loss":
            return Response(reports.profit_and_loss(request.client_id, date_from, date_to))
        if report_key == "balance-sheet":
            return Response(reports.balance_sheet(request.client_id, as_of))
        if report_key == "cash-flow":
            return Response(reports.cash_flow(request.client_id, date_from, date_to))
        if report_key == "trial-balance":
            rows = services.trial_balance(request.client_id, as_of=as_of)
            return Response(
                envelope(
                    rows,
                    aggregates={
                        "totalDebit": round2(sum((r["debit"] for r in rows), ZERO)),
                        "totalCredit": round2(sum((r["credit"] for r in rows), ZERO)),
                    },
                )
            )
        if report_key == "gst-summary":
            return Response(reports.gst_summary(request.client_id, date_from, date_to))
        if report_key == "day-book":
            return Response(reports.day_book(request.client_id, as_of_date=params.get("date") or as_of))
        if report_key == "overdue-summary":
            return Response(reports.salesperson_broker_overdue_view(request.client_id, as_of=as_of))
        if report_key == "sub-ledgers":
            return Response(reports.sub_ledger_summary(request.client_id, ledger_type=params.get("type", "jobwork"), as_of=as_of))

        return Response(
            reports.ageing(
                request.client_id, params.get("type") or "receivable", as_of
            )
        )


class TallyExportView(APIView):
    """``GET /accounts/export/tally/`` -- Tally XML export (PDF §14)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    def get(self, request):
        export_type = request.query_params.get("type", "vouchers")
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        company = getattr(request.user.client, "name", "Oscar ERP")

        if export_type == "masters":
            xml_data = tally_export.export_tally_masters_xml(request.client_id, company_name=company)
            filename = f"tally_masters_{timezone.localdate()}.xml"
        else:
            xml_data = tally_export.export_tally_vouchers_xml(
                request.client_id, date_from=date_from, date_to=date_to, company_name=company
            )
            filename = f"tally_vouchers_{timezone.localdate()}.xml"

        response = HttpResponse(xml_data, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class ExcelExportView(APIView):
    """``GET /accounts/export/excel/`` -- CSV / Excel export for registers (PDF §14)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    def get(self, request):
        register_type = request.query_params.get("register", "sales")
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        csv_data = tally_export.export_register_csv(
            request.client_id, register_type=register_type, date_from=date_from, date_to=date_to
        )
        response = HttpResponse(csv_data, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{register_type}_register_{timezone.localdate()}.csv"'
        return response
