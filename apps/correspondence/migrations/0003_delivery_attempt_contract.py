import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def reject_legacy_delivery_state(apps, schema_editor):
    Attempt = apps.get_model("correspondence", "CorrespondenceDeliveryAttempt")
    attempt_ids = list(
        Attempt.objects.order_by("created_at", "pk").values_list(
            "pk",
            flat=True,
        )[:10]
    )
    if attempt_ids:
        joined = ", ".join(str(value) for value in attempt_ids)
        raise RuntimeError(
            "Cannot migrate pre-contract delivery attempts without inventing "
            "provider evidence. Review and explicitly reconcile these attempt "
            f"IDs before applying correspondence migration 0003: {joined}"
        )

    Delivery = apps.get_model("correspondence", "CorrespondenceDelivery")
    delivery_ids = list(
        Delivery.objects.exclude(status="cancelled")
        .order_by("queued_at", "pk")
        .values_list("pk", flat=True)[:10]
    )
    if delivery_ids:
        joined = ", ".join(str(value) for value in delivery_ids)
        raise RuntimeError(
            "Cannot migrate active or evidentiary pre-contract deliveries "
            "without inventing provider evidence or orphaning unresolved "
            "work. Review and explicitly resolve or cancel these delivery "
            f"IDs before applying correspondence migration 0003: {joined}"
        )


def reject_reverse_with_delivery_state(apps, schema_editor):
    Attempt = apps.get_model("correspondence", "CorrespondenceDeliveryAttempt")
    attempt_ids = list(
        Attempt.objects.order_by("created_at", "pk").values_list(
            "pk",
            flat=True,
        )[:10]
    )
    if attempt_ids:
        joined = ", ".join(str(value) for value in attempt_ids)
        raise RuntimeError(
            "Cannot reverse correspondence migration 0003 because doing so "
            "would discard immutable delivery evidence. Preserve or explicitly "
            f"reconcile these attempt IDs before rollback: {joined}"
        )

    Delivery = apps.get_model("correspondence", "CorrespondenceDelivery")
    delivery_ids = list(
        Delivery.objects.exclude(status="cancelled")
        .order_by("queued_at", "pk")
        .values_list("pk", flat=True)[:10]
    )
    if delivery_ids:
        joined = ", ".join(str(value) for value in delivery_ids)
        raise RuntimeError(
            "Cannot reverse correspondence migration 0003 with active or "
            "evidentiary deliveries because the old release cannot safely "
            "continue their contract. Restore the pre-cutover database backup "
            f"or explicitly resolve or cancel these delivery IDs: {joined}"
        )


def invalidate_legacy_installment_drafts(apps, schema_editor):
    Correspondence = apps.get_model("correspondence", "Correspondence")
    Correspondence.objects.filter(
        direction="outbound",
        kind="installment_notice",
        status="draft",
        template_identifier="installment_notice",
        template_version="1",
    ).update(
        template_version="2",
        draft_subject="Installment delivery foundation requires review",
        draft_body=(
            "This legacy installment draft was invalidated during the "
            "correspondence delivery contract cutover. A Records Officer "
            "must edit it through the structured installment action before "
            "use. It cannot be approved or sent.\n"
        ),
        version=models.F("version") + 1,
    )


def reject_reverse_with_v2_installment_drafts(apps, schema_editor):
    Correspondence = apps.get_model("correspondence", "Correspondence")
    correspondence_ids = list(
        Correspondence.objects.filter(
            kind="installment_notice",
            template_identifier="installment_notice",
            template_version="2",
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:10]
    )
    if correspondence_ids:
        joined = ", ".join(str(value) for value in correspondence_ids)
        raise RuntimeError(
            "Cannot reverse correspondence migration 0003 while version 2 "
            "installment foundations exist because the old release cannot "
            "safely render them. Restore the pre-cutover database backup or "
            f"explicitly reconcile these correspondence IDs: {joined}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0002_correspondence_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="correspondencedeliveryattempt",
            name="action",
            field=models.CharField(
                choices=[("send", "Send"), ("reconcile", "Reconcile")],
                default="send",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="correspondencedeliveryattempt",
            name="result_fingerprint",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="correspondencedeliveryattempt",
            name="provider_timestamp",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="correspondencedeliveryattempt",
            name="result_category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("unknown", "Unknown send outcome"),
                    ("reconciled", "Reconciled"),
                    ("not_found", "Not found during reconciliation"),
                    ("cancelled", "Cancelled"),
                    ("lease_expired", "Lease expired"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="correspondencedeliveryattempt",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="correspondence_delivery_attempts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            reject_legacy_delivery_state,
            reject_reverse_with_delivery_state,
        ),
        migrations.RunPython(
            invalidate_legacy_installment_drafts,
            reject_reverse_with_v2_installment_drafts,
        ),
        migrations.AlterModelOptions(
            name="correspondencedelivery",
            options={
                "base_manager_name": "objects",
                "ordering": ["queued_at", "id"],
                "permissions": [
                    (
                        "deliver_outbound_correspondence",
                        "Can claim and report outbound correspondence deliveries",
                    )
                ],
            },
        ),
        migrations.AlterModelOptions(
            name="correspondencedeliveryattempt",
            options={
                "base_manager_name": "objects",
                "ordering": ["attempt_number"],
            },
        ),
        migrations.AddConstraint(
            model_name="correspondencedelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "pending",
                        "claimed",
                        "awaiting_reconciliation",
                        "sent",
                        "failed",
                        "retryable",
                        "cancelled",
                    ]
                ),
                name="correspondence_delivery_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedelivery",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        claim_owner="",
                        claim_expires_at__isnull=True,
                    )
                    | (
                        ~models.Q(claim_owner="")
                        & models.Q(claim_expires_at__isnull=False)
                    )
                ),
                name="correspondence_delivery_claim_fields_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedelivery",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        status__in=["claimed", "awaiting_reconciliation"]
                    )
                    | models.Q(
                        claim_owner="",
                        claim_expires_at__isnull=True,
                    )
                ),
                name="correspondence_delivery_claim_state_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(action__in=["send", "reconcile"]),
                name="correspondence_delivery_attempt_action_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.UniqueConstraint(
                condition=models.Q(result_at__isnull=True),
                fields=("delivery",),
                name="correspondence_one_open_delivery_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    result_category__in=["sent", "reconciled"]
                ),
                fields=("delivery",),
                name="correspondence_one_successful_delivery_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=~models.Q(claim_owner=""),
                name="correspondence_delivery_attempt_owner_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(result_at__isnull=True)
                    | models.Q(
                        result_at__gte=models.F("claimed_at"),
                    )
                ),
                name="correspondence_delivery_attempt_result_chronology",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="correspondencedeliveryattempt",
            name="correspondence_delivery_attempt_result_complete",
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        result_at__isnull=True,
                        result_category="",
                    )
                    | models.Q(
                        result_at__isnull=False,
                        result_category__in=[
                            "sent",
                            "failed",
                            "unknown",
                            "reconciled",
                            "not_found",
                            "cancelled",
                            "lease_expired",
                        ],
                    )
                ),
                name="correspondence_delivery_attempt_result_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        result_at__isnull=True,
                        result_fingerprint="",
                    )
                    | (
                        models.Q(
                            result_at__isnull=False,
                            result_fingerprint__regex=(
                                r"\A[0-9a-f]{64}\Z"
                            ),
                        )
                    )
                ),
                name="correspondence_delivery_attempt_fingerprint_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(
                        result_category__in=["reconciled", "not_found"]
                    )
                    | models.Q(action="reconcile")
                ),
                name="correspondence_delivery_attempt_reconcile_result",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(result_category__in=["sent", "failed"])
                    | models.Q(action="send")
                ),
                name="correspondence_delivery_attempt_send_result",
            ),
        ),
        migrations.AddConstraint(
            model_name="correspondencedeliveryattempt",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            result_category__in=["sent", "reconciled"]
                        )
                        & ~models.Q(provider_message_id="")
                        & ~models.Q(provider_thread_id="")
                        & models.Q(provider_timestamp__isnull=False)
                    )
                    | (
                        ~models.Q(
                            result_category__in=["sent", "reconciled"]
                        )
                        & models.Q(provider_message_id="")
                        & models.Q(provider_thread_id="")
                        & models.Q(provider_timestamp__isnull=True)
                    )
                ),
                name="correspondence_delivery_attempt_success_evidence",
            ),
        ),
    ]
