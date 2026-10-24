"""Reports, dashboard and public routes (api.md §12, and the public surfaces)."""
from django.urls import path

from . import public_views, views

report_urlpatterns = [
    path("", views.ReportCatalogueView.as_view(), name="reports-catalogue"),
    path(
        "<str:report_key>/export/",
        views.ReportExportView.as_view(),
        name="reports-export",
    ),
    path("<str:report_key>/", views.ReportView.as_view(), name="reports-run"),
]

dashboard_urlpatterns = [
    path("", views.MainDashboardView.as_view(), name="dashboard"),
]

public_urlpatterns = [
    # Quotation sharing (api.md §5.3)
    path(
        "quotations/<str:quotation_number>/<str:token>/",
        public_views.PublicQuotationView.as_view(),
        name="public-quotation",
    ),
    path(
        "quotations/<str:quotation_number>/<str:token>/accept/",
        public_views.PublicQuotationAcceptView.as_view(),
        name="public-quotation-accept",
    ),
    path(
        "quotations/<str:quotation_number>/<str:token>/reject/",
        public_views.PublicQuotationRejectView.as_view(),
        name="public-quotation-reject",
    ),
    path(
        "quotations/<str:quotation_number>/<str:token>/comment/",
        public_views.PublicQuotationCommentView.as_view(),
        name="public-quotation-comment",
    ),
    # PMS client proof approval (api.md §10.6)
    path(
        "pms/approve/<str:token>/",
        public_views.PublicProofView.as_view(),
        name="public-proof",
    ),
    path(
        "pms/approve/<str:token>/decide/",
        public_views.PublicProofDecisionView.as_view(),
        name="public-proof-decide",
    ),
    # PMS daily production entry link
    path(
        "production/<str:token>/",
        public_views.PublicProductionLinkView.as_view(),
        name="public-production-link",
    ),
    path(
        "production/<str:token>/entries/",
        public_views.PublicProductionEntryView.as_view(),
        name="public-production-entry",
    ),
    # Public lead forms (api.md §9.7)
    path(
        "forms/<slug:slug>/",
        public_views.PublicFormView.as_view(),
        name="public-form",
    ),
    path(
        "forms/<slug:slug>/submit/",
        public_views.PublicFormSubmitView.as_view(),
        name="public-form-submit",
    ),
    # Career portal (api.md §11.5)
    path("careers/", public_views.PublicCareersListView.as_view(), name="public-careers"),
    path(
        "careers/<str:job_id>/",
        public_views.PublicCareerDetailView.as_view(),
        name="public-career-detail",
    ),
    path(
        "careers/<str:job_id>/apply/",
        public_views.PublicCareerApplyView.as_view(),
        name="public-career-apply",
    ),
    path(
        "careers/<str:job_id>/upload-url/",
        public_views.PublicUploadUrlView.as_view(),
        name="public-career-upload-url",
    ),
]
