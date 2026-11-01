"""PostgreSQL-only settings for real row-lock and uniqueness race tests."""

import os
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from .test_settings import *  # noqa: F403

_test_database_url = os.environ.get("PRR_POSTGRES_TEST_DATABASE_URL", "")
_test_database = urlparse(_test_database_url)
if (
    _test_database.scheme not in {"postgres", "postgresql"}
    or not _test_database.path.lstrip("/")
    or not _test_database.username
    or not _test_database.hostname
):
    raise ImproperlyConfigured(
        "PRR_POSTGRES_TEST_DATABASE_URL must identify an explicit PostgreSQL "
        "test database."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _test_database.path.lstrip("/"),
        "USER": _test_database.username,
        "PASSWORD": _test_database.password or "",
        "HOST": _test_database.hostname,
        "PORT": _test_database.port or 5432,
    }
}
