"""Platform routes: files, notifications, audit, settings, search, support."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views, views_settings, approval_views

router = DefaultRouter(trailing_slash=True)
router.register("files", views.FileViewSet, basename="files")
router.register("notifications", views.NotificationViewSet, basename="notifications")
router.register("audit", views.AuditLogViewSet, basename="audit")
router.register("approvals/rules", approval_views.ApprovalRuleViewSet, basename="approval-rules")
router.register("approvals/requests", approval_views.ApprovalRequestViewSet, basename="approval-requests")
router.register("alerts/incidents", approval_views.AlertIncidentViewSet, basename="alert-incidents")
router.register("alerts/settings", approval_views.AlertSettingViewSet, basename="alert-settings")

file_urlpatterns = [
    path("upload-url/", views.UploadUrlView.as_view(), name="file-upload-url"),
    path("<uuid:pk>/upload/", views.FileUploadView.as_view(), name="file-upload"),
    path("<uuid:pk>/commit/", views.FileCommitView.as_view(), name="file-commit"),
    path("<uuid:pk>/download/", views.FileDownloadView.as_view(), name="file-download"),
]

settings_urlpatterns = [
    path(
        "company-profile/",
        views_settings.CompanyProfileView.as_view(),
        name="settings-company-profile",
    ),
    path("backup/", views_settings.BackupView.as_view(), name="settings-backup"),
    path("restore/", views_settings.RestoreView.as_view(), name="settings-restore"),
    path(
        "reset-demo-data/",
        views_settings.ResetDemoDataView.as_view(),
        name="settings-reset-demo",
    ),
    path(
        "exchange-rates/", views.ExchangeRatesView.as_view(), name="settings-exchange-rates"
    ),
    # api.md §3.4 -- an alias of /audit/ for the admin screen.
    path(
        "audit-logs/",
        views.AuditLogViewSet.as_view({"get": "list"}),
        name="settings-audit-logs",
    ),
    # The generic blob endpoints: preferences, tax, numbering, print-templates.
    path("<str:key>/", views.SettingBlobView.as_view(), name="settings-blob"),
]

support_router = DefaultRouter(trailing_slash=True)
support_router.register("tickets", views.SupportTicketViewSet, basename="support-tickets")

urlpatterns = [
    path("files/", include(file_urlpatterns)),
    path("settings/", include(settings_urlpatterns)),
    path("support/", include(support_router.urls)),
    path("events/stream/", views.EventStreamView.as_view(), name="events-stream"),
    path("search/", views.GlobalSearchView.as_view(), name="global-search"),
    path("signatures/", views.DocumentSignatureView.as_view(), name="document-signatures"),
    path(
        "approvals/discount-policy/",
        approval_views.DiscountPolicyView.as_view(),
        name="approval-discount-policy",
    ),
    path("", include(router.urls)),
]
