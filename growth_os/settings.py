from __future__ import annotations

import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
ENVIRONMENT = os.getenv("GROWTH_OS_ENV", "local").strip().lower()
if ENVIRONMENT not in {"local", "staging", "production"}:
    raise ImproperlyConfigured("GROWTH_OS_ENV must be local, staging, or production.")
IS_LOCAL = ENVIRONMENT == "local"
IS_PRODUCTION = ENVIRONMENT == "production"

# Deployment identity is intentionally small and non-sensitive.  The release
# SHA is baked into Docker images at build time; source checkouts may leave it
# as ``unknown`` so they never claim to be a traceable deployment by accident.
DEPLOYMENT_STAGE = os.getenv("GROWTH_OS_DEPLOYMENT_STAGE", ENVIRONMENT).strip().lower()
if DEPLOYMENT_STAGE not in {"local", "staging", "staging-candidate", "production"}:
    raise ImproperlyConfigured(
        "GROWTH_OS_DEPLOYMENT_STAGE must be local, staging, staging-candidate, or production."
    )

RELEASE_SHA = os.getenv("GROWTH_OS_RELEASE_SHA", "unknown").strip()
if RELEASE_SHA != "unknown" and not re.fullmatch(r"[0-9a-f]{40}", RELEASE_SHA):
    raise ImproperlyConfigured(
        "GROWTH_OS_RELEASE_SHA must be 'unknown' or a full 40-character lowercase Git SHA."
    )


def csv_env(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in os.getenv(name, default).split(",") if part.strip()]


def positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} must be an integer.") from error
    if value < 1:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


def secret_env_or_file(
    name: str,
    *,
    required: bool = False,
    file_only: bool = False,
) -> str:
    """Read one secret from either NAME or NAME_FILE, never both.

    ``*_FILE`` supports read-only container secret mounts without placing the
    value in Compose files or command arguments.  Error messages deliberately
    name only the setting, never the secret value.
    """

    direct_value = os.getenv(name)
    file_value = os.getenv(f"{name}_FILE")
    if direct_value is not None and file_value is not None:
        raise ImproperlyConfigured(f"Set only one of {name} or {name}_FILE.")
    if file_only and direct_value is not None:
        raise ImproperlyConfigured(
            f"{name} must use the {name}_FILE secret mount outside Local."
        )

    value = direct_value
    if file_value is not None:
        secret_path = Path(file_value)
        try:
            value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ImproperlyConfigured(f"Unable to read {name}_FILE.") from error

    if value is not None and any(character in value for character in ("\x00", "\r", "\n")):
        raise ImproperlyConfigured(f"{name} must be a single-line secret.")
    if required and not value:
        raise ImproperlyConfigured(f"{name} or {name}_FILE must be supplied.")
    return value or ""


SECRET_KEY = secret_env_or_file(
    "DJANGO_SECRET_KEY",
    required=not IS_LOCAL,
    file_only=not IS_LOCAL,
)
if not SECRET_KEY:
    SECRET_KEY = "local-development-only-not-for-deployment"
if not IS_LOCAL and (
    len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY.startswith("django-insecure-")
):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY_FILE must contain a strong deployment signing key."
    )

DEBUG = IS_LOCAL and os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = csv_env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if IS_LOCAL else "")
CSRF_TRUSTED_ORIGINS = csv_env("DJANGO_CSRF_TRUSTED_ORIGINS")
if not IS_LOCAL and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be explicit outside local development.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "accounts",
    "products",
    "workflow",
    "contentops",
    "releasegate",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "growth_os.urls"

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
    }
]

WSGI_APPLICATION = "growth_os.wsgi.application"
ASGI_APPLICATION = "growth_os.asgi.application"

DATABASE_ENGINE = os.getenv("DATABASE_ENGINE", "sqlite" if IS_LOCAL else "postgresql").lower()
if DATABASE_ENGINE == "sqlite":
    if not IS_LOCAL:
        raise ImproperlyConfigured("SQLite is allowed only for local lightweight development.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
elif DATABASE_ENGINE == "postgresql":
    required_db_values = {
        "NAME": os.getenv("POSTGRES_DB", ""),
        "USER": os.getenv("POSTGRES_USER", ""),
        "PASSWORD": secret_env_or_file(
            "POSTGRES_PASSWORD",
            required=True,
            file_only=not IS_LOCAL,
        ),
        "HOST": os.getenv("POSTGRES_HOST", ""),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
    missing = [key for key, value in required_db_values.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"Missing PostgreSQL configuration: {', '.join(missing)}")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            **required_db_values,
            "CONN_MAX_AGE": int(os.getenv("POSTGRES_CONN_MAX_AGE", "60")),
            "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "prefer" if IS_LOCAL else "require")},
        }
    }
else:
    raise ImproperlyConfigured("DATABASE_ENGINE must be sqlite or postgresql.")

AUTH_USER_MODEL = "accounts.Principal"
AUTHENTICATION_BACKENDS = ["accounts.backends.PrincipalStatusBackend"]

PASSWORD_MIN_LENGTH = positive_int_env("PASSWORD_MIN_LENGTH", 6 if IS_LOCAL else 12)
required_password_minimum = 6 if IS_LOCAL else 12
if PASSWORD_MIN_LENGTH < required_password_minimum:
    raise ImproperlyConfigured(
        f"PASSWORD_MIN_LENGTH must be at least {required_password_minimum} in {ENVIRONMENT}."
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "accounts.password_validation.NonBlankAndNoControlCharactersValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": PASSWORD_MIN_LENGTH},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not IS_LOCAL
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if not IS_LOCAL:
    if os.getenv("TRUST_PROXY_SSL_HEADER", "0") == "1":
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    # Container health checks use the private HTTP listener. The reverse proxy
    # must still redirect public HTTP before traffic reaches Django.
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

if IS_PRODUCTION:
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "loggers": {},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
