"""Jobwork routes — /jobwork/process-plans/ and /jobwork/orders/."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("process-plans", views.ProcessPlanViewSet, basename="jobwork-process-plans")
router.register("orders", views.JobWorkOrderViewSet, basename="jobwork-orders")

urlpatterns = [
    path("itc-04/", views.ITC04ReportView.as_view(), name="jobwork-itc04-report"),
    path("itc-04/export/", views.ITC04ExportView.as_view(), name="jobwork-itc04-export"),
    path("", include(router.urls)),
]
