"""Runtime enforcement for actions the capability catalog marks human-only."""

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.response import Response


def human_only_handoff(
    request,
    *,
    capability_id: str,
    action: str,
    required_role: str = "records_officer",
    detail: str | None = None,
):
    """Return a structured handoff for token callers, otherwise ``None``.

    Production humans reach these actions through CSRF-protected staff
    sessions. Attributed staff tokens remain useful for drafting and preview,
    but cannot turn a human-only catalog entry into an automation shortcut.
    """
    if not isinstance(request.successful_authenticator, TokenAuthentication):
        return None
    return Response(
        {
            "code": "human_approval_required",
            "detail": detail or (
                "This legal action requires the accountable human to execute "
                "it in the staff application."
            ),
            "handoff": {
                "capability_id": capability_id,
                "action": action,
                "required_role": required_role,
            },
        },
        status=status.HTTP_403_FORBIDDEN,
    )
