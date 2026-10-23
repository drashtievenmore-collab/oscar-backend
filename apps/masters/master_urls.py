"""Master routes for Agency, Broker, Process, JobWorkRate, RateMaster, Defect, LossReason, ReturnReason."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgencyViewSet,
    BrokerViewSet,
    DefectMasterViewSet,
    JobWorkRateMasterViewSet,
    RateMasterViewSet,
    LossReasonMasterViewSet,
    ProcessMasterViewSet,
    ReturnReasonMasterViewSet,
    TransporterViewSet,
    VehicleViewSet,
    DriverViewSet,
    PartyViewSet,
    ItemViewSet,
)

router = DefaultRouter(trailing_slash=True)
router.register("parties", PartyViewSet, basename="masters-parties")
router.register("items", ItemViewSet, basename="masters-items")
router.register("agencies", AgencyViewSet, basename="masters-agencies")
router.register("brokers", BrokerViewSet, basename="masters-brokers")
router.register("transporters", TransporterViewSet, basename="masters-transporters")
router.register("vehicles", VehicleViewSet, basename="masters-vehicles")
router.register("drivers", DriverViewSet, basename="masters-drivers")
router.register("processes", ProcessMasterViewSet, basename="masters-processes")
router.register("jobwork-rates", JobWorkRateMasterViewSet, basename="masters-jobwork-rates")
router.register("rate-masters", RateMasterViewSet, basename="masters-rate-masters")
router.register("price-lists", RateMasterViewSet, basename="masters-price-lists")
router.register("defects", DefectMasterViewSet, basename="masters-defects")
router.register("loss-reasons", LossReasonMasterViewSet, basename="masters-loss-reasons")
router.register("return-reasons", ReturnReasonMasterViewSet, basename="masters-return-reasons")

urlpatterns = [path("", include(router.urls))]
