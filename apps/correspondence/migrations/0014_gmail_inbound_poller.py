from django.conf import settings
from django.contrib.auth.hashers import is_password_usable, make_password
from django.db import migrations, models
import uuid


SYSTEM_INBOUND_TRANSPORT_USERNAME = "prr-gmail-inbound-transport"


def create_system_inbound_transport(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Token = apps.get_model("authtoken", "Token")
    database = schema_editor.connection.alias
    content_type, _created = ContentType.objects.using(database).get_or_create(
        app_label="correspondence",
        model="inboundcorrespondencereceipt",
    )
    permission, _created = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename="submit_inbound_correspondence",
        defaults={"name": "Can submit inbound correspondence messages"},
    )
    user, created = User.objects.using(database).get_or_create(
        username=SYSTEM_INBOUND_TRANSPORT_USERNAME,
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
            or existing_permissions
            - {("correspondence", "submit_inbound_correspondence")}
        ):
            raise RuntimeError(
                "Refusing to repurpose an unsafe system inbound transport account."
            )
        user.password = make_password(None)
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.save(
            update_fields=["password", "is_active", "is_staff", "is_superuser"]
        )
    user.user_permissions.set([permission])


class Migration(migrations.Migration):
    dependencies = [
        ("authtoken", "0004_alter_tokenproxy_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("correspondence", "0013_system_outbound_transport"),
    ]

    operations = [
        migrations.CreateModel(
            name="GmailSyncState",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("mailbox", models.EmailField(max_length=100)),
                ("environment", models.CharField(max_length=64)),
                ("history_id", models.CharField(blank=True, max_length=255)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["mailbox", "environment"]},
        ),
        migrations.AddConstraint(
            model_name="gmailsyncstate",
            constraint=models.UniqueConstraint(
                fields=("mailbox", "environment"),
                name="correspondence_gmail_sync_mailbox_environment_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="gmailsyncstate",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("history_id", ""), _connector="OR")
                    | models.Q(("history_id__regex", "\\A[0-9]+\\Z"))
                ),
                name="correspondence_gmail_sync_history_id_valid",
            ),
        ),
        migrations.RunPython(
            create_system_inbound_transport,
            migrations.RunPython.noop,
        ),
    ]
