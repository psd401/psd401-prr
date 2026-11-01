# ABOUTME: Builders for matched and unmatched requester emails with their Inbox rows.
# ABOUTME: Shared by the merge, closure, and per-case Inbox tests; not a test module.
from django.utils import timezone

from apps.correspondence.models import Correspondence, InboundCorrespondenceReceipt
from apps.work_queue.models import WorkItem
from apps.work_queue.services import materialize_work_item

_sequence = 0


def _next():
    global _sequence
    _sequence += 1
    return _sequence


def matched_email(prr, *, officer, intake=False, subject="Records request"):
    """One requester email matched to ``prr`` with its Needs-attention row.

    ``intake=True`` models the email that created the case (an "Emailed
    request" row); otherwise it is a reply on an existing case.
    """
    n = _next()
    now = timezone.now()
    correspondence = Correspondence.objects.create(
        request=prr,
        direction=Correspondence.Direction.INBOUND,
        kind=Correspondence.Kind.GENERAL,
        status=Correspondence.Status.RECEIVED,
        received_subject=subject,
        received_body="Requester message body.",
        received_sender="Jane <jane@example.org>",
        received_recipients=["publicrecords@psd401.net"],
        received_at=now,
        matched_at=now,
        provider="gmail",
        provider_account="publicrecords@psd401.net",
        provider_message_id=f"fixture-message-{n}",
        created_by=officer,
    )
    receipt = InboundCorrespondenceReceipt.objects.create(
        provider="gmail",
        provider_account="publicrecords@psd401.net",
        provider_message_id=f"fixture-message-{n}",
        provider_thread_id=f"fixture-thread-{n}",
        received_at=now,
        envelope_sender="jane@example.org",
        envelope_recipients=["publicrecords@psd401.net"],
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=(
            InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
            if intake
            else InboundCorrespondenceReceipt.MatchMethod.MANUAL
        ),
        matched_request=prr,
        matched_correspondence=correspondence,
        matched_at=now,
        matched_by=None if intake else officer,
        quarantined=False,
        raw_message_size=128,
        raw_message_sha256=f"{n:064x}",
        payload_sha256=f"{n + 1000:064x}",
        idempotency_key=f"gmail:poll:fixture-{n}",
        submitted_by=officer,
        completed_at=now,
    )
    work_item = materialize_work_item(
        source_type=WorkItem.SourceType.INBOUND_RECEIPT,
        source_id=receipt.pk,
        work_kind=(
            WorkItem.WorkKind.EMAIL_INTAKE
            if intake
            else WorkItem.WorkKind.INBOUND_REPLY
        ),
        request=prr,
    ).work_item
    return receipt, correspondence, work_item


def unmatched_email(*, officer, subject):
    """A parsed, not-yet-matched requester email, ready for automatic matching."""
    n = _next()
    now = timezone.now()
    correspondence = Correspondence.objects.create(
        request=None,
        direction=Correspondence.Direction.INBOUND,
        kind=Correspondence.Kind.GENERAL,
        status=Correspondence.Status.UNMATCHED,
        received_subject=subject,
        received_body="Requester message body.",
        received_at=now,
        provider="gmail",
        provider_account="publicrecords@psd401.net",
        provider_message_id=f"fixture-unmatched-{n}",
    )
    receipt = InboundCorrespondenceReceipt.objects.create(
        provider="gmail",
        provider_account="publicrecords@psd401.net",
        provider_message_id=f"fixture-unmatched-{n}",
        received_at=now,
        envelope_sender="jane@example.org",
        envelope_recipients=["publicrecords@psd401.net"],
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        matched_correspondence=correspondence,
        quarantined=False,
        raw_message_size=128,
        raw_message_sha256=f"{n:064x}",
        payload_sha256=f"{n + 1000:064x}",
        idempotency_key=f"gmail:poll:fixture-unmatched-{n}",
        submitted_by=officer,
        completed_at=now,
    )
    return receipt, correspondence
