"""Master routes for Agency, Broker, Process, JobWorkRate, Defect, LossReason, ReturnReason."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgencyViewSet,
    BrokerViewSet,
    DefectMasterViewSet,
    JobWorkRateMasterViewSet,
    LossReasonMasterViewSet,
    ProcessMasterViewSet,
    ReturnReasonMasterViewSet,
    TransporterViewSet,
)

router = DefaultRouter(trailing_slash=True)
router.register("agencies", AgencyViewSet, basename="masters-agencies")
router.register("brokers", BrokerViewSet, basename="masters-brokers")
router.register("transporters", TransporterViewSet, basename="masters-transporters")
router.register("processes", ProcessMasterViewSet, basename="masters-processes")
router.register("jobwork-rates", JobWorkRateMasterViewSet, basename="masters-jobwork-rates")
router.register("defects", DefectMasterViewSet, basename="masters-defects")
router.register("loss-reasons", LossReasonMasterViewSet, basename="masters-loss-reasons")
router.register("return-reasons", ReturnReasonMasterViewSet, basename="masters-return-reasons")

urlpatterns = [path("", include(router.urls))]
