"""
Django settings for the Evenmore ERP backend.

Implements the global conventions of api.md §1 and db.md §1:
  - base path /api/v1, DRF trailing-slash routes
  - JWT bearer auth, 401 (never 403) for an expired token
  - the list envelope with `aggregates`
  - the error body { message, code, detail, field_errors }
"""
from datetime import timedelta
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    raw = os.environ.get(key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key-do-not-use-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0,testserver")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # third party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    # project
    # NOTE: All apps stay installed here even in HRMS-only mode, because
    # masters/accounting/inventory/sales have hard FKs between them (e.g.
    # hrms.Payslip -> accounting, accounting -> masters.Party,
    # masters.ItemSerial -> inventory/sales). Removing them from
    # INSTALLED_APPS breaks `manage.py check`. HRMS-only mode only disables
    # their API routes (see ENABLED_MODULES / config/urls.py).
    "apps.core",
    "apps.accounts",
    "apps.masters",
    "apps.sales",
    "apps.purchase",
    "apps.inventory",
    "apps.accounting",
    "apps.crm",
    "apps.pms",
    "apps.hrms",
    "apps.reports",
]

# --------------------------------------------------------------------------
# Module toggle (HRMS-only dev mode)
# --------------------------------------------------------------------------
#: HRMS_ONLY=True exposes only auth/admin/hrms (+ core platform) routes.
#: HRMS_ONLY=False exposes the full ERP. Flip in `.env`, no code edits.
#: Optionally override the exact route set with ENABLED_MODULES (comma-separated).
HRMS_ONLY = env_bool("HRMS_ONLY", True)

#: Route keys used by config/urls.py. "auth"/"admin" (accounts) and "core"
#: platform routes are always on -- HRMS needs login, users and files.
#: "production" is a route key backed by apps.pms, not a separate app.
HRMS_ONLY_MODULES = ["hrms", "production"]
FULL_MODULES = [
    "masters",
    "inventory",
    "sales",
    "purchase",
    "accounting",
    "crm",
    "pms",
    "hrms",
    "production",
    "reports",
    "dashboard",
    "public",
]

_custom_modules = env_list("ENABLED_MODULES", "")
if _custom_modules:
    ENABLED_MODULES = [m.strip().lower() for m in _custom_modules if m.strip()]
elif HRMS_ONLY:
    ENABLED_MODULES = list(HRMS_ONLY_MODULES)
else:
    ENABLED_MODULES = list(FULL_MODULES)


def module_enabled(name):
    """Route-level toggle. Apps stay in INSTALLED_APPS for FK integrity."""
    return name.lower() in ENABLED_MODULES

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Clears the per-request tenant afterwards (db.md §1.3).
    "apps.core.tenancy.TenantMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "evenmore_erp"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", "postgres"),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# auth.E003 wants USERNAME_FIELD globally unique. db.md §2.2 scopes it per
# tenant (`unique (client_id, email)`) because the same address may legitimately
# belong to two tenants. Authentication resolves the tenant before the user
# (apps.accounts.views.LoginView), so global uniqueness is not required -- and
# enforcing it would make tenant isolation leak into the login form.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# DRF (api.md §1.3 - §1.5)
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.accounts.authentication.TenantJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("apps.core.permissions.IsAuthenticatedInTenant",),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.EnvelopePagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": (
        "apps.core.filters.StandardListFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    # api.md §1.6 -- money is a number with 2 decimals, never a formatted string.
    "COERCE_DECIMAL_TO_STRING": False,
    "DEFAULT_THROTTLE_RATES": {
        # Public/unauthenticated surfaces: quotation links, proof links, careers,
        # public lead forms (api.md §5.3, §9.7, §10.6, §11.5).
        "public": "60/min",
        "public_write": "10/min",
        "login": "20/min",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("ACCESS_TOKEN_MINUTES", "60"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("REFRESH_TOKEN_DAYS", "14"))),
    "ROTATE_REFRESH_TOKENS": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_TYPE_CLAIM": "token_type",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Evenmore ERP API",
    "DESCRIPTION": "Unified ERP / CRM / HRMS / PMS backend for Sweven Fabricators.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
}

# --------------------------------------------------------------------------
# CORS (api-integration.md blocker 6)
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
)
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "origin",
    "user-agent",
    "x-requested-with",
    "idempotency-key",
    "if-unmodified-since",
)
CORS_EXPOSE_HEADERS = ("last-modified", "idempotency-replayed")

# --------------------------------------------------------------------------
# Application settings
# --------------------------------------------------------------------------
API_BASE_PATH = "/api/v1"
FILE_STORAGE_BACKEND = env("FILE_STORAGE_BACKEND", "local")
FILE_MAX_BYTES = int(env("FILE_MAX_BYTES", str(25 * 1024 * 1024)))
PMS_PROOF_MAX_BYTES = int(env("PMS_PROOF_MAX_BYTES", str(50 * 1024 * 1024)))
UPLOAD_URL_TTL_SECONDS = 900
#: Signed file download/preview URLs (``previewUrl`` on PMS documents). Long
#: enough that a cached project still previews after days, not just an hour.
FILE_DOWNLOAD_TTL_SECONDS = int(env("FILE_DOWNLOAD_TTL_SECONDS", str(7 * 24 * 3600)))
PUBLIC_SHARE_DEFAULT_EXPIRY_DAYS = 14
EXCHANGE_RATE_URL = env("EXCHANGE_RATE_URL", "https://open.er-api.com/v6/latest/USD")
EXCHANGE_RATE_CACHE_SECONDS = 24 * 60 * 60

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "evenmore-erp",
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}
