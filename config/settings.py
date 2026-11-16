"""Django settings for the EscortArea listings API.

Values that differ per environment come from environment variables, with
development-friendly defaults. See README.md for the production checklist.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Read KEY=value lines from backend/.env (if present). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Tests never read .env, so they don't use real keys (PaymentWings, R2) or a real database.
_TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
if not _TESTING:
    _load_env_file(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production"
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Site identity (shown in the API /api/meta and used by the frontend).
SITE_NAME = os.environ.get("SITE_NAME", "EscortArea")
SITE_COUNTRY = os.environ.get("SITE_COUNTRY", "Denmark")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "support@example.com")
FREE_AD_DAYS = int(os.environ.get("FREE_AD_DAYS", "30"))

INSTALLED_APPS = [
    "daphne",  # makes `runserver` serve ASGI (HTTP + WebSockets)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "corsheaders",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

# Real-time messages. In-memory works for one server process (development);
# set REDIS_URL in production so all processes share events.
if os.environ.get("REDIS_URL"):
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [os.environ["REDIS_URL"]]}}
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Database: SQLite by default. USE_POSTGRES=true switches to PostgreSQL (settings below, from .env).
USE_POSTGRES = env_bool("USE_POSTGRES", bool(os.environ.get("POSTGRES_DB")))
if USE_POSTGRES:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "velora"),
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
            "OPTIONS": {"sslmode": os.environ["POSTGRES_SSLMODE"]} if os.environ.get("POSTGRES_SSLMODE") else {},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "core.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Copenhagen"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
# Never served over HTTP (ID documents, chat files); staff read files through the admin.
# These always stay on the server's own disk – never in the public R2 bucket.
PRIVATE_MEDIA_ROOT = Path(os.environ.get("PRIVATE_MEDIA_ROOT", BASE_DIR / "private_media"))

# Public uploads (ad pictures, ad videos, profile pictures): local MEDIA_ROOT by default, or Cloudflare R2
# with USE_R2=true. Files are then served from CDN_BASE_URL. Tests always use local files.
USE_R2 = env_bool("USE_R2", False) and not _TESTING
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if USE_R2:
    from urllib.parse import urlparse

    _cdn = urlparse(os.environ.get("CDN_BASE_URL", ""))
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["R2_BUCKET"],
            "endpoint_url": os.environ["R2_ENDPOINT"],
            "access_key": os.environ["R2_ACCESS_KEY_ID"],
            "secret_key": os.environ["R2_SECRET_ACCESS_KEY"],
            "region_name": "auto",
            "signature_version": "s3v4",
            "addressing_style": "path",
            "default_acl": None,  # R2 has no ACLs; the bucket's public access decides
            "querystring_auth": False,  # plain public URLs on the CDN
            "file_overwrite": False,
            "custom_domain": (_cdn.netloc + _cdn.path.rstrip("/")) or None,
            "url_protocol": f"{_cdn.scheme or 'https'}:",
            "location": os.environ.get("R2_PREFIX", "media"),
            "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
        },
    }
    MEDIA_URL = (os.environ.get("CDN_BASE_URL", "").rstrip("/") + "/" + os.environ.get("R2_PREFIX", "media") + "/")

# Uploads: pictures are re-encoded to WebP and resized on upload.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PICTURES_PER_AD = 12
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_BYTES

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 24,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "300/min",
        "user": "600/min",
        "auth": "10/min",
        "report": "20/hour",
        "messages": "30/min",
        "tags": "30/hour",
    },
}

# The Next.js app proxies /api and /media to Django, so the browser talks to a
# single origin and session + CSRF cookies just work. CORS is only for direct calls.
FRONTEND_ORIGINS = env_list("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3100,http://127.0.0.1:3100")
CORS_ALLOWED_ORIGINS = FRONTEND_ORIGINS
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = FRONTEND_ORIGINS

# Login lives only in the HttpOnly "session_id" cookie (the browser never sees a token in JS/localStorage).
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "session_id")
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", 14 * 24 * 3600))  # 14 days
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Payments (PaymentWings – app.paymentwings.com → Settings → API). Without keys, DEBUG builds use a fake local gateway.
# Sandbox keys run in test mode on the same URL.
PAYMENTWINGS_PUBLIC_KEY = os.environ.get("PAYMENTWINGS_PUBLIC_KEY", "")
PAYMENTWINGS_PRIVATE_KEY = os.environ.get("PAYMENTWINGS_PRIVATE_KEY", "")
PAYMENTWINGS_WEBHOOK_KEY = os.environ.get("PAYMENTWINGS_WEBHOOK_KEY", "")
PAYMENTWINGS_BASE_URL = os.environ.get("PAYMENTWINGS_BASE_URL", "https://api.paymentwings.com")
PAYMENTS_ALLOW_FAKE = env_bool("PAYMENTS_ALLOW_FAKE", DEBUG)
# Public URLs: where the browser returns after paying, and where PaymentWings can reach
# the webhook (the Next.js proxy forwards /api to Django).
PUBLIC_SITE_URL = os.environ.get("PUBLIC_SITE_URL", "http://localhost:3000").rstrip("/")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", PUBLIC_SITE_URL).rstrip("/")

# Tell the Next.js frontend to refresh cached site settings/plans when they change.
NEXT_REVALIDATE_URL = os.environ.get("NEXT_REVALIDATE_URL", "http://127.0.0.1:3000/internal/revalidate")
REVALIDATE_SECRET = os.environ.get("REVALIDATE_SECRET", "dev-revalidate-secret")
APP_VERSION = os.environ.get("APP_VERSION", "dev")

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
# Sender of every e-mail (must be an address your SMTP provider lets you send from) and the brand name shown.
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", CONTACT_EMAIL)
EMAIL_BRAND_NAME = os.environ.get("EMAIL_BRAND_NAME", "")
EMAIL_TIMEOUT = 15
# aaPanel Mail Server HTTP API (EMAIL_BACKEND=core.mail_backends.BTPanelEmailBackend)
BTPANEL_MAIL_URL = os.environ.get("BTPANEL_MAIL_URL", "")
BTPANEL_MAIL_FROM = os.environ.get("BTPANEL_MAIL_FROM", "")
BTPANEL_MAIL_PASSWORD = os.environ.get("BTPANEL_MAIL_PASSWORD", "")
BTPANEL_MAIL_VERIFY_SSL = env_bool("BTPANEL_MAIL_VERIFY_SSL", False)
# Password reset links work for 2 hours.
PASSWORD_RESET_TIMEOUT = int(os.environ.get("PASSWORD_RESET_TIMEOUT", 2 * 3600))
# SMTP (used when EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
