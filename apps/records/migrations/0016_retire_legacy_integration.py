from django.contrib.auth.hashers import is_password_usable
from django.db import migrations


SYSTEM_TRANSPORT_USERNAMES = {
    "prr-gmail-outbound-transport",
    "prr-gmail-inbound-transport",
}
RETIRED_PERMISSIONS = {
    ("records", "submit_integrated_request"),
    ("correspondence", "deliver_outbound_correspondence"),
    ("correspondence", "submit_inbound_correspondence"),
}


def retire_exact_scope_tokens(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Token = apps.get_model("authtoken", "Token")
    User = apps.get_model("auth", "User")

    permissions = list(
        Permission.objects.filter(
            content_type__app_label__in={
                app_label for app_label, _ in RETIRED_PERMISSIONS
            },
            codename__in={codename for _, codename in RETIRED_PERMISSIONS},
        ).select_related("content_type")
    )
    retired_permission_keys = {
        (permission.content_type.app_label, permission.codename)
        for permission in permissions
        if (permission.content_type.app_label, permission.codename)
        in RETIRED_PERMISSIONS
    }
    permission_ids = [
        permission.pk
        for permission in permissions
        if (permission.content_type.app_label, permission.codename)
        in retired_permission_keys
    ]

    users = User.objects.filter(
        is_staff=False,
        is_superuser=False,
        user_permissions__in=permission_ids,
    ).distinct()
    for user in users.iterator():
        if user.username in SYSTEM_TRANSPORT_USERNAMES:
            continue
        direct_permissions = set(
            user.user_permissions.values_list(
                "content_type__app_label",
                "codename",
            )
        )
        if (
            len(direct_permissions) != 1
            or not direct_permissions.issubset(retired_permission_keys)
            or user.groups.exists()
            or is_password_usable(user.password)
        ):
            continue
        Token.objects.filter(user_id=user.pk).delete()
        user.user_permissions.clear()
        user.is_active = False
        user.save(update_fields=["is_active"])

    Permission.objects.filter(
        content_type__app_label="records",
        codename="submit_integrated_request",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authtoken", "0004_alter_tokenproxy_options"),
        ("records", "0015_publicintakesubmission"),
    ]

    operations = [
        migrations.RunPython(
            retire_exact_scope_tokens,
            migrations.RunPython.noop,
        ),
        migrations.DeleteModel(
            name="IntegratedIntakeSubmission",
        ),
        migrations.AlterModelOptions(
            name="publicrecordsrequest",
            options={
                "ordering": ["-received_at"],
                "permissions": [
                    (
                        "read_staff_api",
                        "Can read the staff API through a service integration",
                    )
                ],
            },
        ),
    ]
