# ABOUTME: Canonical role scope for staff resources that belong to a PRA case.
# ABOUTME: Liaison access follows assigned collection or correspondence-draft work.
from django.db.models import Q

from apps.api.identity import StaffRole, has_staff_role
from apps.work_queue.models import WorkItem

LIAISON_CASE_WORK_KINDS = (
    WorkItem.WorkKind.COLLECTION,
    WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
)


def scope_case_queryset(queryset, user, *, request_prefix=""):
    """Limit liaison-only users to cases represented by their assigned work."""
    if not has_staff_role(user, StaffRole.LIAISON) or has_staff_role(
        user,
        StaffRole.RECORDS_OFFICER,
    ):
        return queryset

    prefix = f"{request_prefix}__" if request_prefix else ""
    return queryset.filter(
        Q(**{f"{prefix}collection_tasks__assigned_to": user})
        | Q(
            **{
                f"{prefix}work_items__assignee": user,
                f"{prefix}work_items__work_kind__in": LIAISON_CASE_WORK_KINDS,
            }
        )
    ).distinct()
