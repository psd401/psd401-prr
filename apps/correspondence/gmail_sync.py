"""Durable in-process ingestion of the pinned Gmail mailbox."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.configuration import runtime
from apps.integrations.gmail import client as gmail_client
from apps.records.service_identities import (
    INBOUND_PERMISSION,
    SYSTEM_INBOUND_TRANSPORT_USERNAME,
    is_exact_service_identity_row,
)

from . import receiving
from .models import GmailSyncState, InboundCorrespondenceReceipt

logger = logging.getLogger(__name__)


def system_inbound_principal():
    """Load the migration-provisioned, exact-scope Gmail poller principal."""
    try:
        actor = get_user_model().objects.get(
            username=SYSTEM_INBOUND_TRANSPORT_USERNAME,
        )
    except get_user_model().DoesNotExist as exc:
        raise PermissionDenied(
            "The system inbound transport principal is not provisioned."
        ) from exc
    if (
        not is_exact_service_identity_row(actor, INBOUND_PERMISSION)
        or Token.objects.filter(user=actor).exists()
    ):
        raise PermissionDenied(
            "The system inbound transport principal does not have exact scope."
        )
    return actor


def _history_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdigit() or len(normalized) > 255:
        raise ValidationError("The Gmail history identifier is invalid.")
    return normalized


def _resync_query() -> str:
    clauses = ["in:inbox"]
    if settings.PRR_ENVIRONMENT != "production":
        clauses.append(f"label:PRR/{settings.PRR_ENVIRONMENT}")
    clauses.extend(
        [
            f"-label:{runtime.get('PRR_GMAIL_INBOUND_PROCESSED_LABEL')}",
            f"newer_than:{runtime.get('PRR_GMAIL_INBOUND_LOOKBACK_DAYS')}d",
        ]
    )
    return " ".join(clauses)


def _history_messages(payload: dict) -> list[gmail_client.SentMessage]:
    history = payload.get("history", [])
    if not isinstance(history, list):
        raise ValidationError("The Gmail history payload is invalid.")
    found = []
    seen = set()
    for entry in history:
        if not isinstance(entry, dict):
            raise ValidationError("The Gmail history payload is invalid.")
        additions = entry.get("messagesAdded", [])
        if not isinstance(additions, list):
            raise ValidationError("The Gmail history payload is invalid.")
        for addition in additions:
            message = addition.get("message") if isinstance(addition, dict) else None
            if not isinstance(message, dict):
                raise ValidationError("The Gmail history payload is invalid.")
            message_id = str(message.get("id", "")).strip()
            thread_id = str(message.get("threadId", "")).strip()
            if not message_id or not thread_id:
                raise ValidationError("The Gmail history payload is invalid.")
            if message_id in seen:
                continue
            seen.add(message_id)
            found.append(gmail_client.SentMessage(message_id, thread_id))
    return found


def _header_section(raw_bytes: bytes) -> bytes | None:
    limit = (
        runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS")
        * (runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES") + 2)
    )
    bounded = raw_bytes[: limit + 4]
    separator = bounded.find(b"\r\n\r\n")
    separator_size = 4
    if separator < 0:
        separator = bounded.find(b"\n\n")
        separator_size = 2
    if separator < 0 or separator > limit:
        return None
    header_section = bounded[: separator + separator_size]
    lines = header_section.splitlines()
    line_limit = runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES")
    if (
        len(lines) > runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS") + 1
        or any(len(line) > line_limit for line in lines)
    ):
        return None
    return header_section


def _routing_recipients(raw_bytes: bytes) -> list[str]:
    """Extract only provider-added routing headers under strict bounds."""
    header_bytes = _header_section(raw_bytes)
    if header_bytes is None:
        return [runtime.get("PRR_GMAIL_MAILBOX")]
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(
            header_bytes,
            headersonly=True,
        )
    except (TypeError, ValueError):
        return [runtime.get("PRR_GMAIL_MAILBOX")]
    try:
        for name in ("Delivered-To", "X-Original-To"):
            for value in headers.get_all(name, []):
                for _display_name, address in getaddresses([value]):
                    try:
                        validate_email(address)
                    except ValidationError:
                        continue
                    return [address]
    except (AttributeError, TypeError, ValueError):
        return [runtime.get("PRR_GMAIL_MAILBOX")]
    return [runtime.get("PRR_GMAIL_MAILBOX")]


def _is_own_auto_generated(fetched) -> bool:
    """Mail this mailbox machine-sent to itself is not inbound correspondence.

    Gmail's SENT label is the authority: an outsider can forge a From header
    but cannot make Gmail label a message as sent by this mailbox. A person
    writing to the mailbox from itself (no Auto-Submitted, or ``no``) is still
    received.
    """
    if "SENT" not in fetched.label_ids:
        return False
    header_bytes = _header_section(fetched.raw_bytes)
    if header_bytes is None:
        return False
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(
            header_bytes,
            headersonly=True,
        )
        values = headers.get_all("Auto-Submitted", [])
    except (AttributeError, TypeError, ValueError):
        return False
    if len(values) != 1:
        return False
    return str(values[0]).strip().casefold() not in {"", "no"}


def _idempotency_key(message_id: str) -> str:
    digest = hashlib.sha256(
        f"{runtime.get('PRR_GMAIL_MAILBOX')}\0{message_id}".encode()
    ).hexdigest()
    return f"gmail:poll:{digest}"


def _advance_checkpoint(state_id, history_id: str) -> None:
    normalized = _history_id(history_id)
    with transaction.atomic():
        state = GmailSyncState.objects.select_for_update().get(pk=state_id)
        current = int(state.history_id) if state.history_id else 0
        if int(normalized) > current:
            state.history_id = normalized
        state.last_synced_at = timezone.now()
        state.save(
            update_fields=["history_id", "last_synced_at", "updated_at"]
        )


def _poll_batch(state: GmailSyncState):
    if not state.history_id or settings.PRR_ENVIRONMENT != "production":
        checkpoint = _history_id(gmail_client.current_history_id())
        return gmail_client.list_messages(_resync_query()), checkpoint, True
    try:
        payload = gmail_client.list_history(state.history_id)
    except gmail_client.GmailUnavailable as exc:
        if exc.status_code != 404:
            raise
        logger.warning(
            "Gmail history expired for mailbox %s at checkpoint %s; "
            "starting a bounded resync",
            state.mailbox,
            state.history_id,
        )
        checkpoint = _history_id(gmail_client.current_history_id())
        return gmail_client.list_messages(_resync_query()), checkpoint, True
    checkpoint = _history_id(payload.get("historyId"))
    return _history_messages(payload), checkpoint, False


def _label_processed(message_id: str) -> int:
    """Apply the processed label; return 1 on a tolerated label failure."""
    try:
        gmail_client.add_label(
            message_id,
            runtime.get("PRR_GMAIL_INBOUND_PROCESSED_LABEL"),
            archive=runtime.get("PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED"),
        )
    except gmail_client.GmailUnavailable:
        logger.warning(
            "Durably handled Gmail message %s but could not apply "
            "the processed label",
            message_id,
        )
        return 1
    return 0


def _ingest_listed(
    listed,
    *,
    actor,
    mailbox: str,
    fetched=None,
    open_cases: bool = True,
) -> str:
    """Durably receive one listed message; return what happened to it.

    Returns ``"received"``, ``"deduplicated"``, or ``"self_generated"``. The
    processed label is applied only after the message is durably received, so
    a failure here leaves it unlabelled for the next reconciliation pass.
    """
    if fetched is None:
        fetched = gmail_client.get_message(listed.message_id)
    if fetched.thread_id != listed.thread_id:
        raise ValidationError("The Gmail message thread changed during polling.")
    if _is_own_auto_generated(fetched):
        from apps.audit.models import audit

        audit(
            "gmail.inbound_self_generated_skipped",
            target=fetched.message_id,
            actor=actor,
            thread_id=fetched.thread_id,
        )
        _label_processed(fetched.message_id)
        return "self_generated"
    result = receiving.receive_inbound_message(
        raw_message=fetched.raw_bytes,
        provider="gmail",
        provider_account=mailbox,
        provider_message_id=fetched.message_id,
        provider_thread_id=fetched.thread_id,
        envelope_sender="",
        envelope_recipients=_routing_recipients(fetched.raw_bytes),
        received_at=fetched.received_at,
        idempotency_key=_idempotency_key(fetched.message_id),
        actor=actor,
    )
    if result.quarantine_status == "quarantined" and not result.deduplicated:
        _alert_whole_message_quarantine(result.receipt, fetched.raw_bytes)
    if open_cases:
        from apps.records.email_intake import create_request_from_unmatched_email

        create_request_from_unmatched_email(result.receipt, actor=actor)
    outcome = "deduplicated" if result.deduplicated else "received"
    if _label_processed(fetched.message_id):
        return f"{outcome}_label_failed"
    return outcome


def _alert_whole_message_quarantine(receipt, raw_bytes: bytes) -> None:
    """Tell the records team at once that a message was received but held.

    A whole-message quarantine has no correspondence, matches no case, and
    opens none, yet Gmail still labels it processed. Without this alert it
    would wait unseen in the staff Inbox while any request in it is already on
    the statutory clock.
    """
    if receipt.parsing_status not in {
        InboundCorrespondenceReceipt.ParsingStatus.FAILED,
        InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
    }:
        return
    from apps.records import notifications

    sender = subject = ""
    header_bytes = _header_section(raw_bytes)
    if header_bytes is not None:
        try:
            headers = BytesHeaderParser(policy=policy.compat32).parsebytes(
                header_bytes, headersonly=True
            )
            sender = str(headers.get("From", ""))[:200]
            subject = str(headers.get("Subject", ""))[:200]
        except (TypeError, ValueError):
            pass
    notifications.notify_inbound_mailbox(
        signal_type="inbound.message_quarantined",
        subject=(
            "[INBOUND MAIL HELD] "
            + (f"\"{subject[:80]}\"" if subject else "A message")
            + " was received but no case was opened"
        ),
        template_name="notifications/inbound_message_quarantined.txt",
        context={
            "receipt_id": str(receipt.pk),
            "reason": receipt.quarantine_reason or receipt.parsing_error,
            "received_at": receipt.received_at,
            "recipients": ", ".join(receipt.envelope_recipients or ()),
            "sender": sender,
            "subject": subject,
        },
        condition_key=str(receipt.pk),
        include_supervisor=True,
        immediate=True,
    )


def _record_message_failure(message_id: str, exc: Exception, *, actor) -> None:
    """Record one message that could not be received, without its content."""
    from apps.audit.models import audit
    from apps.records import notifications

    failure = type(exc).__name__
    logger.error(
        "Inbound Gmail message %s could not be received (%s); it stays "
        "unlabelled for reconciliation",
        message_id,
        failure,
    )
    audit(
        "gmail.inbound_message_failed",
        target=message_id,
        actor=actor,
        failure=failure,
    )
    notifications.notify_inbound_mailbox(
        signal_type="inbound.message_failed",
        subject=f"[INBOUND MAIL FAILED] Gmail message {message_id} was not received",
        template_name="notifications/inbound_message_failed.txt",
        context={
            "message_id": message_id,
            "failure": failure,
            "lookback_days": runtime.get("PRR_GMAIL_INBOUND_LOOKBACK_DAYS"),
        },
        condition_key=f"{message_id}:{failure}",
        include_supervisor=True,
    )


def _ingest_batch(
    messages,
    *,
    actor,
    mailbox: str,
    prefetched=None,
    received_ids=None,
    open_cases: bool = True,
) -> dict[str, int]:
    """Receive each message in turn, isolating any one that fails.

    A Gmail transport failure still aborts the batch: the checkpoint must not
    advance past messages that were never fetched. Any other failure is
    recorded and alerted, and the rest of the batch continues, so one message
    can no longer hold every later message back. The failed message stays
    unlabelled, and the reconciliation pass retries it while it is inside the
    lookback window. A failure that is not about one message (the database
    being down, say) also stops the checkpoint write that follows the batch.
    """
    counts = {
        "received": 0,
        "deduplicated": 0,
        "label_failures": 0,
        "self_generated_skipped": 0,
        "failed": 0,
    }
    for listed in messages:
        try:
            outcome = _ingest_listed(
                listed,
                actor=actor,
                mailbox=mailbox,
                fetched=(prefetched or {}).get(listed.message_id),
                open_cases=open_cases,
            )
        except gmail_client.GmailUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one message's fault is counted, the poll goes on
            counts["failed"] += 1
            _record_message_failure(listed.message_id, exc, actor=actor)
            continue
        if outcome.endswith("_label_failed"):
            counts["label_failures"] += 1
            outcome = outcome.removesuffix("_label_failed")
        if outcome == "self_generated":
            counts["self_generated_skipped"] += 1
        else:
            counts[outcome] += 1
        if outcome == "received" and received_ids is not None:
            received_ids.append(listed.message_id)
    return counts


def poll_inbound_mailbox() -> dict[str, object]:
    """Receive one Gmail history batch and advance only after durable receipt."""
    if not runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED"):
        return {"status": "disabled"}
    if not runtime.get("PRR_GMAIL_ENABLED"):
        logger.warning("Inbound Gmail polling stopped by the global Gmail switch")
        return {"status": "gmail_disabled"}

    actor = system_inbound_principal()
    # Read once for the whole batch. Every message in it is attributed to the
    # mailbox this run polled, so a change mid-batch cannot leave half the
    # batch recorded against a mailbox it did not come from.
    mailbox = runtime.get("PRR_GMAIL_MAILBOX")
    state, _created = GmailSyncState.objects.get_or_create(
        mailbox=mailbox,
        environment=settings.PRR_ENVIRONMENT,
    )
    try:
        messages, checkpoint, resynced = _poll_batch(state)
        counts = _ingest_batch(messages, actor=actor, mailbox=mailbox)
        _advance_checkpoint(state.pk, checkpoint)
    except Exception:
        logger.warning(
            "Inbound Gmail poll failed for mailbox %s at checkpoint %s",
            state.mailbox,
            state.history_id or "initial",
        )
        raise

    return {
        "status": "ok",
        **counts,
        "resynced": resynced,
        "history_id": checkpoint,
    }


def reconciliation_query(*, since_days: int | None = None, after=None) -> str:
    """Every unprocessed message outside sent, drafts, chat, spam, and trash.

    Unlike the history feed, this does not depend on the INBOX label: it also
    finds mail a Gmail filter skipped past the inbox, mail archived by hand
    before it was received, and mail moved out of spam after it arrived.
    """
    clauses = ["-in:sent", "-in:drafts", "-in:chats", "-in:spam", "-in:trash"]
    if settings.PRR_ENVIRONMENT != "production":
        clauses.append(f"label:PRR/{settings.PRR_ENVIRONMENT}")
    clauses.append(f"-label:{runtime.get('PRR_GMAIL_INBOUND_PROCESSED_LABEL')}")
    if after is not None:
        clauses.append(f"after:{after:%Y/%m/%d}")
    else:
        days = since_days or runtime.get("PRR_GMAIL_INBOUND_LOOKBACK_DAYS")
        clauses.append(f"newer_than:{days}d")
    return " ".join(clauses)


def reconcile_inbound_mailbox(*, now=None) -> dict[str, object]:
    """Receive any unprocessed mailbox message the history poll missed.

    Messages newer than the stale threshold are left to the minute poll, so a
    message that is merely in flight is not reported as missed. Anything this
    pass receives is, by definition, mail the poller did not, and the records
    team is told.
    """
    if not runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED"):
        return {"status": "disabled"}
    if not runtime.get("PRR_GMAIL_ENABLED"):
        return {"status": "gmail_disabled"}

    actor = system_inbound_principal()
    mailbox = runtime.get("PRR_GMAIL_MAILBOX")
    observed_at = now or timezone.now()
    grace = dt.timedelta(seconds=runtime.get("PRR_GMAIL_INBOUND_STALE_SECONDS"))
    listed = gmail_client.list_messages(reconciliation_query())
    settled = []
    prefetched = {}
    in_flight = 0
    for item in listed:
        fetched = gmail_client.get_message(item.message_id)
        if fetched.received_at > observed_at - grace:
            in_flight += 1
            continue
        settled.append(item)
        prefetched[item.message_id] = fetched
    recovered_ids = []
    counts = _ingest_batch(
        settled,
        actor=actor,
        mailbox=mailbox,
        prefetched=prefetched,
        received_ids=recovered_ids,
    )
    recovered = counts["received"]
    if recovered:
        from apps.audit.models import audit
        from apps.records import notifications

        audit(
            "gmail.inbound_reconciliation_recovered",
            target=mailbox,
            actor=actor,
            recovered=recovered,
            message_ids=recovered_ids,
        )
        notifications.notify_inbound_mailbox(
            signal_type="inbound.messages_recovered",
            subject=(
                f"[INBOUND MAIL RECOVERED] {recovered} message(s) the poller "
                "missed were received late"
            ),
            template_name="notifications/inbound_messages_recovered.txt",
            context={"recovered": recovered, "message_ids": recovered_ids},
            condition_key=",".join(sorted(recovered_ids)),
            include_supervisor=True,
        )
    return {
        "status": "ok",
        **counts,
        "in_flight": in_flight,
    }


def catch_up_messages(message_ids, *, open_cases: bool) -> dict[str, object]:
    """Receive specific, named mailbox messages an officer chose by hand.

    ``open_cases=False`` receives them for triage only: each lands in the
    staff Inbox and no case is opened on its own.
    """
    if not runtime.get("PRR_GMAIL_ENABLED"):
        return {"status": "gmail_disabled"}
    actor = system_inbound_principal()
    mailbox = runtime.get("PRR_GMAIL_MAILBOX")
    prefetched = {}
    listed = []
    for message_id in message_ids:
        fetched = gmail_client.get_message(message_id)
        prefetched[fetched.message_id] = fetched
        listed.append(gmail_client.SentMessage(fetched.message_id, fetched.thread_id))
    received_ids = []
    counts = _ingest_batch(
        listed,
        actor=actor,
        mailbox=mailbox,
        prefetched=prefetched,
        received_ids=received_ids,
        open_cases=open_cases,
    )
    return {"status": "ok", **counts, "received_ids": received_ids}
