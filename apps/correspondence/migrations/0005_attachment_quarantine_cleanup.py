import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def reject_reverse_cleanup_state(apps, schema_editor):
    Cleanup = apps.get_model(
        "correspondence",
        "CorrespondenceAttachmentQuarantineCleanup",
    )
    cleanup_ids = ", ".join(
        str(value)
        for value in Cleanup.objects.order_by("pk").values_list(
            "pk",
            flat=True,
        )[:10]
    )
    if cleanup_ids:
        raise RuntimeError(
            "Cannot reverse correspondence migration 0005 because doing so "
            "would discard durable quarantine deletion work and attempt "
            "evidence. Drain or preserve the cleanup ledger and roll forward "
            f"instead. Cleanup IDs: {cleanup_ids}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0004_inbound_receipt_contract"),
    ]

    operations = [
        migrations.CreateModel(
            name="CorrespondenceAttachmentQuarantineCleanup",
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
                    "source_storage_key",
                    models.CharField(
                        editable=False,
                        max_length=1000,
                        unique=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "next_attempt_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        editable=False,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                (
                    "last_attempt_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "lease_token",
                    models.UUIDField(blank=True, editable=False, null=True),
                ),
                (
                    "lease_expires_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                    ),
                ),
                (
                    "completed_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "attachment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="quarantine_cleanup",
                        to="correspondence.correspondenceattachment",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        condition=models.Q(
                            ("completed_at__isnull", True)
                        ),
                        fields=[
                            "next_attempt_at",
                            "created_at",
                            "id",
                        ],
                        name="corr_cleanup_pending_due_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            source_storage_key__startswith=(
                                "quarantine/correspondence/attachments/"
                            )
                        ),
                        name="correspondence_cleanup_key_namespace",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("attempt_count", 0),
                                ("completed_at__isnull", True),
                                ("last_attempt_at__isnull", True),
                            )
                            | models.Q(
                                ("attempt_count__gt", 0),
                                ("last_attempt_at__isnull", False),
                            )
                        ),
                        name="correspondence_cleanup_attempt_evidence",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("completed_at__isnull", True))
                            | models.Q(
                                (
                                    "completed_at__gte",
                                    models.F("last_attempt_at"),
                                )
                            )
                        ),
                        name="correspondence_cleanup_completion_chronology",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("lease_expires_at__isnull", True),
                                ("lease_token__isnull", True),
                            )
                            | models.Q(
                                ("lease_expires_at__isnull", False),
                                ("lease_token__isnull", False),
                            )
                        ),
                        name="correspondence_cleanup_lease_complete",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("completed_at__isnull", True))
                            | models.Q(
                                ("lease_expires_at__isnull", True),
                                ("lease_token__isnull", True),
                            )
                        ),
                        name="correspondence_cleanup_completed_unleased",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reject_reverse_cleanup_state,
        ),
    ]
