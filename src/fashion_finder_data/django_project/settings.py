"""Django settings for the local catalogue and review admin."""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
SECRET_KEY = os.environ.get("FASHION_FINDER_SECRET_KEY") or secrets.token_urlsafe(48)
DEBUG = os.environ.get("FASHION_FINDER_DEBUG") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "FASHION_FINDER_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
    ).split(",")
    if host.strip()
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "fashion_finder_data.catalogue",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "fashion_finder_data.django_project.cors.CorsMiddleware",
]
if os.environ.get("FASHION_FINDER_SERVE_STATIC") == "1":
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
ROOT_URLCONF = "fashion_finder_data.django_project.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "fashion_finder_data.django_project.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(
            Path(os.environ.get("FASHION_FINDER_DB", "data/fashion_finder.sqlite")).resolve()
        ),
    }
}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("FASHION_FINDER_STATIC_ROOT", BASE_DIR / "staticfiles"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("FASHION_FINDER_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if "*" in os.environ.get("FASHION_FINDER_CORS_ORIGINS", "").split(","):
    raise ValueError("CORS origins must be explicit")
