"""
Inventory routes (api.md §4.2, §4.3, §7).

The item, category, unit and location masters live in ``apps.masters`` but are
routed under ``/inventory/`` because that is where the frontend calls them
(api.md §4.2 -- ``GET /inventory/items/``).
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.masters.views import (
    DesignViewSet,
    FabricQualityViewSet,
    FabricTypeViewSet,
    FabricVariantViewSet,
    FabricViewSet,
    ItemCategoryViewSet,
    ItemViewSet,
    LocationViewSet,
    ShadeViewSet,
    UnitViewSet,
)

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("items", ItemViewSet, basename="inventory-items")
router.register("categories", ItemCategoryViewSet, basename="inventory-categories")
router.register("units", UnitViewSet, basename="inventory-units")
router.register("locations", LocationViewSet, basename="inventory-locations")
router.register("fabrics", FabricViewSet, basename="inventory-fabrics")
router.register("fabric-qualities", FabricQualityViewSet, basename="inventory-fabric-qualities")
router.register("fabric-types", FabricTypeViewSet, basename="inventory-fabric-types")
router.register("designs", DesignViewSet, basename="inventory-designs")
router.register("shades", ShadeViewSet, basename="inventory-shades")
router.register("fabric-variants", FabricVariantViewSet, basename="inventory-fabric-variants")
router.register("movements", views.StockMovementViewSet, basename="inventory-movements")
router.register("transfers", views.StockTransferViewSet, basename="inventory-transfers")
router.register("faulty-parts", views.FaultyPartViewSet, basename="inventory-faulty-parts")
router.register("service-usage", views.ServiceUsageViewSet, basename="inventory-service-usage")
router.register("zone-requests", views.ZoneRequestViewSet, basename="inventory-zone-requests")
router.register("item-parts", views.ItemPartViewSet, basename="inventory-item-parts")
router.register("audits", views.StockAuditViewSet, basename="inventory-audits")

urlpatterns = [
    path("dashboard/", views.InventoryDashboardView.as_view(), name="inventory-dashboard"),
    path("qc/dashboard/", views.QCDashboardView.as_view(), name="inventory-qc-dashboard"),
    path("qc/pending/", views.QCPendingInspectionsView.as_view(), name="inventory-qc-pending"),
    path("qc/pending/bulk-assign/", views.QCBulkAssignView.as_view(), name="inventory-qc-bulk-assign"),
    path("qc/complete/", views.QCCompleteInspectionView.as_view(), name="inventory-qc-complete"),
    path("qc/register/", views.QCRegisterView.as_view(), name="inventory-qc-register"),
    path("qc/register/manual/", views.QCRegisterView.as_view(), name="inventory-qc-register-manual"),
    path("qc/disposition/", views.QCDispositionView.as_view(), name="inventory-qc-disposition"),
    path("qc/reinspection/", views.QCReinspectionView.as_view(), name="inventory-qc-reinspection"),
    path("qc/reports/", views.QCReportsView.as_view(), name="inventory-qc-reports"),
    path("stock/summary/", views.StockSummaryView.as_view(), name="inventory-stock-summary"),
    path("stock/", views.StockPositionView.as_view(), name="inventory-stock"),
    path("adjustments/", views.StockAdjustmentView.as_view(), name="inventory-adjustments"),
    path("lot-register/", views.LotTakaRegisterView.as_view(), name="inventory-lot-register"),
    path("lot-register/<uuid:pk>/", views.LotTakaRegisterView.as_view(), name="inventory-lot-register-detail"),
    path("", include(router.urls)),
]
