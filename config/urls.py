"""
Root URL configuration.

Base path ``/api/v1`` with DRF trailing slashes (api.md §1.1). Every path here
matches what ``services/domainServices.js`` already emits -- api.md §0 is
explicit that those signatures are part of the contract.

Module visibility is driven by ``settings.ENABLED_MODULES``
(``HRMS_ONLY`` / ``ENABLED_MODULES`` in ``.env``). Disabled modules keep
their code and migrations -- their routes are just not mounted -- so
re-enabling is an env flip, not a code change. String ``include()`` targets
are used so a disabled module is never imported.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.accounts.urls import admin_urlpatterns, auth_urlpatterns


def enabled(name):
    return name.lower() in getattr(settings, "ENABLED_MODULES", ["hrms"])


api_v1 = [
    # Identity and administration (api.md §2, §3) -- always on, HRMS needs them.
    path("auth/", include(auth_urlpatterns)),
    path("admin/", include(admin_urlpatterns)),
]

# Shared masters (api.md §4)
if enabled("masters"):
    api_v1.append(path("parties/", include("apps.masters.urls")))
if enabled("inventory"):
    api_v1.append(path("inventory/", include("apps.inventory.urls")))

# Documents (api.md §5, §6)
if enabled("sales"):
    api_v1.append(path("sales/", include("apps.sales.urls")))
if enabled("purchase"):
    api_v1.append(path("purchase/", include("apps.purchase.urls")))

# Accounts (api.md §8). HRMS payroll posts the ledger internally even when
# this route is off; the route only controls the /accounts/ listing UI.
if enabled("accounting"):
    api_v1.append(path("accounts/", include("apps.accounting.urls")))

# Modules (api.md §9, §10, §11)
if enabled("crm"):
    api_v1.append(path("crm/", include("apps.crm.urls")))
if enabled("pms"):
    api_v1.append(path("pms/", include("apps.pms.urls")))
if enabled("hrms"):
    api_v1.append(path("hrms/", include("apps.hrms.urls")))
if enabled("production"):
    # Served from apps.pms.production_urls. Code lives in PMS per product decision.
    api_v1.append(path("production/", include("apps.pms.production_urls")))

# Reports and dashboards (api.md §12). Imported lazily so HRMS-only mode
# never imports reports (which pulls sales/inventory views at module load).
if enabled("reports"):
    from apps.reports.urls import report_urlpatterns

    api_v1.append(path("reports/", include(report_urlpatterns)))
if enabled("dashboard"):
    from apps.reports.urls import dashboard_urlpatterns

    api_v1.append(path("dashboard/", include(dashboard_urlpatterns)))

# Unauthenticated surfaces (api.md §5.3, §9.7, §10.6, §11.5).
# NOTE: this bundle includes the HRMS careers portal, so enabling "public"
# also re-exposes quotation/proof/lead-form links. Keep off for HRMS-only dev.
if enabled("public"):
    from apps.reports.urls import public_urlpatterns

    api_v1.append(path("public/", include(public_urlpatterns)))

api_v1 += [
    # Platform: files, notifications, audit, settings, search, support
    # Always on -- HRMS needs files (avatars, resumes) and settings.
    path("", include("apps.core.urls")),
    # OpenAPI 3.1, generated from the models so it cannot drift (db.md §14.2)
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

urlpatterns = [
    path("api/v1/", include(api_v1)),
    path("django-admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
