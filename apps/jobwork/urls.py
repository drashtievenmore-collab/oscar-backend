"""Jobwork routes — /jobwork/process-plans/ and /jobwork/orders/."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("process-plans", views.ProcessPlanViewSet, basename="jobwork-process-plans")
router.register("orders", views.JobWorkOrderViewSet, basename="jobwork-orders")
router.register("process-instructions", views.VendorProcessInstructionViewSet, basename="jobwork-process-instructions")
router.register("pi-entries", views.VPIProgressEntryViewSet, basename="jobwork-pi-entries")

urlpatterns = [
    path("", include(router.urls)),
]
