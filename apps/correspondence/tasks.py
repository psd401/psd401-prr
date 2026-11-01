"""Background jobs for correspondence attachment security."""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import time
import uuid

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Replace
from django.utils import timezone

from apps import secure_files
from apps.configuration import runtime
from apps.integrations.gmail import client as gmail_client

from . import gmail_sync, services, transport
from .gmail_observability import publish_inbound_poll_metrics
from .models import (
    CorrespondenceAttachment,
    CorrespondenceAttachmentQuarantineCleanup,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    InboundQuarantineWriteReservation,
)

logger = logging.getLogger(__name__)

_SCAN_UNAVAILABLE_REASON = "Malware scanning is temporarily unavailable."
_MALWARE_REASON = "Malware detected."
_CLEAN_REVALIDATION_UNAVAILABLE_REASON = (
    "Clean attachment revalidation failed; release was revoked."
)
_CLEAN_REVALIDATION_AUDIT_ACTION = (
    "correspondence_attachment.clean_revalidation_failed"
)
_CLEANUP_UNAVAILABLE_REASON = "Quarantine cleanup is temporarily unavailable."
_CLEANUP_LEASE_SECONDS = 15 * 60
_CORRESPONDENCE_QUARANTINE_PREFIX = (
    "quarantine/correspondence/attachments/"
)
_INBOUND_WRITE_CLEANUP_REASON = (
    "Inbound quarantine write cleanup is temporarily unavailable."
)


@shared_task
def poll_inbound_gmail() -> dict[str, object]:
    """Poll the configured mailbox into the durable inbound pipeline."""
    started_at = time.monotonic()
    try:
        result = gmail_sync.poll_inbound_mailbox()
    except Exception as exc:
        publish_inbound_poll_metrics(
            failure=exc,
            duration_seconds=time.monotonic() - started_at,
        )
        raise
    publish_inbound_poll_metrics(
        result=result,
        duration_seconds=time.monotonic() - started_at,
    )
    return result


@shared_task
def reconcile_inbound_gmail() -> dict[str, object]:
    """Receive any unprocessed mailbox message the history poll missed."""
    return gmail_sync.reconcile_inbound_mailbox()


def _delivery_enqueue_key(delivery_id: str, attempt_count: int) -> str:
    return (
        "correspondence:queued-delivery:"
        f"{delivery_id}:attempt-count:{attempt_count}"
    )


def _cleanup_delivery_enqueue_marker(key: str, *, delivery_id: str) -> None:
    try:
        cache.delete(key)
    except Exception:
        logger.warning(
            "Correspondence delivery dedupe marker cleanup failed",
            extra={"delivery": delivery_id},
            exc_info=True,
        )


def enqueue_queued_delivery(
    delivery_id: str,
    *,
    attempt_count: int,
) -> bool:
    """Enqueue one delivery generation, suppressing duplicate nudges."""
    normalized_delivery_id = str(delivery_id)
    normalized_attempt_count = int(attempt_count)
    if normalized_attempt_count < 0:
        raise ValueError("attempt_count cannot be negative.")
    key = _delivery_enqueue_key(
        normalized_delivery_id,
        normalized_attempt_count,
    )
    timeout = max(
        runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
        runtime.get("PRR_CORRESPONDENCE_LEASE_SECONDS"),
    )
    if not cache.add(key, True, timeout=timeout):
        return False
    try:
        send_queued_delivery.delay(
            normalized_delivery_id,
            attempt_count=normalized_attempt_count,
        )
    except Exception:
        _cleanup_delivery_enqueue_marker(
            key,
            delivery_id=normalized_delivery_id,
        )
        raise
    return True


def _provider_metadata(failure: gmail_client.GmailUnavailable) -> dict:
    if failure.status_code is None:
        return {}
    return {"http_status": failure.status_code}


def _report_failed_delivery(
    claim: services.DeliveryClaim,
    *,
    actor,
    category: str,
    provider_metadata: dict | None = None,
):
    return services.report_delivery_result(
        claim.delivery,
        actor=actor,
        attempt_id=claim.attempt.id,
        outcome="failed",
        failure_category=category,
        provider_metadata=provider_metadata or {},
    )


def _report_unknown_reconciliation(
    claim: services.DeliveryClaim,
    *,
    actor,
):
    return services.report_delivery_result(
        claim.delivery,
        actor=actor,
        attempt_id=claim.attempt.id,
        outcome="unknown",
    )


def _task_result(claim: services.DeliveryClaim, result) -> dict[str, object]:
    return {
        "delivery_id": str(claim.delivery.id),
        "attempt_id": str(claim.attempt.id),
        "attempt_number": claim.attempt.attempt_number,
        "action": claim.action,
        "status": result.status,
    }


@shared_task
def send_queued_delivery(
    delivery_id: str,
    *,
    attempt_count: int,
) -> dict[str, object]:
    """Claim and execute one frozen Gmail send or reconciliation action."""
    normalized_delivery_id = str(delivery_id)
    normalized_attempt_count = int(attempt_count)
    key = _delivery_enqueue_key(
        normalized_delivery_id,
        normalized_attempt_count,
    )
    try:
        snapshot = (
            CorrespondenceDelivery.objects.filter(pk=normalized_delivery_id)
            .values("attempt_count", "status", "correspondence__automated")
            .first()
        )
        if snapshot is None:
            return {
                "delivery_id": normalized_delivery_id,
                "status": "missing",
            }
        if snapshot["attempt_count"] != normalized_attempt_count:
            return {
                "delivery_id": normalized_delivery_id,
                "status": "stale",
            }
        if not runtime.get("PRR_GMAIL_ENABLED"):
            logger.warning(
                "Gmail delivery task stopped by the global Gmail switch",
                extra={"delivery": normalized_delivery_id},
            )
            return {
                "delivery_id": normalized_delivery_id,
                "status": "gmail_disabled",
            }
        if (
            snapshot["correspondence__automated"]
            and not runtime.get("PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED")
        ):
            logger.warning(
                "Automated requester delivery stopped by the manual-only switch",
                extra={"delivery": normalized_delivery_id},
            )
            return {
                "delivery_id": normalized_delivery_id,
                "status": "automated_outbound_disabled",
            }

        actor = transport.system_transport_principal()
        try:
            claim = services.claim_delivery(
                normalized_delivery_id,
                actor=actor,
            )
        except (services.DeliveryClaimConflict, services.DeliveryNotClaimable) as exc:
            logger.info(
                "Correspondence delivery task found no claimable work",
                extra={
                    "delivery": normalized_delivery_id,
                    "delivery_contract_code": exc.code,
                },
            )
            current_status = (
                CorrespondenceDelivery.objects.filter(pk=normalized_delivery_id)
                .values_list("status", flat=True)
                .first()
            )
            return {
                "delivery_id": normalized_delivery_id,
                "status": current_status or "missing",
            }

        if claim.action == CorrespondenceDeliveryAttempt.Action.RECONCILE:
            message_id = claim.payload["reconciliation"]["message_id"]
            try:
                found = gmail_client.find_by_rfc822_message_id(message_id)
            except gmail_client.GmailUnavailable:
                logger.warning(
                    "Gmail reconciliation was unavailable",
                    extra={"delivery": normalized_delivery_id},
                )
                result = _report_unknown_reconciliation(claim, actor=actor)
                return _task_result(claim, result)
            if found is None:
                result = services.report_delivery_result(
                    claim.delivery,
                    actor=actor,
                    attempt_id=claim.attempt.id,
                    outcome="not_found",
                )
                return _task_result(claim, result)
            found_at = timezone.now()
            result = services.report_delivery_result(
                claim.delivery,
                actor=actor,
                attempt_id=claim.attempt.id,
                outcome="reconciled",
                provider_message_id=found.message_id,
                provider_thread_id=found.thread_id,
                provider_timestamp=found_at,
                now=found_at,
            )
            return _task_result(claim, result)

        if settings.PRR_ENVIRONMENT != "production":
            allowlist = {
                address.lower()
                for address in runtime.get("PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST")
            }
            recipients = transport.delivery_recipient_addresses(claim)
            if any(address not in allowlist for address in recipients):
                logger.warning(
                    "Development correspondence delivery blocked by allowlist",
                    extra={
                        "delivery": normalized_delivery_id,
                        "recipient_count": len(recipients),
                    },
                )
                result = _report_failed_delivery(
                    claim,
                    actor=actor,
                    category="development_recipient_blocked",
                )
                return _task_result(claim, result)

        try:
            raw_message = transport.build_delivery_message(claim, actor=actor)
        except services.DeliveryAttachmentStorageUnavailable:
            result = _report_failed_delivery(
                claim,
                actor=actor,
                category="provider_unavailable",
            )
            return _task_result(claim, result)
        except services.DeliveryAttachmentNotAvailable:
            result = _report_failed_delivery(
                claim,
                actor=actor,
                category="attachment_rejected",
            )
            return _task_result(claim, result)
        except ValidationError:
            result = _report_failed_delivery(
                claim,
                actor=actor,
                category="message_rejected",
            )
            return _task_result(claim, result)

        try:
            sent = gmail_client.send_raw(raw_message)
        except gmail_client.GmailUnavailable as exc:
            result = _report_failed_delivery(
                claim,
                actor=actor,
                category=exc.category,
                provider_metadata=_provider_metadata(exc),
            )
            return _task_result(claim, result)

        sent_at = timezone.now()
        result = services.report_delivery_result(
            claim.delivery,
            actor=actor,
            attempt_id=claim.attempt.id,
            outcome="sent",
            provider_message_id=sent.message_id,
            provider_thread_id=sent.thread_id,
            provider_timestamp=sent_at,
            now=sent_at,
        )
        return _task_result(claim, result)
    finally:
        _cleanup_delivery_enqueue_marker(
            key,
            delivery_id=normalized_delivery_id,
        )


@shared_task
def watch_stuck_correspondence_deliveries(limit: int = 100) -> dict[str, int]:
    """Finalize expired leases and nudge the same durable delivery."""
    result = services.watch_stuck_deliveries(limit=limit)
    return {
        "expired_claims": result.expired_claims,
        "nudged_deliveries": result.nudged_deliveries,
        "suppressed_nudges": result.suppressed_nudges,
        "notification_failures": result.notification_failures,
    }


def _clean_storage_key(attachment: CorrespondenceAttachment) -> str:
    return (
        f"correspondence/attachments/{attachment.correspondence_id}/"
        f"{attachment.id}"
    )


def _audit_scan(attachment: CorrespondenceAttachment, action: str) -> None:
    from apps.audit.models import audit

    request = attachment.correspondence.request
    audit(
        action,
        target=request.number if request else str(attachment.correspondence_id),
        correspondence_id=str(attachment.correspondence_id),
        attachment_id=str(attachment.id),
        sha256=attachment.sha256,
    )


def _enqueue_quarantine_cleanup_after_commit(
    cleanup_id: str,
    *,
    attachment_id: str,
    correspondence_id: str,
) -> None:
    """Enqueue durable cleanup without making a committed verdict fail."""
    if not _claim_and_enqueue_quarantine_cleanup(cleanup_id):
        logger.error(
            "Could not enqueue correspondence attachment quarantine cleanup",
            extra={
                "attachment": attachment_id,
                "correspondence": correspondence_id,
            },
        )


def _finalize_scan_with_audit(
    attachment: CorrespondenceAttachment,
    *,
    source_key: str,
    scan_status: str,
    scanned_at,
    action: str,
    quarantine_reason: str = "",
    promoted_storage_key: str | None = None,
    delete_quarantine: bool = False,
) -> CorrespondenceAttachment:
    """Commit one scan verdict and its legal audit evidence together."""
    with transaction.atomic():
        finalized = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=source_key,
            scan_status=scan_status,
            scanned_at=scanned_at,
            quarantine_reason=quarantine_reason,
            promoted_storage_key=promoted_storage_key,
        )
        _audit_scan(finalized, action)
        if delete_quarantine:
            cleanup = (
                CorrespondenceAttachmentQuarantineCleanup.objects.create(
                    attachment=finalized,
                    source_storage_key=source_key,
                )
            )
            transaction.on_commit(
                lambda: _enqueue_quarantine_cleanup_after_commit(
                    str(cleanup.id),
                    attachment_id=str(attachment.id),
                    correspondence_id=str(attachment.correspondence_id),
                )
            )
    return finalized


def _finalize_infected_with_audit(
    attachment: CorrespondenceAttachment,
    *,
    source_key: str | None,
    scanned_at,
) -> CorrespondenceAttachment:
    """Reconcile a monotonic positive verdict with audit and cleanup evidence."""
    with transaction.atomic():
        finalized = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_infected_verdict_locked(
            expected_size=attachment.size,
            expected_sha256=attachment.sha256,
            scanned_at=scanned_at,
            quarantine_reason=_MALWARE_REASON,
        )
        if not _infected_audit_exists(finalized.pk):
            _audit_scan(
                finalized,
                "correspondence_attachment.scan_infected",
            )
        cleanup = (
            CorrespondenceAttachmentQuarantineCleanup.objects.select_for_update()
            .filter(attachment=finalized)
            .first()
        )
        if cleanup is None:
            if source_key is not None:
                if not source_key.startswith(
                    _CORRESPONDENCE_QUARANTINE_PREFIX
                ):
                    raise ValidationError(
                        "Positive malware cleanup requires a generated "
                        "quarantine storage key."
                    )
                cleanup = (
                    CorrespondenceAttachmentQuarantineCleanup.objects.create(
                        attachment=finalized,
                        source_storage_key=source_key,
                    )
                )
        elif (
            source_key is not None
            and cleanup.source_storage_key != source_key
        ):
            raise ValidationError(
                "Attachment quarantine cleanup evidence does not match the "
                "positive malware verdict."
            )
        if cleanup is not None and cleanup.completed_at is None:
            transaction.on_commit(
                lambda: _enqueue_quarantine_cleanup_after_commit(
                    str(cleanup.id),
                    attachment_id=str(attachment.id),
                    correspondence_id=str(attachment.correspondence_id),
                )
            )
    return finalized


def _infected_audit_exists(attachment_id) -> bool:
    return _scan_audit_exists(
        attachment_id,
        "correspondence_attachment.scan_infected",
    )


def _scan_audit_exists(attachment_id, action: str) -> bool:
    from apps.audit.models import AuditLog

    return AuditLog.objects.filter(
        action=action,
        detail__attachment_id=str(attachment_id),
    ).exists()


def _positive_verdict_source_key(
    attachment: CorrespondenceAttachment,
) -> str | None:
    cleanup = (
        CorrespondenceAttachmentQuarantineCleanup.objects.filter(
            attachment_id=attachment.pk
        )
        .only("source_storage_key")
        .first()
    )
    if cleanup is not None:
        return cleanup.source_storage_key
    if attachment.storage_key.startswith(
        _CORRESPONDENCE_QUARANTINE_PREFIX
    ):
        return attachment.storage_key
    return None


def _persist_positive_safety_barrier(
    attachment: CorrespondenceAttachment,
    *,
    source_key: str | None,
    scanned_at,
) -> CorrespondenceAttachment:
    """Commit a non-downloadable verdict before fallible audit reconciliation.

    A confirmed malware verdict must survive a worker retry even when a
    competing scanner already promoted the blob and the append-only audit
    write is temporarily unavailable.  The cleanup row preserves the original
    quarantine identity across that retry.  No deletion is enqueued until the
    audit reconciliation succeeds.
    """
    with transaction.atomic():
        finalized = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_infected_verdict_locked(
            expected_size=attachment.size,
            expected_sha256=attachment.sha256,
            scanned_at=scanned_at,
            quarantine_reason=_MALWARE_REASON,
        )
        cleanup = (
            CorrespondenceAttachmentQuarantineCleanup.objects.select_for_update()
            .filter(attachment=finalized)
            .first()
        )
        if cleanup is None and source_key is not None:
            if not source_key.startswith(
                _CORRESPONDENCE_QUARANTINE_PREFIX
            ):
                raise ValidationError(
                    "Positive malware cleanup requires a generated quarantine "
                    "storage key."
                )
            CorrespondenceAttachmentQuarantineCleanup.objects.create(
                attachment=finalized,
                source_storage_key=source_key,
            )
        elif (
            cleanup is not None
            and source_key is not None
            and cleanup.source_storage_key != source_key
        ):
            raise ValidationError(
                "Attachment quarantine cleanup evidence does not match the "
                "positive malware verdict."
            )
    return finalized


def _persist_clean_revalidation_failure_barrier(
    attachment: CorrespondenceAttachment,
    *,
    scanned_at,
) -> CorrespondenceAttachment:
    """Durably revoke release when a worker cannot revalidate clean bytes."""
    try:
        return CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).requarantine_locked(
            quarantine_reason=_CLEAN_REVALIDATION_UNAVAILABLE_REASON,
            quarantined_at=scanned_at,
        )
    except ValidationError:
        current = CorrespondenceAttachment.objects.select_related(
            "correspondence__request"
        ).get(pk=attachment.pk)
        if current.quarantined and current.scan_status in {
            CorrespondenceAttachment.ScanStatus.ERROR,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        }:
            return current
        raise


def _reconcile_clean_revalidation_failure_audit(
    attachment: CorrespondenceAttachment,
) -> CorrespondenceAttachment:
    """Append missing audit evidence for a durable revalidation barrier."""
    with transaction.atomic():
        queryset = CorrespondenceAttachment.objects.filter(pk=attachment.pk)
        correspondence_id = queryset.values_list(
            "correspondence_id",
            flat=True,
        ).get()
        queryset._lock_correspondences({correspondence_id})
        current = (
            queryset.select_for_update(of=("self",))
            .select_related("correspondence__request")
            .get()
        )
        if (
            current.scan_status
            != CorrespondenceAttachment.ScanStatus.ERROR
            or not current.quarantined
            or current.quarantine_reason
            != _CLEAN_REVALIDATION_UNAVAILABLE_REASON
        ):
            return current
        if not _scan_audit_exists(
            current.pk,
            _CLEAN_REVALIDATION_AUDIT_ACTION,
        ):
            _audit_scan(
                current,
                _CLEAN_REVALIDATION_AUDIT_ACTION,
            )
        return current


def _retry_attachment_scan(
    task,
    *,
    attachment: CorrespondenceAttachment,
    confirmed_positive: bool,
):
    logger.warning(
        "Correspondence attachment scan failed closed",
        extra={
            "attachment": str(attachment.id),
            "correspondence": str(attachment.correspondence_id),
        },
    )
    retry_kwargs = (
        {"confirmed_positive": True} if confirmed_positive else None
    )
    sanitized_error = secure_files.AttachmentScanUnavailable(
        _SCAN_UNAVAILABLE_REASON
    )
    retry_options = {
        "exc": sanitized_error,
        "countdown": min(
            60 * (2 ** min(task.request.retries, 4)),
            900,
        ),
    }
    if retry_kwargs is not None:
        retry_options["kwargs"] = retry_kwargs
    raise task.retry(**retry_options)


def _synced_intake_attachment_status(
    attachment: CorrespondenceAttachment,
) -> str:
    """Mirror a final email-attachment verdict before acknowledging the task."""
    from apps.records.email_intake import sync_intake_attachment_verdict

    current = CorrespondenceAttachment.objects.get(pk=attachment.pk)
    sync_intake_attachment_verdict(current)
    return current.scan_status


@shared_task(
    bind=True,
    max_retries=None,
    acks_late=True,
    acks_on_failure_or_timeout=False,
    reject_on_worker_lost=True,
)
def scan_correspondence_attachment(
    self,
    attachment_id: str,
    confirmed_positive: bool = False,
):
    """Scan and promote one correspondence attachment, failing closed."""
    attachment = (
        CorrespondenceAttachment.objects.select_related(
            "correspondence__request"
        )
        .get(pk=attachment_id)
    )
    if (
        attachment.scan_status
        == CorrespondenceAttachment.ScanStatus.INFECTED
    ):
        try:
            if _infected_audit_exists(attachment.pk):
                return _synced_intake_attachment_status(attachment)
        except Exception:  # noqa: BLE001 - a scanner fault retries, never releases
            _retry_attachment_scan(
                self,
                attachment=attachment,
                confirmed_positive=True,
            )
        # A prior attempt committed the non-downloadable safety barrier but
        # could not append its audit row.  Reconcile without rescanning.
        confirmed_positive = True
    released_clean = (
        attachment.scan_status == CorrespondenceAttachment.ScanStatus.CLEAN
    )
    if (
        attachment.scan_status == CorrespondenceAttachment.ScanStatus.ERROR
        and attachment.storage_key == _clean_storage_key(attachment)
        and not confirmed_positive
    ):
        if (
            attachment.quarantine_reason
            == _CLEAN_REVALIDATION_UNAVAILABLE_REASON
        ):
            try:
                attachment = _reconcile_clean_revalidation_failure_audit(
                    attachment
                )
            except Exception:  # noqa: BLE001 - a scanner fault retries, never releases
                _retry_attachment_scan(
                    self,
                    attachment=attachment,
                    confirmed_positive=False,
                )
        # ERROR on the promoted key is an explicit later security revocation,
        # not a transient scanner outage. It is terminal until a human uses a
        # separate reviewed recovery workflow.
        return _synced_intake_attachment_status(attachment)

    if confirmed_positive:
        source_key = None
        try:
            source_key = _positive_verdict_source_key(attachment)
            now = timezone.now()
            attachment = _persist_positive_safety_barrier(
                attachment,
                source_key=source_key,
                scanned_at=now,
            )
            attachment = _finalize_infected_with_audit(
                attachment,
                source_key=source_key,
                scanned_at=now,
            )
        except Exception:  # noqa: BLE001 - a scanner fault retries, never releases
            with contextlib.suppress(Exception):
                attachment = _persist_positive_safety_barrier(
                    attachment,
                    source_key=source_key,
                    scanned_at=timezone.now(),
                )
            _retry_attachment_scan(
                self,
                attachment=attachment,
                confirmed_positive=True,
            )
        return _synced_intake_attachment_status(attachment)

    source_key = attachment.storage_key
    positive_source_key = None if released_clean else source_key
    positive_verdict = False
    try:
        clean, _scanner_detail = secure_files.scan_object_for_malware(
            source_key,
            expected_size=attachment.size,
            expected_sha256=attachment.sha256,
            max_bytes=runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES"),
        )
        now = timezone.now()
        positive_verdict = not clean
        if clean:
            if released_clean:
                return _synced_intake_attachment_status(attachment)
            destination_key = _clean_storage_key(attachment)
            # Retain the quarantine object until the database verdict and its
            # audit row commit. A failed CAS/audit therefore remains retryable.
            secure_files.copy_clean_object(
                source_key,
                destination_key,
            )
            attachment = _finalize_scan_with_audit(
                attachment,
                source_key=source_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=now,
                promoted_storage_key=destination_key,
                action="correspondence_attachment.scan_clean",
                delete_quarantine=True,
            )
        else:
            positive_source_key = _positive_verdict_source_key(attachment)
            # Commit the restrictive verdict and original quarantine identity
            # before the audit write.  A fresh retry can then repair missing
            # append-only evidence without ever restoring download access.
            attachment = _persist_positive_safety_barrier(
                attachment,
                source_key=positive_source_key,
                scanned_at=now,
            )
            attachment = _finalize_infected_with_audit(
                attachment,
                source_key=positive_source_key,
                scanned_at=now,
            )
    except Exception:  # noqa: BLE001 - a scanner fault retries, never releases
        if positive_verdict:
            try:
                attachment = _persist_positive_safety_barrier(
                    attachment,
                    source_key=positive_source_key,
                    scanned_at=timezone.now(),
                )
            except Exception:  # noqa: BLE001 - the retry marker carries the verdict
                # The trusted retry marker carries the confirmed verdict if a
                # transient database failure also prevented this barrier.
                pass
            _retry_attachment_scan(
                self,
                attachment=attachment,
                confirmed_positive=True,
            )
        if released_clean:
            try:
                attachment = _persist_clean_revalidation_failure_barrier(
                    attachment,
                    scanned_at=timezone.now(),
                )
                attachment = _reconcile_clean_revalidation_failure_audit(
                    attachment
                )
            except Exception:  # noqa: BLE001 - a scanner fault retries, never releases
                _retry_attachment_scan(
                    self,
                    attachment=attachment,
                    confirmed_positive=False,
                )
            return _synced_intake_attachment_status(attachment)
        try:
            _finalize_scan_with_audit(
                attachment,
                source_key=source_key,
                scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
                scanned_at=timezone.now(),
                quarantine_reason=_SCAN_UNAVAILABLE_REASON,
                action="correspondence_attachment.scan_error",
            )
        except ValidationError:
            try:
                current = CorrespondenceAttachment.objects.only(
                    "scan_status"
                ).get(pk=attachment.pk)
            except Exception:  # noqa: BLE001 - an unreadable row retries, never releases
                current = None
            if current is not None:
                if current.scan_status == CorrespondenceAttachment.ScanStatus.INFECTED:
                    return _synced_intake_attachment_status(current)
                if (
                    current.scan_status
                    == CorrespondenceAttachment.ScanStatus.CLEAN
                    and not positive_verdict
                ):
                    return _synced_intake_attachment_status(current)
        except Exception:  # noqa: BLE001 - the retry stays fail-closed
            # The retry remains fail-closed even if the database cannot record
            # this attempt. No unscanned attachment is made downloadable.
            pass
        _retry_attachment_scan(
            self,
            attachment=attachment,
            confirmed_positive=False,
        )

    return _synced_intake_attachment_status(attachment)


def _claim_and_enqueue_quarantine_cleanup(
    cleanup_id: str,
    *,
    now=None,
) -> bool:
    claimed_at = now or timezone.now()
    try:
        cleanup = (
            CorrespondenceAttachmentQuarantineCleanup.objects.claim_due(
                cleanup_id=cleanup_id,
                now=claimed_at,
                lease_expires_at=(
                    claimed_at
                    + dt.timedelta(seconds=_CLEANUP_LEASE_SECONDS)
                ),
            )
        )
    except CorrespondenceAttachmentQuarantineCleanup.DoesNotExist:
        return False
    if cleanup is None:
        return False
    try:
        cleanup_correspondence_attachment_quarantine.delay(
            str(cleanup.id),
            str(cleanup.lease_token),
        )
    except Exception:  # noqa: BLE001 — release for the next durable sweep
        failed_at = max(timezone.now(), claimed_at)
        try:
            (
                CorrespondenceAttachmentQuarantineCleanup.objects.release_claim(
                    cleanup_id=cleanup.id,
                    lease_token=cleanup.lease_token,
                    next_attempt_at=failed_at + dt.timedelta(seconds=60),
                )
            )
        except Exception:  # noqa: BLE001 — lease expiry remains a recovery path
            pass
        return False
    return True


@shared_task
def cleanup_correspondence_attachment_quarantine(
    cleanup_id: str,
    lease_token: str,
):
    """Delete one finalized quarantine object with durable retry evidence."""
    cleanup = (
        CorrespondenceAttachmentQuarantineCleanup.objects.select_related(
            "attachment__correspondence__request"
        )
        .get(pk=cleanup_id)
    )
    if cleanup.completed_at is not None:
        return "deleted"
    try:
        expected_lease_token = uuid.UUID(str(lease_token))
    except (TypeError, ValueError):
        return "deferred"
    if cleanup.lease_token != expected_lease_token:
        return "deferred"

    attempted_at = timezone.now()
    try:
        secure_files.delete_quarantined_object(cleanup.source_storage_key)
        with transaction.atomic():
            completed = (
                CorrespondenceAttachmentQuarantineCleanup.objects.complete_locked(
                    cleanup_id=cleanup.pk,
                    lease_token=expected_lease_token,
                    completed_at=attempted_at,
                )
            )
            if completed:
                _audit_scan(
                    cleanup.attachment,
                    "correspondence_attachment.quarantine_deleted",
                )
    except Exception:  # noqa: BLE001 — retry with a sanitized exception
        failed_at = timezone.now()
        retry_seconds = min(
            60 * (2 ** min(cleanup.attempt_count, 4)),
            _CLEANUP_LEASE_SECONDS,
        )
        try:
            (
                CorrespondenceAttachmentQuarantineCleanup.objects
                .record_failure_locked(
                    cleanup_id=cleanup.id,
                    lease_token=expected_lease_token,
                    attempted_at=failed_at,
                    next_attempt_at=(
                        failed_at + dt.timedelta(seconds=retry_seconds)
                    ),
                )
            )
        except Exception:  # noqa: BLE001 — the pending row remains recoverable
            pass
        logger.warning(
            "Correspondence attachment quarantine cleanup failed",
            extra={
                "attachment": str(cleanup.attachment_id),
                "correspondence": str(cleanup.attachment.correspondence_id),
            },
        )
        raise secure_files.AttachmentScanUnavailable(
            _CLEANUP_UNAVAILABLE_REASON
        ) from None

    if not completed:
        return "deferred"

    return "deleted"


@shared_task
def requeue_stale_correspondence_attachment_scans(limit: int = 100) -> int:
    """Recover bounded stale scan work from durable quarantine rows."""
    from apps.audit.models import AuditLog

    bounded_limit = max(1, min(int(limit), 1000))
    stale_before = timezone.now() - dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_SCAN_STALE_SECONDS")
    )
    attachment_id_text = Replace(
        KeyTextTransform("attachment_id", "detail"),
        Value("-"),
        Value(""),
        output_field=CharField(),
    )
    outer_attachment_id_text = Replace(
        Cast(
            OuterRef("pk"),
            output_field=CharField(),
        ),
        Value("-"),
        Value(""),
        output_field=CharField(),
    )
    infected_audits = (
        AuditLog.objects.filter(
            action="correspondence_attachment.scan_infected"
        )
        .annotate(attachment_id_text=attachment_id_text)
        .filter(
            attachment_id_text=outer_attachment_id_text
        )
    )
    clean_revalidation_audits = (
        AuditLog.objects.filter(
            action=_CLEAN_REVALIDATION_AUDIT_ACTION
        )
        .annotate(attachment_id_text=attachment_id_text)
        .filter(
            attachment_id_text=outer_attachment_id_text
        )
    )
    candidates = (
        CorrespondenceAttachment.objects.filter(quarantined=True)
        .annotate(
            has_infected_audit=Exists(infected_audits),
            has_clean_revalidation_audit=Exists(
                clean_revalidation_audits
            ),
        )
        .filter(
            Q(
                scan_status=CorrespondenceAttachment.ScanStatus.PENDING,
                created_at__lte=stale_before,
            )
            | Q(
                scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
                scanned_at__lte=stale_before,
                quarantine_reason=_SCAN_UNAVAILABLE_REASON,
            )
            | Q(
                scan_status=CorrespondenceAttachment.ScanStatus.INFECTED,
                has_infected_audit=False,
            )
            | Q(
                scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
                quarantine_reason=_CLEAN_REVALIDATION_UNAVAILABLE_REASON,
                has_clean_revalidation_audit=False,
            )
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )

    queued = 0
    for attachment_id in candidates:
        try:
            scan_correspondence_attachment.delay(str(attachment_id))
        except Exception:  # noqa: BLE001 — the durable row remains for next beat
            logger.error(
                "Could not requeue a stale correspondence attachment scan",
                extra={"attachment": str(attachment_id)},
            )
        else:
            queued += 1
    return queued


@shared_task
def requeue_pending_correspondence_attachment_cleanups(
    limit: int = 100,
) -> int:
    """Recover bounded quarantine deletion work from its durable ledger."""
    bounded_limit = max(1, min(int(limit), 1000))
    now = timezone.now()
    candidates = (
        CorrespondenceAttachmentQuarantineCleanup.objects.filter(
            completed_at__isnull=True,
            next_attempt_at__lte=now,
        )
        .filter(
            Q(lease_expires_at__isnull=True)
            | Q(lease_expires_at__lte=now)
        )
        .order_by("next_attempt_at", "created_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )

    queued = 0
    for cleanup_id in candidates:
        if not _claim_and_enqueue_quarantine_cleanup(
            str(cleanup_id),
            now=now,
        ):
            logger.error(
                "Could not requeue correspondence attachment quarantine cleanup",
                extra={"cleanup": str(cleanup_id)},
            )
            continue
        queued += 1
    return queued


def _claim_and_enqueue_inbound_quarantine_write(
    reservation_id: str,
    *,
    now=None,
) -> bool:
    claimed_at = now or timezone.now()
    stale_before = claimed_at - dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_INBOUND_RESERVATION_STALE_SECONDS")
    )
    try:
        reservation = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation_id,
            now=claimed_at,
            stale_before=stale_before,
            lease_expires_at=(
                claimed_at + dt.timedelta(seconds=_CLEANUP_LEASE_SECONDS)
            ),
        )
    except InboundQuarantineWriteReservation.DoesNotExist:
        return False
    if reservation is None:
        return False
    try:
        cleanup_inbound_quarantine_write.delay(
            str(reservation.pk),
            str(reservation.lease_token),
        )
    except Exception:  # noqa: BLE001 — durable sweep retries after release
        failed_at = max(timezone.now(), claimed_at)
        try:
            InboundQuarantineWriteReservation.objects.release_claim(
                reservation_id=reservation.pk,
                lease_token=reservation.lease_token,
                next_attempt_at=failed_at + dt.timedelta(seconds=60),
            )
        except Exception:  # noqa: BLE001 — lease expiry is a second recovery path
            pass
        return False
    return True


@shared_task
def cleanup_inbound_quarantine_write(
    reservation_id: str,
    lease_token: str,
):
    """Delete one stale, unadopted inbound write after exact reference checks."""
    reservation = InboundQuarantineWriteReservation.objects.get(
        pk=reservation_id
    )
    if reservation.adopted_at is not None:
        return "retained"
    if reservation.completed_at is not None:
        return "deleted"
    try:
        expected_lease_token = uuid.UUID(str(lease_token))
    except (TypeError, ValueError):
        return "deferred"
    if reservation.lease_token != expected_lease_token:
        return "deferred"

    attempted_at = timezone.now()
    with transaction.atomic():
        cleanup_state = (
            InboundQuarantineWriteReservation.objects
            .begin_cleanup_attempt_locked(
                reservation_id=reservation.pk,
                lease_token=expected_lease_token,
                attempted_at=attempted_at,
            )
        )
        if cleanup_state == "healed":
            from apps.audit.models import audit

            audit(
                "correspondence.inbound_quarantine_reservation_healed",
                target=str(reservation.pk),
                reservation_id=str(reservation.pk),
                kind=reservation.kind,
                size=reservation.size,
                sha256=reservation.sha256,
            )
    if cleanup_state in {"retained", "healed"}:
        return "retained"
    if cleanup_state == "deleted":
        return "deleted"
    if cleanup_state == "deferred":
        return "deferred"
    if cleanup_state == "conflict":
        InboundQuarantineWriteReservation.objects.release_claim(
            reservation_id=reservation.pk,
            lease_token=expected_lease_token,
            next_attempt_at=timezone.now()
            + dt.timedelta(seconds=_CLEANUP_LEASE_SECONDS),
        )
        logger.error(
            "Unadopted inbound reservation has a conflicting domain reference",
            extra={"reservation": str(reservation.pk)},
        )
        return "deferred"

    reservation.refresh_from_db(fields=["attempt_count"])
    try:
        secure_files.delete_quarantined_object(reservation.storage_key)
        with transaction.atomic():
            completed = (
                InboundQuarantineWriteReservation.objects.complete_locked(
                    reservation_id=reservation.pk,
                    lease_token=expected_lease_token,
                    completed_at=attempted_at,
                )
            )
            if completed:
                from apps.audit.models import audit

                audit(
                    "correspondence.inbound_quarantine_orphan_deleted",
                    target=str(reservation.pk),
                    reservation_id=str(reservation.pk),
                    kind=reservation.kind,
                    size=reservation.size,
                    sha256=reservation.sha256,
                )
    except Exception:  # noqa: BLE001 — preserve retry evidence, fail closed
        failed_at = timezone.now()
        retry_seconds = min(
            60 * (2 ** min(reservation.attempt_count, 4)),
            _CLEANUP_LEASE_SECONDS,
        )
        try:
            InboundQuarantineWriteReservation.objects.record_failure_locked(
                reservation_id=reservation.pk,
                lease_token=expected_lease_token,
                attempted_at=failed_at,
                next_attempt_at=failed_at
                + dt.timedelta(seconds=retry_seconds),
            )
        except Exception:  # noqa: BLE001 — lease expiry remains recoverable
            pass
        logger.warning(
            "Inbound quarantine orphan cleanup failed",
            extra={"reservation": str(reservation.pk)},
        )
        raise secure_files.AttachmentScanUnavailable(
            _INBOUND_WRITE_CLEANUP_REASON
        ) from None

    return "deleted" if completed else "deferred"


@shared_task
def requeue_stale_inbound_quarantine_writes(limit: int = 100) -> int:
    """Lease and enqueue a bounded batch of stale unadopted inbound writes."""
    bounded_limit = max(1, min(int(limit), 1000))
    now = timezone.now()
    stale_before = now - dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_INBOUND_RESERVATION_STALE_SECONDS")
    )
    candidates = (
        InboundQuarantineWriteReservation.objects.filter(
            adopted_at__isnull=True,
            completed_at__isnull=True,
            created_at__lte=stale_before,
            next_attempt_at__lte=now,
        )
        .filter(
            (
                Q(lease_expires_at__isnull=True)
                | Q(lease_expires_at__lte=now)
            ),
            (
                Q(processing_expires_at__isnull=True)
                | Q(processing_expires_at__lte=now)
            ),
        )
        .order_by("next_attempt_at", "created_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )

    queued = 0
    for reservation_id in candidates:
        if _claim_and_enqueue_inbound_quarantine_write(
            str(reservation_id),
            now=now,
        ):
            queued += 1
    return queued
