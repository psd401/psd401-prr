"""Disposable PostgreSQL settings for deterministic performance evidence."""

import os

from apps.correspondence.performance_safety import validate_disposable_database

from .postgres_test_settings import *  # noqa: F403

validate_disposable_database(
    os.environ.get("PRR_POSTGRES_TEST_DATABASE_URL", ""),
    sentinel=os.environ.get("PRR_CASEWORK_PERFORMANCE_DISPOSABLE", ""),
)

PRR_TEST_FIXTURE_SEED_ENABLED = True
ALLOWED_HOSTS = ["testserver"]
