import apps.correspondence.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _sample_ids(queryset):
    return ", ".join(
        str(value)
        for value in queryset.order_by("pk").values_list("pk", flat=True)[:10]
    )


def reject_precontract_inbound_state(apps, schema_editor):
    Receipt = apps.get_model(
        "correspondence",
        "InboundCorrespondenceReceipt",
    )
    receipt_ids = _sample_ids(Receipt.objects.all())
    if receipt_ids:
        raise RuntimeError(
            "Cannot migrate pre-contract inbound receipts without inventing "
            "the submitter, upstream idempotency key, or canonical payload "
            "digest. Review and explicitly reconcile these receipt IDs before "
            f"applying correspondence migration 0004: {receipt_ids}"
        )

    Attachment = apps.get_model(
        "correspondence",
        "CorrespondenceAttachment",
    )
    inbound_attachment_ids = _sample_ids(
        Attachment.objects.filter(correspondence__direction="inbound")
    )
    if inbound_attachment_ids:
        raise RuntimeError(
            "Cannot migrate pre-contract inbound attachments because their "
            "receipt provenance cannot be inferred. Review and explicitly "
            "reconcile these attachment IDs before applying correspondence "
            f"migration 0004: {inbound_attachment_ids}"
        )

    duplicate_attachment_keys = list(
        Attachment.objects.values("storage_key")
        .annotate(row_count=models.Count("pk"))
        .filter(row_count__gt=1)
        .order_by("storage_key")
        .values_list("storage_key", flat=True)[:10]
    )
    if duplicate_attachment_keys:
        joined = ", ".join(duplicate_attachment_keys)
        raise RuntimeError(
            "Cannot enforce generated attachment-key uniqueness while legacy "
            "duplicates exist. Review and explicitly reconcile these storage "
            f"keys before applying correspondence migration 0004: {joined}"
        )

    exact_scan_state = (
        models.Q(
            scan_status="pending",
            quarantined=True,
            quarantine_reason="",
            scanned_at__isnull=True,
        )
        | models.Q(
            scan_status="clean",
            quarantined=False,
            quarantine_reason="",
            scanned_at__isnull=False,
            scanned_at__gte=models.F("created_at"),
        )
        | (
            models.Q(
                scan_status__in=["infected", "error"],
                quarantined=True,
                scanned_at__isnull=False,
                scanned_at__gte=models.F("created_at"),
            )
            & ~models.Q(quarantine_reason="")
        )
    )
    invalid_scan_attachment_ids = _sample_ids(
        Attachment.objects.exclude(exact_scan_state)
    )
    if invalid_scan_attachment_ids:
        raise RuntimeError(
            "Cannot enforce exact correspondence attachment scan evidence "
            "while legacy rows have incomplete or contradictory verdicts. "
            "Review and explicitly reconcile these attachment IDs before "
            "applying correspondence migration 0004: "
            f"{invalid_scan_attachment_ids}"
        )

    Correspondence = apps.get_model("correspondence", "Correspondence")
    inbound_correspondence_ids = _sample_ids(
        Correspondence.objects.filter(direction="inbound")
    )
    if inbound_correspondence_ids:
        raise RuntimeError(
            "Cannot migrate pre-contract inbound correspondence without "
            "inventing the parsed sender, recipient, and top-level header "
            "snapshot. Review and explicitly reconcile these correspondence "
            f"IDs before applying correspondence migration 0004: "
            f"{inbound_correspondence_ids}"
        )


def reject_reverse_with_inbound_state(apps, schema_editor):
    Receipt = apps.get_model(
        "correspondence",
        "InboundCorrespondenceReceipt",
    )
    receipt_ids = _sample_ids(Receipt.objects.all())
    if receipt_ids:
        raise RuntimeError(
            "Cannot reverse correspondence migration 0004 because doing so "
            "would discard immutable inbound transport and matching evidence. "
            "Restore the verified pre-cutover backup or roll forward instead. "
            f"Inbound receipt IDs: {receipt_ids}"
        )

    Attachment = apps.get_model(
        "correspondence",
        "CorrespondenceAttachment",
    )
    provenance_ids = _sample_ids(
        Attachment.objects.filter(inbound_receipt__isnull=False)
    )
    if provenance_ids:
        raise RuntimeError(
            "Cannot reverse correspondence migration 0004 because doing so "
            "would discard inbound attachment provenance. Restore the verified "
            "pre-cutover backup or roll forward instead. Attachment IDs: "
            f"{provenance_ids}"
        )

    Correspondence = apps.get_model("correspondence", "Correspondence")
    inbound_correspondence_ids = _sample_ids(
        Correspondence.objects.filter(direction="inbound")
    )
    if inbound_correspondence_ids:
        raise RuntimeError(
            "Cannot reverse correspondence migration 0004 because the old "
            "release cannot preserve parsed inbound header snapshots. Restore "
            "the verified pre-cutover backup or roll forward instead. "
            f"Correspondence IDs: {inbound_correspondence_ids}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0003_delivery_attempt_contract"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="idempotency_key",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="payload_sha256",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="submitted_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submitted_inbound_correspondence_receipts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="ip_address",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="inboundcorrespondencereceipt",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="correspondence",
            name="received_sender",
            field=models.CharField(blank=True, default="", max_length=998),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="correspondence",
            name="received_recipients",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="correspondence",
            name="received_headers",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="correspondenceattachment",
            name="inbound_receipt",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="attachments",
                to="correspondence.inboundcorrespondencereceipt",
            ),
        ),
        migrations.RunPython(
            reject_precontract_inbound_state,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="inboundcorrespondencereceipt",
            name="idempotency_key",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="inboundcorrespondencereceipt",
            name="payload_sha256",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="inboundcorrespondencereceipt",
            name="submitted_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submitted_inbound_correspondence_receipts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="inboundcorrespondencereceipt",
            name="raw_storage_key",
            field=models.CharField(
                default=apps.correspondence.models._raw_message_storage_key,
                editable=False,
                max_length=1000,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="correspondenceattachment",
            name="storage_key",
            field=models.CharField(
                default=apps.correspondence.models._attachment_storage_key,
                editable=False,
                max_length=1000,
                unique=True,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="correspondenceattachment",
            name="correspondence_attachment_unquarantined_is_clean",
        ),
        migrations.AddConstraint(
            model_name="correspondenceattachment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        scan_status="pending",
                        quarantined=True,
                        quarantine_reason="",
                        scanned_at__isnull=True,
                    )
                    | models.Q(
                        scan_status="clean",
                        quarantined=False,
                        quarantine_reason="",
                        scanned_at__isnull=False,
                    )
                    | (
                        models.Q(
                            scan_status__in=["infected", "error"],
                            quarantined=True,
                            scanned_at__isnull=False,
                        )
                        & ~models.Q(quarantine_reason="")
                    )
                ),
                name="correspondence_attachment_scan_state_exact",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondenceattachment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(scanned_at__isnull=True)
                    | models.Q(scanned_at__gte=models.F("created_at"))
                ),
                name="correspondence_attachment_scan_chronology",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondence",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(direction="inbound")
                    | models.Q(received_at__isnull=False)
                ),
                name="correspondence_inbound_received_at_present",
            ),
        ),
        migrations.AlterModelOptions(
            name="correspondence",
            options={
                "base_manager_name": "objects",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AlterModelOptions(
            name="correspondenceattachment",
            options={
                "base_manager_name": "objects",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AlterModelOptions(
            name="inboundcorrespondencereceipt",
            options={
                "base_manager_name": "objects",
                "ordering": ["received_at", "id"],
                "permissions": [
                    (
                        "submit_inbound_correspondence",
                        "Can submit inbound correspondence messages",
                    )
                ],
            },
        ),
        migrations.RemoveConstraint(
            model_name="inboundcorrespondencereceipt",
            name="correspondence_inbound_match_complete",
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.UniqueConstraint(
                fields=("submitted_by", "idempotency_key"),
                name="correspondence_inbound_idempotency_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="correspondence_inbound_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(provider="")
                    & ~models.Q(provider_account="")
                    & ~models.Q(provider_message_id="")
                    & models.Q(
                        idempotency_key__regex=(
                            r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
                        )
                    )
                    & models.Q(raw_message_size__gte=1)
                ),
                name="correspondence_inbound_identity_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        raw_message_sha256__regex=r"\A[0-9a-f]{64}\Z"
                    )
                    & models.Q(
                        payload_sha256__regex=r"\A[0-9a-f]{64}\Z"
                    )
                ),
                name="correspondence_inbound_hashes_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            matching_status="matched",
                            parsing_status="parsed",
                            quarantined=False,
                            quarantine_reason="",
                            parsing_error="",
                        )
                        & models.Q(matched_request__isnull=False)
                        & models.Q(matched_correspondence__isnull=False)
                        & models.Q(matched_at__isnull=False)
                        & models.Q(candidate_request__isnull=True)
                        & models.Q(duplicate_of__isnull=True)
                        & (
                            models.Q(
                                match_method="manual",
                                matched_by__isnull=False,
                            )
                            | models.Q(
                                match_method__in=[
                                    "reply_token",
                                    "in_reply_to",
                                    "provider_thread",
                                    "references",
                                ],
                                matched_by__isnull=True,
                            )
                        )
                    )
                    | models.Q(
                        matching_status="candidate",
                        parsing_status="parsed",
                        quarantined=False,
                        quarantine_reason="",
                        parsing_error="",
                        match_method="subject",
                        candidate_request__isnull=False,
                        matched_correspondence__isnull=False,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=True,
                    )
                    | models.Q(
                        matching_status="duplicate",
                        parsing_status="duplicate",
                        quarantined=True,
                        parsing_error="",
                        match_method="",
                        candidate_request__isnull=True,
                        matched_correspondence__isnull=True,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=False,
                    )
                    | (
                        models.Q(
                            matching_status="unmatched",
                            parsing_status="pending",
                            quarantined=True,
                            quarantine_reason="",
                            parsing_error="",
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=True,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                    )
                    | (
                        models.Q(
                            matching_status="unmatched",
                            parsing_status="parsed",
                            quarantined=False,
                            quarantine_reason="",
                            parsing_error="",
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=False,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                    )
                    | models.Q(
                        matching_status="ambiguous",
                        parsing_status="parsed",
                        quarantined=False,
                        quarantine_reason="",
                        parsing_error="",
                        match_method="",
                        candidate_request__isnull=True,
                        matched_correspondence__isnull=False,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=True,
                    )
                    | (
                        models.Q(
                            matching_status="quarantined",
                            parsing_status__in=["failed", "quarantined"],
                            quarantined=True,
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=True,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                        & (
                            ~models.Q(quarantine_reason="")
                            | ~models.Q(parsing_error="")
                        )
                    )
                ),
                name="correspondence_inbound_state_exact",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    parsing_status__in=[
                        "pending",
                        "parsed",
                        "failed",
                        "quarantined",
                        "duplicate",
                    ]
                ),
                name="correspondence_inbound_parsing_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        parsing_status="pending",
                        completed_at__isnull=True,
                    )
                    | (
                        ~models.Q(parsing_status="pending")
                        & models.Q(completed_at__isnull=False)
                    )
                ),
                name="correspondence_inbound_parsing_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        parsing_status="duplicate",
                        matching_status="duplicate",
                    )
                    | (
                        ~models.Q(parsing_status="duplicate")
                        & ~models.Q(matching_status="duplicate")
                    )
                ),
                name="correspondence_inbound_duplicate_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundcorrespondencereceipt",
            constraint=models.CheckConstraint(
                condition=~models.Q(duplicate_of=models.F("id")),
                name="correspondence_inbound_not_self_duplicate",
            ),
        ),
        migrations.AddConstraint(
            model_name="clarificationcycle",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(responded_at__isnull=True)
                    | models.Q(responded_at__gte=models.F("opened_at"))
                ),
                name="correspondence_clarification_response_chronology",
            ),
        ),
        migrations.AddConstraint(
            model_name="clarificationcycle",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(resolved_at__isnull=True)
                    | models.Q(resolved_at__gte=models.F("responded_at"))
                ),
                name="correspondence_clarification_resolution_chronology",
            ),
        ),
        migrations.AddConstraint(
            model_name="clarificationcycle",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(cancelled_at__isnull=True)
                    | models.Q(cancelled_at__gte=models.F("opened_at"))
                ),
                name="correspondence_clarification_cancellation_chronology",
            ),
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reject_reverse_with_inbound_state,
        ),
    ]
