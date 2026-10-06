"""Purchase routes (api.md §6)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.inventory.views import QualityStandardViewSet

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("orders", views.PurchaseOrderViewSet, basename="purchase-orders")
router.register("bills", views.PurchaseBillViewSet, basename="purchase-bills")
router.register("receipts", views.GoodsReceiptViewSet, basename="purchase-receipts")
router.register("payments", views.PaymentOutViewSet, basename="purchase-payments")
router.register("returns", views.PurchaseReturnViewSet, basename="purchase-returns")
router.register("expenses", views.ExpenseViewSet, basename="purchase-expenses")
router.register("vendors", views.VendorLookupViewSet, basename="purchase-vendors")
router.register(
    "vendor-bills", views.VendorBillViewSet, basename="purchase-vendor-bills"
)
router.register(
    "quality-standards", QualityStandardViewSet, basename="purchase-quality-standards"
)

urlpatterns = [path("", include(router.urls))]
