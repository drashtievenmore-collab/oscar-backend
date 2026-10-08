"""
Inventory routes (api.md §4.2, §4.3, §7).

The item, category, unit and location masters live in ``apps.masters`` but are
routed under ``/inventory/`` because that is where the frontend calls them
(api.md §4.2 -- ``GET /inventory/items/``).
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.masters.views import (
    FabricViewSet,
    ItemCategoryViewSet,
    ItemViewSet,
    LocationViewSet,
    UnitViewSet,
)

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("items", ItemViewSet, basename="inventory-items")
router.register("categories", ItemCategoryViewSet, basename="inventory-categories")
router.register("units", UnitViewSet, basename="inventory-units")
router.register("locations", LocationViewSet, basename="inventory-locations")
router.register("fabrics", FabricViewSet, basename="inventory-fabrics")
router.register("movements", views.StockMovementViewSet, basename="inventory-movements")
router.register("transfers", views.StockTransferViewSet, basename="inventory-transfers")
router.register("faulty-parts", views.FaultyPartViewSet, basename="inventory-faulty-parts")
router.register("service-usage", views.ServiceUsageViewSet, basename="inventory-service-usage")
router.register("zone-requests", views.ZoneRequestViewSet, basename="inventory-zone-requests")
router.register("item-parts", views.ItemPartViewSet, basename="inventory-item-parts")
router.register("audits", views.StockAuditViewSet, basename="inventory-audits")

urlpatterns = [
    path("stock/summary/", views.StockSummaryView.as_view(), name="inventory-stock-summary"),
    path("stock/", views.StockPositionView.as_view(), name="inventory-stock"),
    path("adjustments/", views.StockAdjustmentView.as_view(), name="inventory-adjustments"),
    path("valuation/", views.ValuationView.as_view(), name="inventory-valuation"),
    path("", include(router.urls)),
]
