import os

os.environ["DJANGO_DEBUG"] = "true"

from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CELERY_TASK_ALWAYS_EAGER = True
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "prr-tests",
    },
}
# Django's default PBKDF2 runs 1.2M iterations, which costs roughly 0.13s per
# hash. The suite creates on the order of a thousand users across its setUp
# methods, so the default spends minutes per full run proving nothing: no test
# asserts on stored hash format, and production keeps the real hasher.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
# Phase C settings no longer have a Django settings attribute in
# config/settings.py, so the suite states the development values it assumes
# here. The store's bootstrap resolves a key with no stored row through
# Django settings first, which is also what keeps `override_settings` working
# for these keys in tests that need a different sign-in world.
PRR_ENABLE_ADMIN = True
PRR_AUTH_MODE = "local"
PRR_CORRESPONDENCE_OUTBOUND_ENABLED = True
PRR_TEST_EAGER_SOURCE_WORK_RECONCILIATION = False

# The settings store caches resolved values in the Django cache so a change
# propagates to every worker without a restart. LocMemCache lives for the
# whole test process, but `TestCase` rolls the database back between tests --
# so a test that stored a setting would leave its value cached for whatever
# test ran next, and the failure would land in an unrelated file.
#
# Zero here means every read in the suite goes to the store, which is the
# behaviour a test almost always wants to assert. The two tests that are
# *about* the cache raise the TTL themselves.
import apps.configuration.runtime as _configuration_runtime  # noqa: E402

_configuration_runtime.CACHE_TTL_SECONDS = 0
