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
        # The Review tab renders previews and prefetches the next record while
        # a decision is written. SQLite refuses a read-to-write lock upgrade
        # at once rather than waiting, so transactions start IMMEDIATE (they
        # take the write lock up front and queue on the timeout instead).
        "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
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

# Trust ladder for the sampled-batch flows (#39): non-responsive may be batched
# and auto-staged; responsive stays per-record. Only PRR-E2E-0011 carries AI
# suggestions, so no other case's Records tab gains a Ready-to-discard pile.
PRR_TRUST_NON_RESPONSIVE_MODE = "auto"
PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE = 90
PRR_TRUST_BATCH_SAMPLE_SIZE = 2
PRR_TRUST_ELUSION_SAMPLE_SIZE = 3
# Redaction proposals may be accepted a group at a time (#40).
PRR_TRUST_REDACTION_MODE = "batch"

# A local stand-in for the private bucket, so acceptance flows can open,
# redact, and burn in real PDFs. apps.secure_files honours it only while the
# test-fixture switch above is on; no deployed setting names it.
PRR_E2E_FILESYSTEM_OBJECT_STORE = str(BASE_DIR / ".verification" / "e2e-objects")  # noqa: F405

# A deterministic stand-in for Google Vault (apps.integrations.vault.client),
# honoured only with the test-fixture switch above, so acceptance flows can
# count, approve and collect, and re-scope. Synthetic identifiers only.
PRR_E2E_FAKE_VAULT = True
PRR_VAULT_ADMIN_USER = "vault-admin@psd401.net"
PRR_VAULT_IMPERSONATION_TARGET = "prr-vault-e2e@psd401-prr-identity.iam.gserviceaccount.com"
PRR_GOOGLE_HOSTED_DOMAIN = "psd401.net"

