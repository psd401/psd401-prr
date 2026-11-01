"""Credential-free local settings for deterministic Playwright flows."""

import os

os.environ["DJANGO_DEBUG"] = "true"
os.environ["PRR_AUTH_MODE"] = "local"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from .test_settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / ".verification" / "e2e.sqlite3",  # noqa: F405
    }
}
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1:4173"]
PRR_AUTH_MODE = "local"
PRR_LOCAL_IMPLICIT_STAFF_ROLES = ()
PRR_TEST_FIXTURE_SEED_ENABLED = True
# Multi-role acceptance flows deliberately switch among five deterministic
# identities in one browser run; keep production's login throttle unchanged.
PRR_STAFF_LOGIN_RATE = "100/min"
