import hashlib
import logging
import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle

from apps.configuration import runtime

logger = logging.getLogger(__name__)


def public_intake_client_ip(request):
    return BaseThrottle().get_ident(request)


class _AtomicFixedWindowThrottle(SimpleRateThrottle):
    """A shared-cache hard ceiling rather than DRF's intentionally fuzzy list."""

    rejection_reason = "rate_limit"
    timer = time.time

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        identity = self.get_cache_key(request, view)
        if identity is None:
            return True

        now = self.timer()
        window = int(now // self.duration)
        digest = hashlib.sha256(identity.encode()).hexdigest()
        key = f"throttle_{self.scope}_{digest}_{window}"
        self._wait = max(1, ((window + 1) * self.duration) - now)
        timeout = int(self._wait) + 1

        if cache.add(key, 1, timeout=timeout):
            count = 1
        else:
            try:
                count = cache.incr(key)
            except ValueError:
                count = 1 if cache.add(key, 1, timeout=timeout) else cache.incr(key)

        if count <= self.num_requests:
            return True
        logger.warning("Request throttled: %s", self.rejection_reason)
        return False

    def wait(self):
        return self._wait


class PublicIntakePerIpThrottle(_AtomicFixedWindowThrottle):
    scope = "public_intake_per_ip"
    rejection_reason = "per_ip_rate_limit"

    def get_rate(self):
        return runtime.get("PRR_PUBLIC_INTAKE_PER_IP_RATE")

    def get_cache_key(self, request, view):
        return self.get_ident(request)


class PublicIntakeGlobalThrottle(_AtomicFixedWindowThrottle):
    scope = "public_intake_global"
    rejection_reason = "global_rate_limit"

    def get_rate(self):
        return runtime.get("PRR_PUBLIC_INTAKE_GLOBAL_RATE")

    def get_cache_key(self, request, view):
        return "all-public-intake"


class InstallmentDownloadPerIpThrottle(_AtomicFixedWindowThrottle):
    scope = "installment_download_per_ip"
    rejection_reason = "installment_download_per_ip_rate_limit"

    def get_rate(self):
        return runtime.get("PRR_INSTALLMENT_DOWNLOAD_PER_IP_RATE")

    def get_cache_key(self, request, view):
        return self.get_ident(request)


class InstallmentAccessCodePerIpThrottle(_AtomicFixedWindowThrottle):
    """A hard ceiling on one address guessing installment access codes.

    The package view reads a code from a POST and from nothing else, so
    only a POST counts; reading the page or fetching an uncoded package is
    governed by the download throttle.
    """

    scope = "installment_access_code_per_ip"
    rejection_reason = "installment_access_code_per_ip_rate_limit"

    def get_rate(self):
        return runtime.get("PRR_INSTALLMENT_ACCESS_CODE_PER_IP_RATE")

    def get_cache_key(self, request, view):
        if request.method != "POST":
            return None
        return self.get_ident(request)


class InstallmentAccessCodePerInstallmentThrottle(_AtomicFixedWindowThrottle):
    """A hard ceiling on guessing one installment's code from anywhere.

    The per-address ceiling means nothing to a guesser with a pool of
    addresses. This one is keyed on the link itself, so the total number of
    attempts against a leaked link is bounded no matter where they come from.
    """

    scope = "installment_access_code_per_installment"
    rejection_reason = "installment_access_code_per_installment_rate_limit"

    def get_rate(self):
        return runtime.get("PRR_INSTALLMENT_ACCESS_CODE_PER_INSTALLMENT_RATE")

    def get_cache_key(self, request, view):
        if request.method != "POST":
            return None
        return f"installment:{view.kwargs.get('token', '')}"
