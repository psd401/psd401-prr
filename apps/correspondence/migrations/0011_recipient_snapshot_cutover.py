import hashlib
import json
import re

from django.db import migrations


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def refuse_unattested_recipient_snapshots(apps, schema_editor):
    Correspondence = apps.get_model("correspondence", "Correspondence")
    Recipient = apps.get_model(
        "correspondence",
        "CorrespondenceRecipient",
    )
    AuditLog = apps.get_model("audit", "AuditLog")
    unsafe_ids = []
    candidates = (
        Correspondence.objects.filter(
            direction="outbound",
            status__in=["approved", "queued", "failed"],
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for correspondence_id in candidates.iterator(chunk_size=500):
        approvals = list(
            AuditLog.objects.filter(
                action="correspondence.approved",
                detail__correspondence_id=str(correspondence_id),
            )
            .order_by("pk")
            .values_list("detail", flat=True)[:2]
        )
        if len(approvals) != 1:
            unsafe_ids.append(str(correspondence_id))
            continue
        detail = approvals[0]
        if not isinstance(detail, dict):
            unsafe_ids.append(str(correspondence_id))
            continue
        digest = detail.get("recipient_snapshot_sha256")
        count = detail.get("recipient_count")
        recipients = [
            {
                "id": str(recipient_id),
                "recipient_type": recipient_type,
                "email": email,
                "display_name": display_name,
                "requester_id": (
                    str(requester_id) if requester_id else None
                ),
            }
            for (
                recipient_id,
                recipient_type,
                email,
                display_name,
                requester_id,
            ) in Recipient.objects.filter(
                correspondence_id=correspondence_id
            ).order_by("id").values_list(
                "id",
                "recipient_type",
                "email",
                "display_name",
                "requester_id",
            )
        ]
        serialized = json.dumps(
            recipients,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual_digest = hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or count != len(recipients)
            or digest != actual_digest
        ):
            unsafe_ids.append(str(correspondence_id))

    if unsafe_ids:
        sample = ", ".join(unsafe_ids[:25])
        suffix = (
            f" (and {len(unsafe_ids) - 25} more)"
            if len(unsafe_ids) > 25
            else ""
        )
        raise RuntimeError(
            "Cannot enable recipient-snapshot claim verification while "
            "approved or claimable legacy correspondence lacks an immutable "
            "approval digest. Before retrying this deployment, use the prior "
            "release to drain, cancel, or replace these correspondence IDs: "
            f"{sample}{suffix}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("correspondence", "0010_correspondence_recipient_base_manager"),
    ]

    operations = [
        migrations.RunPython(
            refuse_unattested_recipient_snapshots,
            migrations.RunPython.noop,
        ),
    ]
