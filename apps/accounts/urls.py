"""Auth (api.md §2) and Administration (api.md §3) routes."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views
from .customer_clients import CustomerClientViewSet

router = DefaultRouter(trailing_slash=True)
router.register("users", views.UserViewSet, basename="admin-users")
router.register("roles", views.RoleViewSet, basename="admin-roles")
router.register("clients", views.ClientViewSet, basename="admin-clients")
router.register("customer-clients", CustomerClientViewSet, basename="admin-customer-clients")

auth_urlpatterns = [
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("refresh/", views.RefreshView.as_view(), name="auth-refresh"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("me/", views.MeView.as_view(), name="auth-me"),
    path("change-password/", views.ChangePasswordView.as_view(), name="auth-change-password"),
    path("forgot-password/", views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password/", views.ResetPasswordView.as_view(), name="auth-reset-password"),
    path("sessions/", views.SessionListView.as_view(), name="auth-sessions"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="auth-session-detail"),
]

admin_urlpatterns = [
    path("permissions/", views.PermissionCatalogueView.as_view(), name="admin-permissions"),
    path("", include(router.urls)),
]
