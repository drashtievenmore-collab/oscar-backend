"""Sales routes (api.md §5). Paths match ``domainServices.js`` exactly."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("quotations", views.QuotationViewSet, basename="sales-quotations")
router.register("orders", views.SalesOrderViewSet, basename="sales-orders")
router.register(
    "proforma-invoices", views.ProformaInvoiceViewSet, basename="sales-proformas"
)
router.register("challans", views.DeliveryChallanViewSet, basename="sales-challans")
router.register("invoices", views.SalesInvoiceViewSet, basename="sales-invoices")
router.register("payments", views.PaymentInViewSet, basename="sales-payments")
router.register("returns", views.SalesReturnViewSet, basename="sales-returns")
router.register("warranties", views.WarrantyCardViewSet, basename="sales-warranties")

urlpatterns = [path("", include(router.urls))]
