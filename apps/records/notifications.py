"""Internal staff notification email over the mailbox-pinned Gmail client.

These operational messages are deliberately separate from requester
``Correspondence``.  They never carry statutory lifecycle effects and share
only the Gmail transport.  A cache-backed suppression key prevents repeated
watchdog runs from sending the same unchanged alert, while Celery provides
bounded retry for transport failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Final

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime
from apps.integrations.gmail import client as gmail_client

logger = logging.getLogger(__name__)

SIGNAL_TYPES: Final = frozenset(
    {
        "deadline.warning",
        "deadline.critical",
        "estimate.overdue",
        "estimate.approaching",
        "request.stalled",
        "footage.manual_hold_required",
        "footage.hold_complete",
        "footage.adequate_search_review_queued",
        "collection.task_created",
        "intake.auto_applied",
        "correspondence.delivery_effects_failed",
        "inbound.message_failed",
        "inbound.messages_recovered",
        "inbound.triage_waiting",
    }
)
#: Mailbox alerts concern the intake mailbox, not one case. Their payload
#: carries this in place of a request number for audit and log context.
MAILBOX_ALERT_TARGET: Final = "inbound-mailbox"
_CACHE_PREFIX: Final = "records:staff-notification:"


def _normalized_recipients(recipients: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for supplied in recipients:
        address = str(supplied or "").strip().lower()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError as exc:
            raise ValueError("Staff notification recipients must be valid emails.") from exc
        if address not in normalized:
            normalized.append(address)
    if not normalized:
        raise ValueError("A staff notification requires at least one recipient.")
    return tuple(normalized)


def _records_officer_recipients() -> tuple[str, ...]:
    return tuple(runtime.get("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"))


def _records_team_recipients(*, include_supervisor: bool) -> tuple[str, ...]:
    recipients = list(_records_officer_recipients())
    if include_supervisor:
        recipients.append(runtime.get("PRA_SUPERVISOR_EMAIL"))
    return _normalized_recipients(recipients)


def build_notification(
    *,
    signal_type: str,
    request,
    recipients: Iterable[str],
    subject: str,
    template_name: str,
    context: Mapping[str, object],
    condition_key: str = "",
) -> dict[str, object]:
    """Build one JSON-serializable, immutable task payload."""
    if signal_type not in SIGNAL_TYPES:
        raise ValueError("Unknown internal staff notification signal type.")
    normalized_subject = str(subject).strip()
    if (
        not normalized_subject
        or len(normalized_subject) > 998
        or "\r" in normalized_subject
        or "\n" in normalized_subject
    ):
        raise ValueError("Staff notification subjects must be one safe header line.")
    recipient_list = _normalized_recipients(recipients)
    if request is None:
        request_id = ""
        request_number = MAILBOX_ALERT_TARGET
        render_context = {
            **dict(context),
            "inbox_url": f"{runtime.get('PORTAL_BASE_URL')}/inbox",
        }
    else:
        request_id = str(request.pk)
        request_number = request.number
        render_context = {
            **dict(context),
            "request_number": request.number,
            "request_received_at": request.received_at,
            "request_description": request.description,
            "status_url": (
                f"{runtime.get('PORTAL_BASE_URL')}/requests/{request.number}"
            ),
        }
    body = render_to_string(template_name, render_context).strip() + "\n"
    fingerprint_input = json.dumps(
        {
            "signal_type": signal_type,
            "request_id": request_id,
            "recipients": recipient_list,
            "condition_key": str(condition_key),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_input).hexdigest()
    return {
        "signal_type": signal_type,
        "request_id": request_id,
        "request_number": request_number,
        "recipients": list(recipient_list),
        "subject": normalized_subject,
        "body": body,
        "fingerprint": fingerprint,
    }


def _dedupe_key(payload: Mapping[str, object]) -> str:
    return f"{_CACHE_PREFIX}{payload['fingerprint']}"


def _audit_payload(action: str, payload: Mapping[str, object], **detail) -> None:
    audit(
        action,
        target=str(payload["request_number"]),
        signal_type=str(payload["signal_type"]),
        recipients=list(payload["recipients"]),
        **detail,
    )


def _safe_audit_payload(
    action: str,
    payload: Mapping[str, object],
    **detail,
) -> bool:
    try:
        _audit_payload(action, payload, **detail)
    except Exception:
        logger.exception(
            "Internal staff notification audit write failed",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
                "staff_notification_audit_action": action,
            },
        )
        return False
    return True


def _safe_cache_delete(key: str, payload: Mapping[str, object]) -> None:
    try:
        cache.delete(key)
    except Exception:
        logger.exception(
            "Internal staff notification dedupe marker cleanup failed",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
            },
        )


def _notification_block_reason(payload: Mapping[str, object]) -> str | None:
    if not runtime.get("PRR_STAFF_NOTIFICATIONS_ENABLED"):
        return "staff_notifications_disabled"
    if not runtime.get("PRR_GMAIL_ENABLED"):
        return "gmail_disabled"
    if settings.PRR_ENVIRONMENT != "production":
        allowlist = {
            address.lower()
            for address in runtime.get("PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST")
        }
        if any(
            str(address).lower() not in allowlist
            for address in payload["recipients"]
        ):
            return "development_recipient_blocked"
    return None


def _block_notification(
    payload: Mapping[str, object],
    *,
    release_dedupe: bool,
) -> dict[str, object] | None:
    reason = _notification_block_reason(payload)
    if reason is None:
        return None
    _safe_audit_payload("staff_notification.blocked", payload, reason=reason)
    if release_dedupe:
        _safe_cache_delete(_dedupe_key(payload), payload)
    logger.warning(
        "Internal staff notification blocked by a runtime transport gate",
        extra={
            "request": payload["request_number"],
            "staff_notification_signal": payload["signal_type"],
            "staff_notification_block_reason": reason,
        },
    )
    return {
        "status": "blocked",
        "reason": reason,
        "signal_type": payload["signal_type"],
        "request_number": payload["request_number"],
    }


def _message_bytes(payload: Mapping[str, object]) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = runtime.get("PRR_GMAIL_MAILBOX")
    message["To"] = list(payload["recipients"])
    message["Subject"] = str(payload["subject"])
    # RFC 3834: machine-generated, so no auto-reply and, when the recipient
    # list includes the pinned mailbox itself, the inbound poller skips it.
    message["Auto-Submitted"] = "auto-generated"
    message["Message-ID"] = make_msgid(
        domain=runtime.get("PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN"),
    )
    message.set_content(str(payload["body"]))
    return message.as_bytes()


def _send_notification(payload: Mapping[str, object]) -> dict[str, object]:
    if not _safe_audit_payload("staff_notification.sending", payload):
        return {
            "status": "audit_failed",
            "signal_type": payload["signal_type"],
            "request_number": payload["request_number"],
        }
    sent = gmail_client.send_raw(_message_bytes(payload))
    sent_audited = _safe_audit_payload(
        "staff_notification.sent",
        payload,
        provider_message_id=sent.message_id,
        provider_thread_id=sent.thread_id,
    )
    if not sent_audited:
        logger.error(
            "Internal staff notification sent but final audit write failed",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
                "gmail_message_id": sent.message_id,
            },
        )
    logger.info(
        "Internal staff notification sent",
        extra={
            "request": payload["request_number"],
            "staff_notification_signal": payload["signal_type"],
            "recipient_count": len(payload["recipients"]),
        },
    )
    return {
        "status": "sent" if sent_audited else "sent_audit_failed",
        "signal_type": payload["signal_type"],
        "request_number": payload["request_number"],
    }


def _record_persistent_failure(
    payload: Mapping[str, object],
    failure: gmail_client.GmailUnavailable,
) -> dict[str, object]:
    _safe_audit_payload(
        "staff_notification.failed",
        payload,
        failure_category=failure.category,
        http_status=failure.status_code,
    )
    logger.error(
        "Internal staff notification permanently failed after retries",
        extra={
            "request": payload["request_number"],
            "staff_notification_signal": payload["signal_type"],
            "gmail_failure_category": failure.category,
        },
    )
    return {
        "status": "failed",
        "signal_type": payload["signal_type"],
        "request_number": payload["request_number"],
    }


def _record_internal_failure(
    payload: Mapping[str, object],
    exc: BaseException,
) -> dict[str, object]:
    _safe_audit_payload(
        "staff_notification.failed",
        payload,
        failure_category="internal_notification_error",
    )
    logger.error(
        "Internal staff notification failed before Gmail accepted it",
        extra={
            "request": payload["request_number"],
            "staff_notification_signal": payload["signal_type"],
        },
        exc_info=exc,
    )
    return {
        "status": "failed",
        "signal_type": payload["signal_type"],
        "request_number": payload["request_number"],
    }


@shared_task(bind=True, max_retries=5)
def deliver_staff_notification(self, payload: dict[str, object]) -> dict[str, object]:
    """Send one internal notification with bounded exponential retry."""
    blocked = _block_notification(payload, release_dedupe=True)
    if blocked is not None:
        return blocked
    try:
        return _send_notification(payload)
    except gmail_client.GmailUnavailable as exc:
        logger.warning(
            "Internal staff notification Gmail send failed",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
                "gmail_failure_category": exc.category,
                "retry_number": self.request.retries,
            },
        )
        if self.request.retries >= self.max_retries:
            return _record_persistent_failure(payload, exc)
        try:
            raise self.retry(
                exc=exc,
                countdown=min(60 * (2 ** self.request.retries), 900),
            )
        except MaxRetriesExceededError:
            return _record_persistent_failure(payload, exc)
    except Exception as exc:  # noqa: BLE001 - an internal fault is recorded, not raised
        return _record_internal_failure(payload, exc)


def _schedule_payload(
    payload: dict[str, object],
    *,
    immediate: bool,
) -> bool | None:
    key = _dedupe_key(payload)
    try:
        claimed = cache.add(
            key,
            True,
            timeout=runtime.get("PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS"),
        )
    except Exception:
        logger.warning(
            "Internal staff notification dedupe store unavailable",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
            },
            exc_info=True,
        )
        return False
    if not claimed:
        logger.info(
            "Duplicate internal staff notification suppressed",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
            },
        )
        return None

    if immediate:
        try:
            result = _send_notification(payload)
            if result["status"] != "audit_failed":
                return True
            _safe_cache_delete(key, payload)
            return False
        except gmail_client.GmailUnavailable as exc:
            logger.warning(
                "Immediate internal staff notification failed; queueing retry",
                extra={
                    "request": payload["request_number"],
                    "staff_notification_signal": payload["signal_type"],
                    "gmail_failure_category": exc.category,
                },
            )
        except Exception as exc:  # noqa: BLE001 - an internal fault is recorded, not raised
            _safe_cache_delete(key, payload)
            _record_internal_failure(payload, exc)
            return False

    try:
        if not _safe_audit_payload("staff_notification.queued", payload):
            _safe_cache_delete(key, payload)
            return False
        deliver_staff_notification.delay(payload)
    except Exception:
        _safe_cache_delete(key, payload)
        _safe_audit_payload("staff_notification.enqueue_failed", payload)
        logger.warning(
            "Internal staff notification could not be queued",
            extra={
                "request": payload["request_number"],
                "staff_notification_signal": payload["signal_type"],
            },
            exc_info=True,
        )
        return False
    return True


def enqueue_staff_notification(
    *,
    signal_type: str,
    request,
    recipients: Iterable[str],
    subject: str,
    template_name: str,
    context: Mapping[str, object],
    condition_key: str = "",
    immediate: bool = False,
) -> bool | None:
    """Return true for delivery, false for failure/blocking, or none for dedupe."""
    try:
        payload = build_notification(
            signal_type=signal_type,
            request=request,
            recipients=recipients,
            subject=subject,
            template_name=template_name,
            context=context,
            condition_key=condition_key,
        )
    except Exception:
        logger.warning(
            "Internal staff notification could not be prepared",
            extra={
                "request": getattr(request, "number", ""),
                "staff_notification_signal": signal_type,
            },
            exc_info=True,
        )
        return False
    if _block_notification(payload, release_dedupe=False) is not None:
        return False
    return _schedule_payload(payload, immediate=immediate)


def notify_deadline(
    request,
    *,
    level: str,
    include_supervisor: bool,
) -> bool | None:
    signal_type = (
        "deadline.warning" if level == "WARNING" else "deadline.critical"
    )
    return enqueue_staff_notification(
        signal_type=signal_type,
        request=request,
        recipients=_records_team_recipients(include_supervisor=include_supervisor),
        subject=(
            f"[{level}] {request.number} statutory response due "
            f"{request.statutory_response_due}"
        ),
        template_name="notifications/deadline.txt",
        context={
            "level": level,
            "statutory_response_due": request.statutory_response_due,
        },
        condition_key=f"{level}:{request.statutory_response_due}",
    )


def notify_estimate(request, *, approaching: bool) -> bool | None:
    signal_type = "estimate.approaching" if approaching else "estimate.overdue"
    level = "ESTIMATE APPROACHING" if approaching else "ESTIMATE OVERDUE"
    return enqueue_staff_notification(
        signal_type=signal_type,
        request=request,
        recipients=_records_team_recipients(include_supervisor=not approaching),
        subject=f"[{level}] {request.number}: {request.estimated_completion}",
        template_name="notifications/estimate.txt",
        context={
            "level": level,
            "estimated_completion": request.estimated_completion,
        },
        condition_key=str(request.estimated_completion),
    )


def notify_stalled(
    request,
    *,
    last_activity,
    revision_count: int,
) -> bool | None:
    return enqueue_staff_notification(
        signal_type="request.stalled",
        request=request,
        recipients=_records_team_recipients(include_supervisor=True),
        subject=f"[STALLED] {request.number}: no substantive activity",
        template_name="notifications/stalled.txt",
        context={
            "last_activity": last_activity,
            "stalled_days": (timezone.now() - last_activity).days,
            "revision_count": revision_count,
            "stall_threshold_days": runtime.get("STALL_THRESHOLD_DAYS"),
        },
        condition_key=last_activity.isoformat(),
    )


def _footage_sites(codes) -> str:
    from .footage_sites import site_name

    return ", ".join(f"{site_name(code)} ({code})" for code in codes)


def notify_footage_manual_hold(
    preservation,
    *,
    error: str,
    site_codes=None,
) -> bool | None:
    """URGENT: a human must bookmark footage in Aware by hand. ``site_codes``
    scopes the instructions; empty means every camera district-wide."""
    request = preservation.request
    if site_codes is None:
        site_codes = preservation.site_codes
    return enqueue_staff_notification(
        signal_type="footage.manual_hold_required",
        request=request,
        recipients=(runtime.get("FACILITIES_EMAIL"), runtime.get("PRA_SUPERVISOR_EMAIL")),
        subject=(
            f"[URGENT — MANUAL HOLD] {request.number}: preserve camera footage NOW"
        ),
        template_name="notifications/footage_manual_hold.txt",
        context={
            "window_start": preservation.window_start,
            "window_end": preservation.window_end,
            "camera_retention_days": runtime.get("CAMERA_RETENTION_DAYS"),
            "failure": str(error)[:500],
            "sites": _footage_sites(site_codes or ()),
            "held_count": preservation.held_count,
        },
        condition_key=(
            f"{preservation.pk}:{preservation.window_start.isoformat()}:"
            f"{preservation.window_end.isoformat()}"
        ),
        immediate=True,
    )


def notify_footage_hold_complete(
    preservation,
    *,
    held: int,
    gap: int = 0,
    failed: int = 0,
    not_retained: int = 0,
) -> bool | None:
    request = preservation.request
    short = bool(gap or failed or not_retained)
    recipients = list(_records_officer_recipients())
    if short:
        recipients.append(runtime.get("PRA_SUPERVISOR_EMAIL"))
    return enqueue_staff_notification(
        signal_type="footage.hold_complete",
        request=request,
        recipients=recipients,
        subject=(
            f"[FOOTAGE HOLD {'PARTIAL' if short else 'COMPLETE'}] "
            f"{request.number}: {held} held, {gap} gap, {failed} failed, "
            f"{not_retained} not retained"
        ),
        template_name="notifications/footage_hold_complete.txt",
        context={
            "held_count": held,
            "gap_count": gap,
            "failed_count": failed,
            "not_retained_count": not_retained,
            "sites": _footage_sites(preservation.site_codes),
        },
        condition_key=f"{preservation.pk}:{held}:{gap}:{failed}:{not_retained}",
    )


def notify_intake_auto_applied(applied) -> bool | None:
    """Tell Records Officers the system closed a case on its own (#34)."""
    request = applied.request
    label = "SPAM" if applied.label == "spam" else "NOT A REQUEST"
    return enqueue_staff_notification(
        signal_type="intake.auto_applied",
        request=request,
        recipients=_records_officer_recipients(),
        subject=f"[AUTO-CLOSED {label}] {request.number}",
        template_name="notifications/intake_auto_applied.txt",
        context={
            "label": applied.label,
            "confidence_percent": round(applied.confidence * 100),
        },
        condition_key=str(applied.pk),
    )


def notify_adequate_search_review(
    preservation,
    *,
    no_camera_sites=(),
    note: str = "",
) -> bool | None:
    """Queue the human adequate-search review: either nothing is retained
    for the window, or a named site has no cameras in Aware."""
    request = preservation.request
    no_camera_sites = list(no_camera_sites)
    return enqueue_staff_notification(
        signal_type="footage.adequate_search_review_queued",
        request=request,
        recipients=_records_officer_recipients(),
        subject=f"[FOOTAGE REVIEW REQUIRED] {request.number}: adequate search",
        template_name="notifications/footage_adequate_search.txt",
        context={
            "window_start": preservation.window_start,
            "window_end": preservation.window_end,
            # The window ends at midnight after the last requested day.
            "window_last_day": preservation.window_end - timezone.timedelta(microseconds=1),
            "camera_retention_days": runtime.get("CAMERA_RETENTION_DAYS"),
            "no_camera_sites": _footage_sites(no_camera_sites),
            "note": str(note)[:500],
        },
        condition_key=(
            f"{preservation.pk}:no-cameras:{','.join(no_camera_sites)}"
            if no_camera_sites else str(preservation.pk)
        ),
    )


def notify_delivery_effects_failed(
    request,
    *,
    correspondence_id: str,
    correspondence_kind: str,
    error: str,
) -> bool | None:
    return enqueue_staff_notification(
        signal_type="correspondence.delivery_effects_failed",
        request=request,
        recipients=_records_team_recipients(include_supervisor=True),
        subject=(
            f"[URGENT — DELIVERY EFFECT NOT APPLIED] {request.number}: "
            f"{correspondence_kind} delivered"
        ),
        template_name="notifications/delivery_effects_failed.txt",
        context={
            "correspondence_id": correspondence_id,
            "correspondence_kind": correspondence_kind,
            "failure": str(error)[:500],
        },
        condition_key=str(correspondence_id),
        immediate=True,
    )


def notify_collection_task(task) -> bool | None:
    request = task.request
    system = task.system
    recipients = []
    if system and system.custodian_email:
        recipients.append(system.custodian_email)
    else:
        recipients.extend(_records_officer_recipients())
    urgent = bool(system and system.urgent_hold)
    if urgent:
        recipients.append(runtime.get("PRA_SUPERVISOR_EMAIL"))
    return enqueue_staff_notification(
        signal_type="collection.task_created",
        request=request,
        recipients=recipients,
        subject=(
            f"[{'URGENT PRESERVATION' if urgent else 'COLLECTION TASK'}] "
            f"{request.number}: {system.name if system else 'records collection'}"
        ),
        template_name="notifications/collection_task.txt",
        context={
            "system_name": system.name if system else "Records collection",
            "retention_note": system.retention_note if system else "",
            "search_checklist": system.search_checklist if system else task.instructions,
            "urgent": urgent,
            "due_date": task.due_date,
        },
        condition_key=str(task.pk),
        immediate=urgent,
    )


def notify_inbound_mailbox(
    *,
    signal_type: str,
    subject: str,
    template_name: str,
    context: Mapping[str, object],
    condition_key: str,
    include_supervisor: bool = False,
    immediate: bool = False,
) -> bool | None:
    """Alert the records team about the intake mailbox rather than one case."""
    return enqueue_staff_notification(
        signal_type=signal_type,
        request=None,
        recipients=_records_team_recipients(include_supervisor=include_supervisor),
        subject=subject,
        template_name=template_name,
        context=context,
        condition_key=condition_key,
        immediate=immediate,
    )
