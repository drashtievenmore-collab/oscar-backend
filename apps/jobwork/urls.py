"""Jobwork routes — /jobwork/process-plans/ and /jobwork/orders/."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("process-plans", views.ProcessPlanViewSet, basename="jobwork-process-plans")
router.register("orders", views.JobWorkOrderViewSet, basename="jobwork-orders")

urlpatterns = [
    path("", include(router.urls)),
]
