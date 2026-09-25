"""Production monitoring routes, served from PMS."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import production_views as views

router = DefaultRouter(trailing_slash=True)
router.register(
    "instructions", views.ProductionInstructionViewSet, basename="production-instructions"
)
router.register(
    "daily-entries", views.DailyProductionEntryViewSet, basename="production-entries"
)
router.register(
    "incentive-schemes", views.IncentiveSchemeViewSet, basename="production-schemes"
)
router.register(
    "incentive-calcs",
    views.IncentiveCalculationViewSet,
    basename="production-incentive-calcs",
)

urlpatterns = [
    path(
        "dashboard/agencies/",
        views.AgencyProductionView.as_view(),
        name="production-agency-dashboard",
    ),
    path(
        "dashboard/employees/",
        views.EmployeeProductionView.as_view(),
        name="production-employee-dashboard",
    ),
    path(
        "dashboard/salesperson/",
        views.SalespersonView.as_view(),
        name="production-salesperson-dashboard",
    ),
    path("", include(router.urls)),
]
