"""Fail-closed validation for destructive synthetic performance fixtures."""

from urllib.parse import ParseResult, urlparse

from django.core.exceptions import ImproperlyConfigured

DISPOSABLE_SENTINEL = "casework-performance-disposable"
_DISPOSABLE_IDENTITIES = {
    ("prr_ci", "prr_ci"),
    ("prr_contract", "prr_contract"),
}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_disposable_database(url: str, *, sentinel: str) -> ParseResult:
    """Allow fixture writes only to explicitly named loopback databases."""
    parsed = urlparse(url)
    identity = (parsed.path.lstrip("/"), parsed.username or "")
    if (
        sentinel != DISPOSABLE_SENTINEL
        or parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or identity not in _DISPOSABLE_IDENTITIES
    ):
        raise ImproperlyConfigured(
            "casework performance fixtures require the explicit disposable "
            "sentinel and an allowlisted loopback database/user pair"
        )
    return parsed
