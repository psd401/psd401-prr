from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from apps.api.identity import has_staff_role


def lock_authorized_staff(users_with_roles, *, message):
    """Lock current user and managed-role rows before authorizing workflow."""
    requested = {}
    for user, roles in users_with_roles:
        if getattr(user, "pk", None) is None:
            raise PermissionDenied(message)
        requested.setdefault(int(user.pk), []).append(frozenset(roles))
    user_model = get_user_model()
    current = {
        user.pk: user
        for user in user_model.objects.select_for_update(of=("self",))
        .filter(pk__in=sorted(requested))
        .order_by("pk")
    }
    if set(current) != set(requested):
        raise PermissionDenied(message)
    membership_model = user_model.groups.through
    list(
        membership_model.objects.select_for_update()
        .filter(user_id__in=sorted(requested))
        .order_by("user_id", "pk")
        .values_list("pk", flat=True)
    )
    for user_id, role_requirements in requested.items():
        user = current[user_id]
        if (
            not getattr(user, "is_authenticated", False)
            or not user.is_active
            or not user.is_staff
            or any(
                not has_staff_role(user, *roles)
                for roles in role_requirements
            )
        ):
            raise PermissionDenied(message)
    return current
