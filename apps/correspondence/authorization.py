"""Transactional authorization helpers for human correspondence actions."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from apps.api.identity import WORKFLOW_STAFF_ROLES, has_staff_role


def require_current_human_staff(
    actor,
    *,
    roles=(),
    message: str,
):
    """Reload and lock the staff identity and its current role memberships."""
    required_roles = tuple(roles) or tuple(WORKFLOW_STAFF_ROLES)
    actor_id = getattr(actor, "pk", None)
    if actor_id is None:
        raise PermissionDenied(message)
    user_model = get_user_model()
    try:
        current_actor = user_model.objects.select_for_update(of=("self",)).get(
            pk=actor_id
        )
    except user_model.DoesNotExist as exc:
        raise PermissionDenied(message) from exc

    membership_model = current_actor.groups.through
    list(
        membership_model.objects.select_for_update()
        .filter(user_id=current_actor.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if (
        not getattr(current_actor, "is_authenticated", False)
        or not current_actor.is_active
        or not current_actor.is_staff
        or not has_staff_role(current_actor, *required_roles)
    ):
        raise PermissionDenied(message)
    return current_actor
