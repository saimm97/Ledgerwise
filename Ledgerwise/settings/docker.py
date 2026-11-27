# Ledgerwise/settings/docker.py
import os

from .base import *  # noqa: F403
from .base import require_env

# DEBUG, SECRET_KEY, ALLOWED_HOSTS and the HTTPS options all come from base.py (environment variables).

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "ledgerwise_db"),
        "USER": os.getenv("POSTGRES_USER", "ledgerwise_user"),
        "PASSWORD": require_env("POSTGRES_PASSWORD"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

# Celery for Docker
CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Same Redis as the default cache, so the login throttle is shared between workers.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": CELERY_BROKER_URL}}
