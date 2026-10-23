"""Accounts routes (api.md §8)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("bank-accounts", views.BankAccountViewSet, basename="accounts-bank-accounts")
router.register("journal", views.JournalEntryViewSet, basename="accounts-journal")
router.register("budgets", views.BudgetViewSet, basename="accounts-budgets")
router.register("transfers", views.BankTransferViewSet, basename="accounts-transfers")
router.register(
    "expense-categories", views.ExpenseCategoryViewSet, basename="accounts-expense-categories"
)
router.register("chart-of-accounts", views.AccountViewSet, basename="accounts-coa")
router.register("standalone-notes", views.StandaloneNoteViewSet, basename="accounts-standalone-notes")

urlpatterns = [
    path("balances/", views.BalancesView.as_view(), name="accounts-balances"),
    path("accounting-log/", views.AccountingLogView.as_view(), name="accounts-accounting-log"),
    path(
        "ledger/<uuid:account_id>/",
        views.AccountLedgerView.as_view(),
        name="accounts-ledger",
    ),
    path(
        "reports/<str:report_key>/",
        views.FinancialReportView.as_view(),
        name="accounts-reports",
    ),
    path("export/tally/", views.TallyExportView.as_view(), name="accounts-tally-export"),
    path("export/excel/", views.ExcelExportView.as_view(), name="accounts-excel-export"),
    path("", include(router.urls)),
]
