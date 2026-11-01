# ABOUTME: The password backend, always installed and always gated on the
# ABOUTME: settings store rather than on what the process started with.
"""A break-glass door has to be openable during the incident.

``AUTHENTICATION_BACKENDS`` used to gain ``ModelBackend`` only when the
process started in local sign-in mode or with break-glass already enabled.
That is the wrong moment to decide: the break-glass path exists precisely
for the hour when Google sign-in is broken, and opening it meant editing the
host's environment and restarting — the operation moving settings into the
application was meant to remove.

So the backend is always installed and asks the settings store on every
attempt. It answers a password only when local sign-in is the mode, or when
an Application Admin has deliberately turned break-glass on with a reason on
the record. Otherwise it declines, exactly as an absent backend would, and
Django falls through to the OIDC backend.

Two details are load-bearing:

* it declines rather than raising, so a settings store that cannot be read
  fails closed into "no password sign-in" instead of into an error page that
  reveals the store is down;
* ``user_can_authenticate`` is inherited unchanged, so an inactive account is
  still refused.
"""

import logging

from django.contrib.auth.backends import ModelBackend

logger = logging.getLogger(__name__)


def password_sign_in_allowed() -> bool:
    """Whether a district password may be exchanged for a session right now."""
    from apps.configuration import runtime

    try:
        if str(runtime.get("PRR_AUTH_MODE")).lower() == "local":
            return True
        return bool(runtime.get("PRR_BREAK_GLASS_PASSWORD_AUTH"))
    except Exception:  # an unreadable store closes the door
        logger.exception("Password sign-in gate could not be read; refusing.")
        return False


class GatedPasswordBackend(ModelBackend):
    """``ModelBackend``, but only while the application says passwords count."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not password_sign_in_allowed():
            return None
        return super().authenticate(
            request, username=username, password=password, **kwargs
        )
