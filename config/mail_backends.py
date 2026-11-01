# ABOUTME: Fail-closed mail backends for deployment modes without Django email.
# ABOUTME: Production application mail must use the explicitly gated Gmail paths.
from django.core.mail.backends.base import BaseEmailBackend


class ProductionDenyEmailBackend(BaseEmailBackend):
    """Reject generic Django email instead of silently discarding it."""

    def send_messages(self, email_messages):
        messages = list(email_messages or ())
        if not messages:
            return 0
        raise RuntimeError(
            "Generic Django email is disabled in production; "
            "use the gated Gmail transports."
        )
