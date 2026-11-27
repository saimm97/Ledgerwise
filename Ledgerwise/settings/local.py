# Ledgerwise/settings/local.py
import os

from .base import *  # noqa: F403
from .base import require_env

# Local development: set DJANGO_DEBUG=True in your .env (see .env.example).

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "ledgerwise_db"),
        "USER": os.getenv("DB_USER", "ledgerwise_user"),
        "PASSWORD": require_env("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# Celery settings for local dev (using Redis)
CELERY_BROKER_URL = os.getenv("REDIS_URL_local", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("REDIS_URL_local", "redis://localhost:6379/0")
