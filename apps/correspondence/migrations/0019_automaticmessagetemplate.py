import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0018_alter_correspondence_kind"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomaticMessageTemplate",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("acknowledgment", "Five-day acknowledgment"),
                            ("submission_receipt", "Submission receipt"),
                        ],
                        max_length=40,
                    ),
                ),
                ("template_version", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("active", "Active"),
                            ("retired", "Retired"),
                        ],
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("subject_template", models.CharField(max_length=998)),
                ("body_template", models.TextField()),
                ("change_reason", models.CharField(max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "activated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="activated_automatic_message_templates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_automatic_message_templates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["kind", "-template_version", "-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="automaticmessagetemplate",
            constraint=models.UniqueConstraint(
                fields=("kind", "template_version"),
                name="automatic_template_kind_version_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="automaticmessagetemplate",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")),
                fields=("kind",),
                name="automatic_template_one_active_per_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="automaticmessagetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("template_version__gte", 1)),
                name="automatic_template_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="automaticmessagetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)),
                name="automatic_template_revision_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="automaticmessagetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("activated_at__isnull", True),
                        ("activated_by__isnull", True),
                        ("state", "draft"),
                    ),
                    models.Q(
                        ("activated_at__isnull", False),
                        ("activated_by__isnull", False),
                        ("state__in", ["active", "retired"]),
                    ),
                    _connector="OR",
                ),
                name="automatic_template_activation_evidence",
            ),
        ),
    ]
