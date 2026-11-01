import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def reject_reverse_reservation_state(apps, schema_editor):
    Reservation = apps.get_model(
        "correspondence",
        "InboundQuarantineWriteReservation",
    )
    reservation_ids = ", ".join(
        str(value)
        for value in Reservation.objects.order_by("pk").values_list(
            "pk",
            flat=True,
        )[:10]
    )
    Alias = apps.get_model(
        "correspondence",
        "InboundQuarantineTransportAlias",
    )
    alias_ids = ", ".join(
        str(value)
        for value in Alias.objects.order_by("pk").values_list(
            "pk",
            flat=True,
        )[:10]
    )
    if reservation_ids or alias_ids:
        raise RuntimeError(
            "Cannot reverse correspondence migration 0006 because doing so "
            "would discard durable pre-upload quarantine reservation, "
            "adoption, cleanup, or attempt evidence. Drain or preserve the "
            "reservation ledger and roll forward instead. Reservation IDs: "
            f"{reservation_ids or 'none'}; transport alias IDs: "
            f"{alias_ids or 'none'}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0005_attachment_quarantine_cleanup"),
    ]

    operations = [
        migrations.CreateModel(
            name="InboundQuarantineWriteReservation",
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
                    "logical_identity",
                    models.CharField(
                        editable=False,
                        max_length=64,
                    ),
                ),
                (
                    "generation",
                    models.PositiveIntegerField(default=1, editable=False),
                ),
                (
                    "transport_idempotency_identity",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "transport_provider_identity",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("raw_message", "Raw message"),
                            ("attachment", "Attachment"),
                        ],
                        editable=False,
                        max_length=20,
                    ),
                ),
                ("size", models.PositiveBigIntegerField(editable=False)),
                (
                    "sha256",
                    models.CharField(editable=False, max_length=64),
                ),
                (
                    "storage_key",
                    models.CharField(
                        editable=False,
                        max_length=1000,
                        unique=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "stored_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "adopted_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "processing_expires_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                    ),
                ),
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
                    "adopted_attachment",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="quarantine_write_reservation",
                        to="correspondence.correspondenceattachment",
                    ),
                ),
                (
                    "adopted_receipt",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="raw_quarantine_reservation",
                        to="correspondence.inboundcorrespondencereceipt",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        condition=models.Q(
                            ("adopted_at__isnull", True),
                            ("completed_at__isnull", True),
                        ),
                        fields=[
                            "next_attempt_at",
                            "created_at",
                            "id",
                        ],
                        name="corr_in_resv_pending_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("logical_identity", "generation"),
                        name="corr_in_resv_identity_generation_unique",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("completed_at__isnull", True)),
                        fields=("logical_identity",),
                        name="corr_in_resv_one_active_identity",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("completed_at__isnull", True),
                            (
                                "transport_idempotency_identity__isnull",
                                False,
                            ),
                        ),
                        fields=("transport_idempotency_identity",),
                        name="corr_in_resv_one_active_idempotency",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("completed_at__isnull", True),
                            (
                                "transport_provider_identity__isnull",
                                False,
                            ),
                        ),
                        fields=("transport_provider_identity",),
                        name="corr_in_resv_one_active_provider",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                (
                                    "logical_identity__regex",
                                    r"\A[0-9a-f]{64}\Z",
                                ),
                                ("sha256__regex", r"\A[0-9a-f]{64}\Z"),
                                ("generation__gte", 1),
                            )
                            & (
                                models.Q(
                                    ("kind", "raw_message"),
                                    ("size__gte", 1),
                                )
                                | models.Q(
                                    ("kind", "attachment"),
                                    ("size__gte", 0),
                                )
                            )
                        ),
                        name="corr_in_resv_evidence_valid",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("kind", "raw_message"),
                                (
                                    "storage_key__startswith",
                                    "quarantine/correspondence/raw/",
                                ),
                            )
                            | models.Q(
                                ("kind", "attachment"),
                                (
                                    "storage_key__startswith",
                                    "quarantine/correspondence/attachments/",
                                ),
                            )
                        ),
                        name="corr_in_resv_key_namespace",
                    ),
                    models.CheckConstraint(
                        condition=(
                            (
                                models.Q(("kind", "raw_message"))
                                & models.Q(
                                    (
                                        "transport_idempotency_identity__isnull",
                                        False,
                                    )
                                )
                                & models.Q(
                                    (
                                        "transport_provider_identity__isnull",
                                        False,
                                    )
                                )
                            )
                            | (
                                models.Q(("kind", "attachment"))
                                & models.Q(
                                    (
                                        "transport_idempotency_identity__isnull",
                                        True,
                                    )
                                )
                                & models.Q(
                                    (
                                        "transport_provider_identity__isnull",
                                        True,
                                    )
                                )
                            )
                        ),
                        name="corr_in_resv_transport_identity_exact",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("adopted_at__isnull", True),
                                ("adopted_attachment__isnull", True),
                                ("adopted_receipt__isnull", True),
                            )
                            | models.Q(
                                ("adopted_at__isnull", False),
                                ("adopted_attachment__isnull", True),
                                ("adopted_receipt__isnull", False),
                                ("kind", "raw_message"),
                            )
                            | models.Q(
                                ("adopted_at__isnull", False),
                                ("adopted_attachment__isnull", False),
                                ("adopted_receipt__isnull", True),
                                ("kind", "attachment"),
                            )
                        ),
                        name="corr_in_resv_adoption_exact",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("stored_at__isnull", True))
                            | models.Q(
                                (
                                    "stored_at__gte",
                                    models.F("created_at"),
                                )
                            )
                        ),
                        name="corr_in_resv_storage_chronology",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("processing_expires_at__isnull", True)
                            )
                            | models.Q(
                                (
                                    "processing_expires_at__gte",
                                    models.F("created_at"),
                                )
                            )
                        ),
                        name="corr_in_resv_processing_chronology",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("adopted_at__isnull", True),
                                ("completed_at__isnull", True),
                            )
                            | models.Q(
                                ("processing_expires_at__isnull", True)
                            )
                        ),
                        name="corr_in_resv_terminal_unprocessed",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("adopted_at__isnull", True))
                            | (
                                models.Q(
                                    (
                                        "adopted_at__gte",
                                        models.F("created_at"),
                                    )
                                )
                                & (
                                    models.Q(("stored_at__isnull", True))
                                    | models.Q(
                                        (
                                            "adopted_at__gte",
                                            models.F("stored_at"),
                                        )
                                    )
                                )
                            )
                        ),
                        name="corr_in_resv_adoption_chronology",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("completed_at__isnull", True))
                            | models.Q(
                                ("adopted_at__isnull", True),
                                ("adopted_attachment__isnull", True),
                                ("adopted_receipt__isnull", True),
                                ("lease_expires_at__isnull", True),
                                ("lease_token__isnull", True),
                            )
                        ),
                        name="corr_in_resv_completed_unadopted",
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
                        name="corr_in_resv_lease_complete",
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
                        name="corr_in_resv_attempt_evidence",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="InboundQuarantineTransportAlias",
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
                            ("idempotency", "Idempotency"),
                            ("provider", "Provider"),
                        ],
                        editable=False,
                        max_length=20,
                    ),
                ),
                (
                    "identity",
                    models.CharField(editable=False, max_length=64),
                ),
                (
                    "logical_identity",
                    models.CharField(editable=False, max_length=64),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "identity"),
                        name="corr_in_alias_kind_identity_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("identity__regex", r"\A[0-9a-f]{64}\Z"),
                            (
                                "logical_identity__regex",
                                r"\A[0-9a-f]{64}\Z",
                            ),
                        ),
                        name="corr_in_alias_hashes_valid",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reject_reverse_reservation_state,
        ),
    ]
