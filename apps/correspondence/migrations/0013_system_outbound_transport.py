from django.conf import settings
from django.contrib.auth.hashers import is_password_usable, make_password
from django.db import migrations


SYSTEM_TRANSPORT_USERNAME = "prr-gmail-outbound-transport"


def create_system_outbound_transport(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Token = apps.get_model("authtoken", "Token")
    database = schema_editor.connection.alias
    content_type, _created = ContentType.objects.using(database).get_or_create(
        app_label="correspondence",
        model="correspondencedelivery",
    )
    permission, _created = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename="deliver_outbound_correspondence",
        defaults={
            "name": "Can claim and report outbound correspondence deliveries",
        },
    )
    user, created = User.objects.using(database).get_or_create(
        username=SYSTEM_TRANSPORT_USERNAME,
        defaults={
            "password": make_password(None),
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
        },
    )
    if not created:
        existing_permissions = set(
            user.user_permissions.values_list(
                "content_type__app_label",
                "codename",
            )
        )
        if (
            user.is_staff
            or user.is_superuser
            or is_password_usable(user.password)
            or user.groups.exists()
            or Token.objects.using(database).filter(user_id=user.pk).exists()
            or existing_permissions - {
                ("correspondence", "deliver_outbound_correspondence")
            }
        ):
            raise RuntimeError(
                "Refusing to repurpose an unsafe system transport account."
            )
        user.password = make_password(None)
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.save(
            update_fields=[
                "password",
                "is_active",
                "is_staff",
                "is_superuser",
            ]
        )
    user.user_permissions.set([permission])


class Migration(migrations.Migration):
    dependencies = [
        ("authtoken", "0004_alter_tokenproxy_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("correspondence", "0012_resulting_status_event_integrity"),
    ]

    operations = [
        migrations.RunPython(
            create_system_outbound_transport,
            migrations.RunPython.noop,
        ),
    ]
