from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.configuration import runtime
from apps.records.service_identities import is_exact_mcp_identity

from .identity import (
    WORKFLOW_STAFF_ROLES,
    GoogleDirectoryUnavailable,
    refresh_google_roles,
    staff_roles,
)
from .models import StaffIdentity


class StaffAuthorizationUnavailable(APIException):
    status_code = 503
    default_detail = "Staff authorization could not be verified."
    default_code = "staff_authorization_unavailable"


def _refresh_google_authorization_if_due(user):
    identity = getattr(user, "staff_identity", None)
    if not identity or identity.source != StaffIdentity.Source.GOOGLE:
        return
    last_check = identity.last_group_check_at
    age = (
        (timezone.now() - last_check).total_seconds()
        if last_check
        else float("inf")
    )
    if age < runtime.get("PRR_GOOGLE_GROUP_RECHECK_SECONDS"):
        return
    try:
        refresh_google_roles(user)
    except GoogleDirectoryUnavailable as exc:
        if age >= runtime.get("PRR_GOOGLE_GROUP_MAX_STALE_SECONDS"):
            raise StaffAuthorizationUnavailable() from exc


class IsStaffOrReadOnlyAgent(BasePermission):
    """Role-aware staff access; explicitly scoped service tokens stay read-only."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if user.is_staff:
            # AuthenticationMiddleware sees session users, while DRF token
            # authentication happens later. Recheck here as well so an
            # attributed staff token cannot outlive Google group removal.
            _refresh_google_authorization_if_due(user)
            roles = staff_roles(user)
            action = getattr(view, "action", None)
            if action is None:
                # Routers leave action unset for unsupported methods. Let DRF
                # return the accurate 405 without weakening any real action.
                return bool(roles)
            action_matrix = getattr(view, "role_permissions", None)
            if action_matrix is not None:
                # An action matrix is an allowlist: a newly added action must be
                # assigned deliberately instead of inheriting broad access.
                allowed_roles = action_matrix.get(action, set())
            else:
                allowed_roles = getattr(
                    view,
                    "allowed_roles",
                    WORKFLOW_STAFF_ROLES,
                )
            return bool(roles.intersection(allowed_roles))
        exact_mcp_identity = is_exact_mcp_identity(user)
        if (
            exact_mcp_identity
            and getattr(view, "action", None)
            in getattr(view, "human_only_handoff_actions", set())
        ):
            return True
        return bool(
            request.method in SAFE_METHODS
            and exact_mcp_identity
            and getattr(view, "allow_readonly_agent", True)
        )
