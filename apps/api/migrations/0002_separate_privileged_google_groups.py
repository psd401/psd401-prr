from django.db import migrations, models
from django.db.models.functions import Lower


PRIVILEGED_ROLES = frozenset({"admin", "records_officer"})


def separate_existing_privileged_groups(apps, schema_editor):
    Mapping = apps.get_model("api", "GoogleGroupRoleMapping")
    StaffIdentity = apps.get_model("api", "StaffIdentity")
    Group = apps.get_model("auth", "Group")
    Session = apps.get_model("sessions", "Session")
    AuditLog = apps.get_model("audit", "AuditLog")
    retained_by_group = {}
    deactivated_roles = set()
    mappings = Mapping.objects.filter(
        active=True,
        role__in=PRIVILEGED_ROLES,
    ).order_by("google_group", "role", "pk")
    for mapping in mappings.iterator():
        normalized_group = mapping.google_group.strip().lower()
        retained = retained_by_group.get(normalized_group)
        if retained is None:
            retained_by_group[normalized_group] = mapping
            continue
        # Prefer retaining Application Admin when an existing group carries
        # both powers. Deactivating the legal role fails closed while leaving
        # configuration access available for an independently authorized
        # Records Officer to create a distinct mapping.
        if mapping.role == "admin" and retained.role != "admin":
            deactivate = retained
            retained_by_group[normalized_group] = mapping
        else:
            deactivate = mapping
        Mapping.objects.filter(pk=deactivate.pk).update(active=False)
        deactivated_roles.add(deactivate.role)
        AuditLog.objects.create(
            action="identity.privileged_mapping_deactivated",
            target=str(deactivate.pk),
            detail={
                "role": deactivate.role,
                "google_group": deactivate.google_group,
                "reason": (
                    "Secure migration separated Application Admin and "
                    "Records Officer Workspace groups."
                ),
            },
        )
    if not deactivated_roles:
        return

    # Role mappings are the source of truth, but Google authorization is
    # materialized into Django groups and cached by StaffIdentity. Revoke every
    # deactivated privileged role from Google principals and expire the cache
    # in the same migration so an already-elevated legacy session cannot keep
    # using legal authority after its source mapping has been disabled.
    google_user_ids = list(
        StaffIdentity.objects.filter(source="google").values_list(
            "user_id",
            flat=True,
        )
    )
    managed_group_ids = list(
        Group.objects.filter(
            name__in=[
                f"prr-role:{role}"
                for role in sorted(deactivated_roles)
            ]
        ).values_list("pk", flat=True)
    )
    Membership = Group.user_set.through
    memberships_revoked, _ = Membership.objects.filter(
        user_id__in=google_user_ids,
        group_id__in=managed_group_ids,
    ).delete()
    identities_expired = StaffIdentity.objects.filter(source="google").update(
        last_group_check_at=None,
        last_group_error="",
    )
    # Historical Session rows cannot be decoded through the migration-state
    # model. A one-time global logout is the fail-closed upgrade boundary.
    sessions_revoked, _ = Session.objects.all().delete()
    AuditLog.objects.create(
        action="identity.privileged_authorizations_expired",
        target="api.0002",
        detail={
            "roles": sorted(deactivated_roles),
            "google_identities_expired": identities_expired,
            "managed_memberships_revoked": memberships_revoked,
            "sessions_revoked": sessions_revoked,
            "reason": (
                "Secure migration revoked authorization materialized from "
                "deactivated privileged Workspace group mappings."
            ),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_staff_identity"),
        ("audit", "0001_initial"),
        ("sessions", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            separate_existing_privileged_groups,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="googlegrouprolemapping",
            constraint=models.UniqueConstraint(
                Lower("google_group"),
                condition=models.Q(
                    active=True,
                    role__in=["admin", "records_officer"],
                ),
                name="api_unique_active_privileged_google_group",
            ),
        ),
    ]
