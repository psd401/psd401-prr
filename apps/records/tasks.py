"""
Background jobs. The deadline watchdog is the single most important piece of
Phase 1 — RCW 42.56.550 penalties accrue per day, so a missed 5-day
acknowledgment is the district's most expensive possible bug.
"""
import datetime as dt
import logging
import re
from importlib import import_module
from typing import NamedTuple

from celery import shared_task
from django.core.exceptions import ValidationError
from django.db.models import (
    CharField,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from apps.configuration import runtime

logger = logging.getLogger(__name__)
notifications = SimpleLazyObject(
    lambda: import_module("apps.records.notifications")
)

_VAULT_INGEST_ENQUEUED_ACTION = "vault.ingest.enqueued"
_ADVISORY_PAUSE_FAILURE_CODES = ("advisory_disabled", "automatic_disabled")


@shared_task
def execute_advisory_analysis(run_id: str):
    """Run one bounded advisory model call; no legal outcome is applied."""
    from .advisory_ai import _fail_run, execute_advisory_run

    try:
        execute_advisory_run(run_id)
    except Exception:
        _fail_run(run_id, code="worker_error")
        logger.exception(
            "Advisory analysis failed unexpectedly",
            extra={"advisory_run": str(run_id)},
        )
        raise


@shared_task
def continue_scoping(session_id: int) -> None:
    """Take the next scoping step after a count finishes (#36). Counts only."""
    from .scoping import continue_scoping as continue_session

    continue_session(session_id)


@shared_task
def prepare_estimate_letters(*, limit: int = 10) -> int:
    """Draft interpretation-and-estimate letters for ready cases (#42).

    Drafts only: a Records Officer approves and sends each letter.
    """
    from .estimate_letters import prepare_estimate_letters as prepare

    return prepare(limit=limit)


@shared_task
def prepare_record_review_advisories(*, limit: int = 25) -> int:
    """Recover missed automatic record analysis in small bounded batches."""
    if not (
        runtime.get("PRR_AI_ADVISORY_ENABLED")
        and runtime.get("PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED")
    ):
        return 0

    from .advisory_ai import request_automatic_record_advisory
    from .models import AdvisoryRun, RecordItem

    limit = max(1, min(int(limit), 100))
    active_states = [
        AdvisoryRun.State.PENDING,
        AdvisoryRun.State.RUNNING,
        AdvisoryRun.State.COMPLETE,
    ]
    responsiveness_runs = AdvisoryRun.objects.filter(
        record_item_id=OuterRef("pk"),
        kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
        state__in=active_states,
    )
    redaction_runs = AdvisoryRun.objects.filter(
        record_item_id=OuterRef("pk"),
        kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
        state__in=active_states,
    )
    redaction_signal = AdvisoryRun.objects.filter(
        record_item_id=OuterRef("pk"),
        kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
        state=AdvisoryRun.State.COMPLETE,
        proposal__suggested_state__in=["responsive", "uncertain"],
    )
    candidates = list(
        RecordItem.objects.annotate(
            has_responsiveness=Exists(responsiveness_runs),
            has_redactions=Exists(redaction_runs),
            should_precompute_redactions=Exists(redaction_signal),
            failed_responsiveness=Count(
                "advisory_runs",
                filter=(
                    Q(
                        advisory_runs__kind=(
                            AdvisoryRun.Kind.RECORD_RESPONSIVENESS
                        ),
                        advisory_runs__trigger=AdvisoryRun.Trigger.AUTOMATIC,
                        advisory_runs__state=AdvisoryRun.State.FAILED,
                    )
                    & ~Q(
                        advisory_runs__failure_code__in=(
                            _ADVISORY_PAUSE_FAILURE_CODES
                        )
                    )
                ),
                distinct=True,
            ),
            failed_redactions=Count(
                "advisory_runs",
                filter=(
                    Q(
                        advisory_runs__kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
                        advisory_runs__trigger=AdvisoryRun.Trigger.AUTOMATIC,
                        advisory_runs__state=AdvisoryRun.State.FAILED,
                    )
                    & ~Q(
                        advisory_runs__failure_code__in=(
                            _ADVISORY_PAUSE_FAILURE_CODES
                        )
                    )
                ),
                distinct=True,
            ),
        )
        .filter(
            Q(
                review_state=RecordItem.ReviewState.UNREVIEWED,
                has_responsiveness=False,
                failed_responsiveness__lt=3,
            )
            | Q(
                review_state=RecordItem.ReviewState.UNREVIEWED,
                should_precompute_redactions=True,
                has_redactions=False,
                failed_redactions__lt=3,
            )
            | Q(
                review_state=RecordItem.ReviewState.RESPONSIVE,
                has_redactions=False,
                failed_redactions__lt=3,
            )
        )
        .order_by("created_at", "pk")[:limit]
    )
    queued = 0
    for item in candidates:
        kind = (
            AdvisoryRun.Kind.RECORD_RESPONSIVENESS
            if (
                item.review_state == RecordItem.ReviewState.UNREVIEWED
                and not item.has_responsiveness
            )
            else AdvisoryRun.Kind.RECORD_REDACTIONS
        )
        try:
            request_automatic_record_advisory(item.pk, kind=kind)
        except Exception:
            logger.exception(
                "Automatic advisory supervisor could not queue record",
                extra={"record_item": str(item.pk), "advisory_kind": kind},
            )
            continue
        queued += 1
    return queued


@shared_task
def execute_vault_count(run_id: str):
    """Start one count-only Vault query and hand off bounded polling."""
    from .vault_counts import fail_vault_count, start_vault_count

    try:
        if not start_vault_count(run_id):
            poll_vault_count.apply_async(args=[run_id], countdown=2)
    except Exception:
        fail_vault_count(
            run_id,
            code="provider_unavailable",
            detail="Google Vault counting is temporarily unavailable.",
        )
        # Provider errors can contain the submitted query. Keep logs bounded
        # to the durable run identity; the staff-safe failure code is stored.
        logger.error(
            "Vault count failed to start; provider detail suppressed",
            extra={"vault_count_run": str(run_id)},
        )
        raise


class _VaultCollectionRetry(RuntimeError):
    """Content-free retry marker; provider errors never enter task evidence."""


@shared_task(bind=True, max_retries=4)
def execute_vault_collection(self, collection_task_id: int):
    """Start one confirmed Vault hold/export with bounded safe retries."""
    from .vault_collections import fail_vault_collection, start_vault_collection

    try:
        start_vault_collection(collection_task_id)
    except Exception:  # noqa: BLE001 - a provider fault retries, then fails the collection
        if self.request.retries >= self.max_retries:
            fail_vault_collection(
                collection_task_id,
                code="provider_unavailable",
                detail=(
                    "Vault collection could not start after bounded retries; "
                    "staff follow-up is required."
                ),
            )
            logger.error(
                "Vault collection exhausted startup retries; provider detail suppressed",
                extra={"collection_task": collection_task_id},
            )
            return
        countdown = min(30 * (2 ** self.request.retries), 300)
        raise self.retry(
            exc=_VaultCollectionRetry(
                "Vault collection provider is temporarily unavailable."
            ),
            countdown=countdown,
        ) from None


@shared_task(bind=True, max_retries=300)
def poll_vault_count(self, run_id: str):
    """Poll a Google count operation for up to roughly ten minutes."""
    from .vault_counts import advance_vault_count, fail_vault_count

    try:
        complete = advance_vault_count(run_id)
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=2) from exc
        fail_vault_count(
            run_id,
            code="provider_unavailable",
            detail="Google Vault counting did not complete after repeated checks.",
        )
        raise
    if not complete:
        if self.request.retries >= self.max_retries:
            fail_vault_count(
                run_id,
                code="provider_timeout",
                detail="Google Vault counting did not complete within ten minutes.",
            )
            return
        raise self.retry(countdown=2)


def _notify_safety_net_officers(prr, *, level: str) -> None:
    """Keep internal notification failures from starving durable queue work."""
    try:
        _notify(prr, level=level, cc_supervisor=True)
    except Exception:
        logger.exception(
            "Safety net officer notification failed",
            extra={"request": prr.number, "level": level},
        )


@shared_task
def compute_statutory_deadline(request_id: str):
    """Set the RCW 42.56.520 deadline as soon as a request is created.

    A litigation pull and a FERPA request are not RCW 42.56 requests and never
    acquire this clock, whoever calls this task (ADR 0004)."""
    from .deadlines import statutory_response_due
    from .models import MatterKind, PublicRecordsRequest, StatusEvent

    prr = PublicRecordsRequest.objects.get(pk=request_id)
    if prr.matter_kind != MatterKind.PUBLIC_RECORDS:
        return
    prr.statutory_response_due = statutory_response_due(
        prr.received_at, prr.tolled_business_days
    )
    prr.save(update_fields=["statutory_response_due"])
    StatusEvent.objects.create(
        request=prr,
        note=f"Statutory 5-business-day response due {prr.statutory_response_due}.",
    )


@shared_task(bind=True, max_retries=5)
def scan_intake_attachment(self, attachment_id: str):
    """Stream one quarantined intake file through ClamAV.

    Clean bytes are promoted to the private intake prefix. Infected bytes are
    deleted while their filename, digest, and scan result remain available as
    an evidentiary record. Scanner/storage outages remain fail-closed and are
    retried with bounded exponential backoff.
    """
    from apps.audit.models import audit

    from . import attachments
    from .models import IntakeAttachment

    attachment = IntakeAttachment.objects.select_related("request").get(
        pk=attachment_id
    )
    if attachment.scan_status in {
        IntakeAttachment.ScanStatus.CLEAN,
        IntakeAttachment.ScanStatus.INFECTED,
    }:
        return attachment.scan_status

    try:
        clean, detail = attachments.scan_object_for_malware(
            attachment.storage_key,
            expected_size=attachment.size,
            expected_sha256=attachment.sha256,
        )
        now = timezone.now()
        if clean:
            destination_key = attachments.clean_storage_key(attachment)
            attachments.promote_clean_attachment(
                attachment.storage_key, destination_key
            )
            attachment.storage_key = destination_key
            attachment.scan_status = IntakeAttachment.ScanStatus.CLEAN
            action = "intake_attachment.scan_clean"
        else:
            attachments.delete_quarantined_attachment(attachment.storage_key)
            attachment.scan_status = IntakeAttachment.ScanStatus.INFECTED
            action = "intake_attachment.scan_infected"
        attachment.scan_detail = detail[:500]
        attachment.scanned_at = now
        attachment.save(
            update_fields=["storage_key", "scan_status", "scan_detail", "scanned_at"]
        )
        audit(
            action,
            target=attachment.request.number,
            attachment_id=str(attachment.id),
            sha256=attachment.sha256,
        )
    except Exception as exc:
        attachment.scan_status = IntakeAttachment.ScanStatus.ERROR
        attachment.scan_detail = "Malware scanning is temporarily unavailable."
        attachment.save(update_fields=["scan_status", "scan_detail"])
        logger.exception(
            "Intake attachment scan failed",
            extra={"attachment": str(attachment.id)},
        )
        raise self.retry(
            exc=exc, countdown=min(60 * (2**self.request.retries), 900)
        ) from exc

    from .advisory_ai import defer_automatic_request_intent_advisory

    # The last final verdict is what lets the intake proposal start.
    defer_automatic_request_intent_advisory(attachment.request_id)
    return attachment.scan_status


@shared_task
def deadline_watchdog():
    """Celery-beat hourly. Escalating notifications:
    - due in 2 business days & no first response -> nag assigned officer
    - due tomorrow/today & no first response     -> officer + supervisor
    - overdue                                    -> supervisor + PRA officer, daily
    """
    from .deadlines import business_days_between
    from .models import (
        PublicRecordsRequest,
        RequestStatus,
        awaiting_initial_response,
    )

    today = timezone.localdate()
    awaiting_response = awaiting_initial_response(
        PublicRecordsRequest.objects.public_records()
    ).filter(
        statutory_response_due__isnull=False,
    ).exclude(status__in=[
        RequestStatus.FULFILLED, RequestStatus.CLOSED_WITHDRAWN,
        RequestStatus.CLOSED_ABANDONED, RequestStatus.CLOSED_NON_REQUEST,
        RequestStatus.CLOSED_NO_RECORDS,
        RequestStatus.CLOSED_DUPLICATE,
        RequestStatus.DENIED,
    ])

    for prr in awaiting_response:
        days_left = business_days_between(today, prr.statutory_response_due)
        overdue = today > prr.statutory_response_due
        if overdue:
            _notify(prr, level="OVERDUE", cc_supervisor=True)
        elif days_left <= 1:
            _notify(prr, level="URGENT", cc_supervisor=True)
        elif days_left <= 2:
            _notify(prr, level="WARNING", cc_supervisor=False)


def _notify(prr, level: str, cc_supervisor: bool):
    sent = notifications.notify_deadline(
        prr,
        level=level,
        include_supervisor=cc_supervisor,
    )
    if sent:
        logger.warning(
            "Deadline notification queued",
            extra={"request": prr.number, "level": level},
        )
    return sent


def _enqueue_vault_ingest(task):
    """Publish one claimed ingest handoff and retain durable enqueue evidence."""
    from apps.audit.models import audit

    from .models import CollectionTask

    try:
        ingest_vault_export.delay(task.pk)
        audit(
            _VAULT_INGEST_ENQUEUED_ACTION,
            target=str(task.pk),
            request_id=str(task.request_id),
            vault_export_id=task.vault_export_id,
        )
    except Exception:
        # The broker publish and database are not one transaction. Returning
        # to RUNNING makes an uncertain handoff retryable; ingestion must stay
        # idempotent because the broker may already have accepted the message.
        CollectionTask.objects.filter(
            pk=task.pk,
            state=CollectionTask.State.INGESTING,
        ).update(
            state=CollectionTask.State.RUNNING,
            updated_at=timezone.now(),
        )
        raise


def _reconcile_stale_vault_ingest_claims():
    """Re-publish only abandoned pre-enqueue claims after a bounded lease."""
    from apps.audit.models import AuditLog

    from .models import CollectionTask

    stale_before = timezone.now() - timezone.timedelta(
        seconds=runtime.get("PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS"),
    )
    enqueue_evidence = AuditLog.objects.filter(
        action=_VAULT_INGEST_ENQUEUED_ACTION,
        target=Cast(OuterRef("pk"), output_field=CharField()),
    )
    stale_claims = (
        CollectionTask.objects.filter(
            state=CollectionTask.State.INGESTING,
            updated_at__lt=stale_before,
        )
        .annotate(has_enqueue_evidence=Exists(enqueue_evidence))
        .filter(has_enqueue_evidence=False)
        .order_by("pk")
    )

    for task in stale_claims.iterator():
        # An INGESTING task with append-only enqueue evidence belongs to the
        # idempotent worker. Without that evidence, an old claim represents
        # the process-death window before delay().
        claim_time = timezone.now()
        claimed = CollectionTask.objects.filter(
            pk=task.pk,
            state=CollectionTask.State.INGESTING,
            updated_at__lt=stale_before,
        ).update(updated_at=claim_time)
        if claimed:
            _enqueue_vault_ingest(task)


@shared_task
def poll_vault_exports():
    """Advance Vault exports and reconcile abandoned ingestion enqueue claims."""
    from apps.integrations.vault import client as vault

    from .fees import open_deposit_gate
    from .models import CollectionTask, FeeDepositGate

    running = CollectionTask.objects.filter(
        state=CollectionTask.State.RUNNING
    ).exclude(vault_export_id="").select_related("request")

    for task in running:
        export = vault.get_export(task.vault_matter_id, task.vault_export_id)
        if export.get("status") == "COMPLETED":
            if open_deposit_gate(
                task.request,
                scope=FeeDepositGate.Scope.COLLECTION,
            ) is not None:
                # A collection deposit opened mid-export holds ingest. The
                # task stays RUNNING and ingests on the first poll after the
                # deposit is recorded or lifted.
                continue
            claimed = CollectionTask.objects.filter(
                pk=task.pk,
                state=CollectionTask.State.RUNNING,
            ).update(
                state=CollectionTask.State.INGESTING,
                updated_at=timezone.now(),
            )
            if not claimed:
                continue
            _enqueue_vault_ingest(task)
        elif export.get("status") == "FAILED":
            from apps.audit.models import audit

            from .models import StatusEvent

            task.state = CollectionTask.State.FAILED
            task.completed_at = None
            task.save(update_fields=["state", "completed_at", "updated_at"])
            StatusEvent.objects.create(
                request=task.request,
                note=(
                    "Vault collection failed before ingestion: Google Vault "
                    "reported the export as failed. Staff follow-up is required."
                ),
            )
            audit(
                "vault.export.failed",
                target=task.request.number,
                collection_task_id=task.pk,
                vault_export_id=task.vault_export_id,
            )

    _reconcile_stale_vault_ingest_claims()


@shared_task(bind=True, max_retries=5)
def ingest_vault_export(self, collection_task_id: int):
    """Retain and resume one completed Vault export into the review queue."""
    from apps.audit.models import audit

    from .models import CollectionTask, StatusEvent
    from .vault_ingest import (
        VaultIngestPermanentError,
        ingest_completed_export,
    )

    logger.info("ingest_vault_export started", extra={"task": collection_task_id})
    try:
        return ingest_completed_export(collection_task_id).pk
    except VaultIngestPermanentError as exc:
        task = CollectionTask.objects.select_related("request").get(
            pk=collection_task_id,
        )
        changed = CollectionTask.objects.filter(
            pk=task.pk,
            state=CollectionTask.State.INGESTING,
        ).update(
            state=CollectionTask.State.FAILED,
            completed_at=None,
            updated_at=timezone.now(),
        )
        if changed:
            StatusEvent.objects.create(
                request=task.request,
                note=(
                    "Vault collection ingestion failed and requires staff "
                    f"follow-up ({exc.code})."
                ),
            )
            audit(
                "vault.ingest.permanent_failure",
                target=task.request.number,
                collection_task_id=task.pk,
                error_code=exc.code,
            )
        raise
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            task = CollectionTask.objects.select_related("request").get(
                pk=collection_task_id,
            )
            changed = CollectionTask.objects.filter(
                pk=task.pk,
                state=CollectionTask.State.INGESTING,
            ).update(
                state=CollectionTask.State.FAILED,
                completed_at=None,
                updated_at=timezone.now(),
            )
            if changed:
                StatusEvent.objects.create(
                    request=task.request,
                    note=(
                        "Vault collection ingestion exhausted its bounded "
                        "retries; staff follow-up is required."
                    ),
                )
                audit(
                    "vault.ingest.retries_exhausted",
                    target=task.request.number,
                    collection_task_id=task.pk,
                )
            raise
        countdown = min(60 * (2 ** self.request.retries), 900)
        raise self.retry(exc=exc, countdown=countdown) from exc


@shared_task
def auto_acknowledge_safety_net():
    """Hourly. If a request reaches the LAST DAY of its RCW 42.56.520 window
    with no human response, queue a durable automated acknowledgment. Queueing
    is not delivery: the request remains RECEIVED until the provider success
    callback applies the legal effect. Officers are notified that the net
    queued work so they can monitor delivery and review the estimate."""
    from apps.correspondence import services as correspondence_services

    from .models import (
        PublicRecordsRequest,
        RequestStatus,
        awaiting_initial_response,
    )

    # An unreadable settings store raises here rather than reading as "off":
    # a failed run is visible and the next hourly tick retries, where a
    # silent skip on the due date would be a late acknowledgment.
    if not runtime.get("AUTO_ACK_SAFETY_NET", raise_on_store_error=True):
        return

    today = timezone.localdate()
    terminal_statuses = {
        RequestStatus.FULFILLED,
        RequestStatus.CLOSED_WITHDRAWN,
        RequestStatus.CLOSED_ABANDONED,
        RequestStatus.CLOSED_NON_REQUEST,
        RequestStatus.CLOSED_NO_RECORDS,
        RequestStatus.CLOSED_DUPLICATE,
        RequestStatus.DENIED,
    }
    # The safety net is the District's only automated statutory response, so
    # it sees only the matters that owe one: RCW 42.56 requests (ADR 0004).
    at_risk = awaiting_initial_response(
        PublicRecordsRequest.objects.public_records()
    ).filter(
        statutory_response_due__lte=today,   # last day of window or already past
    ).exclude(status__in=terminal_statuses).order_by(
        "statutory_response_due",
        "received_at",
        "pk",
    )

    for prr in at_risk:
        try:
            queued = correspondence_services.queue_automated_acknowledgment(
                prr
            )
        except ValidationError:
            prr.refresh_from_db(fields=["acknowledgment_sent_at", "status"])
            if (
                prr.initial_response_delivered
                or prr.status in terminal_statuses
            ):
                continue
            _notify_safety_net_officers(prr, level="AUTO_ACK_BLOCKED")
            logger.error(
                "Safety net acknowledgment could not be queued",
                extra={"request": prr.number},
            )
            continue
        if not queued.created:
            continue
        _notify_safety_net_officers(prr, level="AUTO_ACK_QUEUED")
        logger.warning(
            "Safety net acknowledgment queued",
            extra={"request": prr.number},
        )


@shared_task
def estimate_watchdog():
    """Daily. Estimated completion dates are commitments — when one is
    approaching or blown, the officer must either produce or file a reasoned
    revision through the durable correspondence lifecycle. No silent slippage."""
    from .deadlines import business_days_between
    from .models import PublicRecordsRequest, RequestStatus

    today = timezone.localdate()
    open_qs = PublicRecordsRequest.objects.filter(
        estimated_completion__isnull=False,
    ).exclude(status__in=[
        RequestStatus.FULFILLED, RequestStatus.CLOSED_WITHDRAWN,
        RequestStatus.CLOSED_ABANDONED, RequestStatus.CLOSED_NON_REQUEST,
        RequestStatus.CLOSED_NO_RECORDS,
        RequestStatus.CLOSED_DUPLICATE,
        RequestStatus.DENIED,
    ])

    for prr in open_qs:
        if today > prr.estimated_completion:
            notifications.notify_estimate(prr, approaching=False)
        elif business_days_between(today, prr.estimated_completion) <= 3:
            notifications.notify_estimate(prr, approaching=True)


@shared_task
def unpaid_fee_watchdog():
    """Daily. Surface a case whose fee or deposit has gone unanswered.

    RCW 42.56.120(2) lets the District stop fulfilling a request when the
    requester does not pay. Whether to do that, and whether to waive instead,
    is a Records Officer's decision with consequences a schedule cannot weigh,
    so this task only creates the work item that puts the case in front of
    one. It drafts nothing, sends nothing, and closes nothing.
    """
    from apps.work_queue.models import WorkItem
    from apps.work_queue.source_reconciliation import reconcile_fee_unpaid_work

    from .fees import unpaid_fee_attention_requests
    from .models import PublicRecordsRequest

    owing = {prr.pk: prr for prr in unpaid_fee_attention_requests()}
    for prr in owing.values():
        reconcile_fee_unpaid_work(prr, eligible=True)
    # Chips still open on cases that no longer owe anything (paid, lifted,
    # closed, or merged away) retire here rather than linger in the Inbox.
    settled = PublicRecordsRequest.objects.filter(
        work_items__work_kind=WorkItem.WorkKind.FEE_UNPAID,
        work_items__disposition__in=[
            WorkItem.Disposition.NEEDS_ATTENTION,
            WorkItem.Disposition.WAITING,
        ],
    ).exclude(pk__in=owing)
    for prr in settled:
        reconcile_fee_unpaid_work(prr, eligible=False)
    return len(owing)


def _stall_activity_queryset():
    """Annotate open requests without multiplying independent child rows."""
    from .models import (
        CollectionTask,
        EstimateRevision,
        PublicRecordsRequest,
        RequestStatus,
        StatusEvent,
        TimeEntry,
    )

    latest_event = (
        StatusEvent.objects.filter(request_id=OuterRef("pk"))
        .order_by("-occurred_at")
        .values("occurred_at")[:1]
    )
    latest_collection_activity = (
        CollectionTask.objects.filter(request_id=OuterRef("pk"))
        .order_by("-updated_at")
        .values("updated_at")[:1]
    )
    latest_time_entry = (
        TimeEntry.objects.filter(request_id=OuterRef("pk"))
        .order_by("-date", "-pk")
        .values("date")[:1]
    )
    estimate_revision_counts = (
        EstimateRevision.objects.filter(request_id=OuterRef("pk"))
        .order_by()
        .values("request_id")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )
    return PublicRecordsRequest.objects.exclude(
        status__in=[
            RequestStatus.FULFILLED,
            RequestStatus.CLOSED_WITHDRAWN,
            RequestStatus.CLOSED_ABANDONED,
            RequestStatus.CLOSED_NON_REQUEST,
            RequestStatus.CLOSED_NO_RECORDS,
            RequestStatus.CLOSED_DUPLICATE,
            RequestStatus.DENIED,
        ]
    ).annotate(
        latest_event_at=Subquery(latest_event),
        latest_collection_activity_at=Subquery(
            latest_collection_activity
        ),
        latest_time_entry_date=Subquery(latest_time_entry),
        estimate_revision_total=Coalesce(
            Subquery(
                estimate_revision_counts,
                output_field=IntegerField(),
            ),
            Value(0),
        ),
    )


@shared_task
def stall_detector():
    """Weekly. Surfaces the exact pattern the user has experienced as a
    requester: a request that only gets touched to reset its dates. A request
    is STALLED when nothing substantive has happened for STALL_THRESHOLD_DAYS
    — no status events, no collection activity, no time entries. Stalls go to
    the supervisor as a digest. The system makes stagnation loud instead of
    helping hide it."""

    threshold = timezone.now() - timezone.timedelta(days=runtime.get("STALL_THRESHOLD_DAYS"))
    open_qs = _stall_activity_queryset()

    stalled = []
    for prr in open_qs:
        latest_time_entry_at = None
        if prr.latest_time_entry_date:
            latest_time_entry_at = timezone.make_aware(
                timezone.datetime.combine(
                    prr.latest_time_entry_date,
                    timezone.datetime.min.time(),
                )
            )
        last_activity = max(
            filter(
                None,
                [
                    prr.latest_event_at,
                    prr.latest_collection_activity_at,
                    latest_time_entry_at,
                    prr.received_at,
                ],
            )
        )
        if last_activity < threshold:
            stalled.append((prr, last_activity))

    for prr, last_activity in stalled:
        notifications.notify_stalled(
            prr,
            last_activity=last_activity,
            revision_count=prr.estimate_revision_total,
        )


# A camera is HELD only when footage covers at least this share of the
# requested window on every head; anything less is a GAP a human reviews.
FULL_COVERAGE = 0.95


def _notify_manual_hold(
    fp, *, reason: str, error: str, site_codes, cameras=(), condition: str = "",
) -> None:
    """Send the URGENT manual-hold instructions and record on the timeline
    whether the alert went out."""
    from .models import StatusEvent

    extra = {"cameras": list(cameras), "condition": condition} if cameras or condition else {}
    notification_result = notifications.notify_footage_manual_hold(
        fp,
        error=error,
        site_codes=list(site_codes),
        **extra,
    )
    if notification_result is True:
        notification_note = "notification sent or queued to facilities."
    elif notification_result is None:
        notification_note = (
            "an equivalent notification was already sent or queued; "
            "duplicate alert suppressed."
        )
    else:
        notification_note = (
            "notification delivery failed or was blocked; operational "
            "intervention required."
        )
    StatusEvent.objects.create(
        request=fp.request,
        note=(
            f"Footage preservation: {reason} — URGENT manual hold "
            + notification_note
        ),
    )


class _HoldRun(NamedTuple):
    """One run of the hold task: the marker it owns and the state it began in."""

    started_at: dt.datetime
    from_state: str


def _start_hold_run(fp) -> _HoldRun:
    """Adopt the run marker intake or the widen set (or start one for a
    rerun) and heartbeat once."""
    from .models import FootagePreservation

    now = timezone.now()
    run = _HoldRun(fp.hold_run_started_at or now, fp.state)
    FootagePreservation.objects.filter(pk=fp.pk).update(
        hold_run_started_at=run.started_at, hold_heartbeat_at=now,
    )
    fp.hold_run_started_at, fp.hold_heartbeat_at = run.started_at, now
    return run


def _heartbeat(fp, run: _HoldRun) -> None:
    from .models import FootagePreservation

    FootagePreservation.objects.filter(
        pk=fp.pk, hold_run_started_at=run.started_at,
    ).update(hold_heartbeat_at=timezone.now())


def _finish_hold_run(fp, run: _HoldRun, state) -> bool:
    """Write the run's result only if nothing changed the hold since the run
    began; in particular never overwrite a MANUAL the sweeper set. Audited
    when it loses: the URGENT notice that superseded it already went out."""
    from apps.audit.models import audit

    from .models import FootagePreservation

    won = FootagePreservation.objects.filter(
        pk=fp.pk, state=run.from_state, hold_run_started_at=run.started_at,
    ).update(state=state, hold_run_started_at=None)
    if won:
        fp.state, fp.hold_run_started_at = state, None
        return True
    audit(
        "footage.hold_result_superseded", target=fp.request.number,
        result=str(state), run_started=run.started_at.isoformat(),
    )
    return False


def _degrade_or_report_widen(
    fp, run: _HoldRun, *, only_new: bool, widened_from, reason: str, error: str,
) -> None:
    """A widen only adds cameras: when it cannot run, send the URGENT
    district-wide notice and leave the hold's state and held cameras as they
    were. Its scope returns to the sites actually held, so the confirmation
    never claims district-wide cameras and the widen can be posted again.
    A first run degrades to MANUAL."""
    from apps.audit.models import audit

    from . import triage
    from .models import FootagePreservation, StatusEvent

    if not only_new:
        _degrade_footage_hold_to_manual(fp, run, reason=reason, error=error)
        return
    _notify_manual_hold(
        fp, reason=f"widened hold not run: {reason}", error=error, site_codes=[],
    )
    if widened_from and FootagePreservation.objects.filter(
        pk=fp.pk, site_codes=[], hold_run_started_at=run.started_at,
    ).update(site_codes=list(widened_from)):
        fp.site_codes = list(widened_from)
        sites = triage.describe_sites(widened_from)
        audit("footage.hold_widen_not_run", target=fp.request.number,
              sites=fp.site_codes, reason=reason)
        StatusEvent.objects.create(
            request=fp.request,
            note=(
                f"Footage hold not widened ({reason}): Aware still holds only "
                f"{sites}, so the hold's scope is back to {sites}. Facilities "
                "has URGENT manual instructions for every camera district-wide; "
                "widen it again once Aware answers."
            ),
        )


def _end_hold_run(preservation_id: int, run: _HoldRun | None) -> None:
    """Clear this run's marker when it stops without writing a result."""
    from .models import FootagePreservation

    if run is not None:
        FootagePreservation.objects.filter(
            pk=preservation_id, hold_run_started_at=run.started_at,
        ).update(hold_run_started_at=None)


def _degrade_footage_hold_to_manual(
    fp, run: _HoldRun, *, reason: str, error: str, site_codes=None,
) -> None:
    """Rule 7: when no camera ended up held, fall back to an URGENT manual
    hold — facilities gets exact instructions and the timeline says why and
    whether the alert went out. Never silence, never a false "Held"."""
    from .models import FootagePreservation

    if not _finish_hold_run(fp, run, FootagePreservation.State.MANUAL):
        return
    _notify_manual_hold(
        fp,
        reason=reason,
        error=error,
        site_codes=fp.site_codes if site_codes is None else site_codes,
    )


# Said on every camera whose create may have landed without confirming.
BOOKMARK_MAY_EXIST = (
    "a bookmark may already exist on this device; check it in Aware before retrying"
)
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_REDACT = re.compile(
    r"https?://\S+|host='[^']*'|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _safe_alta_error(exc: BaseException, *, action: str = "bookmark") -> str:
    """A short class-plus-status description of an Aware failure. Stored and
    emailed, so it never carries a URL, a hostname, or a device id."""
    from apps.integrations.avigilon import client as alta

    status = _HTTP_STATUS.search(str(exc))
    detail = type(exc).__name__ + (f", HTTP {status.group(1)}" if status else "")
    logger.debug("Aware %s failed: %s", action, _REDACT.sub("<redacted>", str(exc)))
    if isinstance(exc, alta.AltaWriteUncertain):
        return f"Aware {action} uncertain ({detail}): {BOOKMARK_MAY_EXIST}."
    return f"Aware {action} failed ({detail})."


def _camera_line(ch) -> str:
    """One camera for a human checklist: name, site, and what happened."""
    from .models import CameraHold

    label = f"{ch.camera_name or 'Unnamed camera'} ({ch.site or 'no site'})"
    if ch.state == CameraHold.State.GAP:
        confirmed = f"{ch.coverage_fraction:.0%}" if ch.coverage_fraction is not None else "unknown"
        return f"{label}: footage gap or unverified coverage ({confirmed} confirmed)"
    if ch.state == CameraHold.State.FAILED:
        return f"{label}: failed — {ch.error}" if ch.error else f"{label}: failed"
    if ch.state == CameraHold.State.NOT_RETAINED:
        return f"{label}: no footage retained for the window"
    return f"{label}: not attempted"


def _no_cameras_at(fp, site_codes) -> None:
    """A named site has no cameras in Aware: queue the adequate search and
    send the URGENT manual notice, since footage may sit on another recorder."""
    from . import triage

    sites = triage.describe_sites(site_codes)
    triage.record_no_cameras_review(fp, site_codes=site_codes)
    _notify_manual_hold(
        fp,
        reason=f"Aware has no cameras at {sites}",
        error=(
            f"Aware lists no cameras at {sites}, so nothing was bookmarked "
            "there; a human must find what, if anything, records the site "
            "and preserve it"
        ),
        site_codes=site_codes,
        condition="no-cameras:" + ",".join(site_codes),
    )


def _local_time(value) -> str:
    return f"{timezone.localtime(value):%Y-%m-%d %H:%M} Pacific"


def _bookmark_coverage(bookmark, *, sources, start, end, now):
    """Return (lowest share of the REQUESTED window that footage covers
    across the camera's heads, a note per short or erroring head, whether
    every head's coverage was verified).

    Each head reports the seconds of footage Aware stores inside the
    bookmark. Score them against the requested window [start, end) up to
    ``now``: footage after now cannot exist yet, and footage before the
    bookmark start (the oldest retained) is gone, so it counts against the
    window. A bookmarked head with no report is unverified. All arguments
    are UTC."""
    if not bookmark.coverage:
        return 0.0, ["no per-head coverage reported by Aware; check the bookmark"], False
    elapsed = (min(end, now) - start).total_seconds()
    if elapsed <= 0:
        elapsed = (end - start).total_seconds()
    fractions, notes = [], []
    for head in bookmark.coverage:
        fraction = min(1.0, max(0.0, head.covered_seconds) / elapsed)
        fractions.append(fraction)
        if fraction < FULL_COVERAGE or head.error:
            note = f"head {head.source}: {fraction:.0%}"
            if head.error:
                note += f" ({head.error})"
            notes.append(note)
    reported = {head.source for head in bookmark.coverage}
    unreported = [sid for sid in dict.fromkeys(sources) if sid not in reported]
    for sid in unreported:
        fractions.append(0.0)
        notes.append(f"head {sid}: no coverage reported by Aware")
    verified = not unreported and not any(head.error for head in bookmark.coverage)
    return min(fractions), notes, verified


def _hold_one_camera(fp, cam, ch) -> None:
    """Bookmark one camera from the later of the window start and its oldest
    retained footage, then record coverage. Never raises."""
    import datetime as dt

    from apps.integrations.avigilon import client as alta

    from . import triage
    from .models import CameraHold

    start = fp.window_start.astimezone(dt.UTC)
    end = fp.window_end.astimezone(dt.UTC)
    notes = []
    if cam.status and cam.status != "CONNECTED":
        notes.append(f"Camera status {cam.status} when the hold ran.")
    if end > timezone.now():
        notes.append("The window ends after the hold ran; later footage did not exist yet.")

    pruned_to = cam.pruned_to
    if pruned_to is not None:
        # Aware times are UTC; a naive one must not be read as server-local.
        pruned_to = (
            pruned_to.replace(tzinfo=dt.UTC) if timezone.is_naive(pruned_to)
            else pruned_to.astimezone(dt.UTC)
        )
    if pruned_to is None:
        hold_start = start
        notes.append("Aware reported no oldest-footage time; bookmarked from the window start.")
    elif pruned_to >= end:
        ch.state = CameraHold.State.NOT_RETAINED
        ch.coverage_fraction = 0.0
        ch.error = ""
        ch.hold_claimed_at = None  # no create was attempted
        notes.append(
            f"Oldest retained footage ({_local_time(pruned_to)}) is at or after "
            "the window end; nothing to bookmark, but other users' bookmarks "
            "on this device may still hold older footage: check them in Aware."
        )
        ch.coverage_note = " ".join(notes)[:500]
        ch.save()
        return
    else:
        hold_start = max(start, pruned_to)
        if pruned_to > start:
            notes.append(
                f"Oldest retained footage is {_local_time(pruned_to)}, after the "
                "window start; bookmarked from then."
            )

    try:
        bookmark = alta.create_bookmark_hold(
            cam, hold_start, end,
            name=triage.bookmark_name(fp.request, cam.name),
            description=triage.BOOKMARK_DESCRIPTION,
        )
    except Exception as exc:  # noqa: BLE001 — per-camera isolation
        ch.state = CameraHold.State.FAILED
        ch.error = _safe_alta_error(exc)[:500]
        if isinstance(exc, alta.AltaWriteUncertain):
            notes.append(BOOKMARK_MAY_EXIST[0].upper() + BOOKMARK_MAY_EXIST[1:] + ".")
        else:
            ch.hold_claimed_at = None  # Aware made no bookmark; a rerun may retry
        ch.coverage_note = " ".join(notes)[:500]
        ch.save()
        return

    # The bookmark exists now: record it before anything else can fail, so a
    # rerun never makes a second one and a release can still delete it.
    ch.clip_id = str(bookmark.id)[:100]
    ch.save(update_fields=["clip_id"])
    try:
        # Score against when this bookmark was made: a camera reached late in
        # a long run had more footage to store than existed when the run began.
        fraction, head_notes, verified = _bookmark_coverage(
            bookmark, sources=cam.sources, start=start, end=end, now=timezone.now(),
        )
        ch.coverage_fraction = fraction
        # An unverified head is never a clean hold.
        ch.state = (
            CameraHold.State.HELD if verified and fraction >= FULL_COVERAGE
            else CameraHold.State.GAP
        )
        ch.error = ""
        if head_notes:
            notes.append("Short coverage: " + "; ".join(head_notes) + ".")
        ch.coverage_note = " ".join(notes)[:500]
        ch.save()
    except Exception as exc:  # the bookmark stands; say so
        logger.exception("Recording footage hold %s failed after its bookmark", ch.pk)
        ch.state = CameraHold.State.FAILED
        ch.error = (
            f"Recording the hold failed ({type(exc).__name__}) after Aware made "
            "the bookmark: the bookmark exists, but its coverage is unconfirmed; "
            "check it in Aware."
        )[:500]
        ch.save(update_fields=["state", "error"])


def _window_predates_flat_retention(fp) -> bool:
    floor = fp.created_at - dt.timedelta(days=runtime.get("CAMERA_RETENTION_DAYS"))
    return fp.window_end < floor


# A hold run whose heartbeat is this old has stopped; tell a human.
FOOTAGE_HOLD_STALL_MINUTES = 15
HOLD_STOPPED_ERROR = (
    "The automatic hold stopped before it finished, so some or all cameras "
    "may not be bookmarked"
)
WIDEN_STOPPED_ERROR = (
    "Widening the hold district-wide stopped before it finished, so some "
    "cameras outside the original sites may not be bookmarked"
)


def _claim_pending_as_manual(fp, *, reason: str, error: str, stale_before=None) -> bool:
    """Move a still-PENDING preservation to MANUAL and send the URGENT notice.
    Conditional, so a finished hold, a run that just heartbeat, or a second
    sweeper is left alone."""
    from .models import FootagePreservation

    still = FootagePreservation.objects.filter(
        pk=fp.pk, state=FootagePreservation.State.PENDING,
    )
    if stale_before is not None:
        still = still.filter(hold_heartbeat_at__lt=stale_before)
    if not still.update(state=FootagePreservation.State.MANUAL, hold_run_started_at=None):
        return False
    fp.state, fp.hold_run_started_at = FootagePreservation.State.MANUAL, None
    _notify_manual_hold(fp, reason=reason, error=error, site_codes=fp.site_codes)
    return True


def _notify_widen_stopped(fp) -> None:
    _notify_manual_hold(
        fp, reason="widened hold stopped before it finished",
        error=WIDEN_STOPPED_ERROR, site_codes=[], condition="widen-stopped",
    )


def _claim_stalled_widen(fp, *, stale_before) -> bool:
    """A widen that stopped keeps the hold's state (its earlier cameras are
    still bookmarked); end the run and send the URGENT notice."""
    from .models import FootagePreservation

    if not FootagePreservation.objects.filter(
        pk=fp.pk, hold_run_started_at=fp.hold_run_started_at,
        hold_heartbeat_at__lt=stale_before,
    ).update(hold_run_started_at=None):
        return False
    fp.hold_run_started_at = None
    _notify_widen_stopped(fp)
    return True


@shared_task
def sweep_stalled_footage_holds() -> int:
    """Rule 7 backstop: a hold run (the first one or a widen) that never
    started or stopped mid-way must not sit with nobody told while retention
    runs out. Acts on a stale heartbeat, never on age: a long district-wide
    run keeps heartbeating."""
    from .models import FootagePreservation

    cutoff = timezone.now() - dt.timedelta(minutes=FOOTAGE_HOLD_STALL_MINUTES)
    stalled = FootagePreservation.objects.select_related("request").filter(
        Q(state=FootagePreservation.State.PENDING) | Q(hold_run_started_at__isnull=False),
        hold_heartbeat_at__lt=cutoff,
    ).exclude(state=FootagePreservation.State.RELEASED)
    claimed = 0
    for fp in stalled:
        if fp.state == FootagePreservation.State.PENDING:
            claimed += _claim_pending_as_manual(
                fp,
                reason=f"hold not finished after {FOOTAGE_HOLD_STALL_MINUTES} minutes",
                error=HOLD_STOPPED_ERROR,
                stale_before=cutoff,
            )
        else:
            claimed += _claim_stalled_widen(fp, stale_before=cutoff)
    return claimed


@shared_task
def execute_footage_hold(
    preservation_id: int, only_new: bool = False, widened_from=None,
):
    """Run the hold; whatever happens, never leave it PENDING. ``only_new``
    (a widen) bookmarks only cameras with no hold row yet; ``widened_from``
    is the scope it widened, restored if the widen cannot run."""
    try:
        _execute_footage_hold(
            preservation_id, only_new=only_new, widened_from=widened_from,
        )
    except Exception:
        if only_new:
            _widen_stopped(preservation_id)
        raise
    finally:
        _never_leave_pending(preservation_id)


def _widen_stopped(preservation_id: int) -> None:
    """A widen that raised is not PENDING, so _never_leave_pending skips it;
    tell a human now instead of waiting for the sweeper."""
    from .models import FootagePreservation

    try:
        fp = FootagePreservation.objects.select_related("request").get(pk=preservation_id)
        _notify_widen_stopped(fp)
    except Exception:  # keep the original error
        logger.exception("Could not report widened footage hold %s", preservation_id)


def _never_leave_pending(preservation_id: int) -> None:
    from .models import FootagePreservation

    try:
        fp = FootagePreservation.objects.select_related("request").get(pk=preservation_id)
        if fp.state == FootagePreservation.State.PENDING:
            _claim_pending_as_manual(
                fp, reason="hold stopped before it finished", error=HOLD_STOPPED_ERROR,
            )
    except Exception:  # the sweeper retries; keep the original error
        logger.exception("Could not move footage hold %s off PENDING", preservation_id)


def _execute_footage_hold(
    preservation_id: int, *, only_new: bool = False, widened_from=None,
):
    """Bookmark every in-scope camera for the requested window: each camera at
    the sites the request names, or every camera district-wide when it names
    none. Broad by default — narrowing is a human decision
    (triage.exclude_camera). Holds run one camera at a time, and a rerun
    skips cameras that already have a bookmark.

    Outcomes: HELD only when every in-scope camera is fully covered; PARTIAL
    when something is bookmarked but anything is short; NO_CAMERAS when the
    named sites have no cameras in Aware; EXPIRED when no in-scope camera
    retains the window; MANUAL (urgent email to facilities) when the API is
    unreachable, returns no cameras, a named site has no Aware group, or no
    hold succeeded — the retention clock does not care about our API status.
    Every result is written conditionally (_finish_hold_run), so a run the
    sweeper already reported never overwrites what it set.
    """
    from .models import FootagePreservation

    fp = FootagePreservation.objects.select_related("request").get(pk=preservation_id)
    if fp.state == FootagePreservation.State.RELEASED:
        return
    run = _start_hold_run(fp)
    try:
        _run_footage_hold(fp, run, only_new=only_new, widened_from=widened_from)
    finally:
        _end_hold_run(fp.pk, run)


def _run_footage_hold(fp, run: _HoldRun, *, only_new: bool, widened_from) -> None:
    from collections import Counter

    from apps.integrations.avigilon import client as alta

    from . import triage
    from .models import CameraHold, FootagePreservation, StatusEvent

    prr = fp.request
    named = list(fp.site_codes or [])

    try:
        cameras = alta.list_device_cameras()
        groups = alta.list_device_groups() if named else []
    # Rule 7: transport, HTTP, and decode errors must degrade like AltaError
    # does; an escaping exception would leave the hold PENDING and nobody told.
    except Exception as exc:  # noqa: BLE001
        error = _safe_alta_error(exc, action="camera list")
        if _window_predates_flat_retention(fp):
            # Never EXPIRED: only Aware knows which cameras keep it longer.
            error += (
                " The window predates the "
                f"{runtime.get('CAMERA_RETENTION_DAYS')}-day retention setting, "
                "but some cameras keep footage longer: check each camera's "
                "retention in Aware and bookmark any that still hold the window."
            )
        _degrade_or_report_widen(
            fp, run, only_new=only_new, widened_from=widened_from,
            reason="API unavailable", error=error,
        )
        return

    if not cameras:
        _degrade_or_report_widen(
            fp, run, only_new=only_new, widened_from=widened_from,
            reason="Aware returned zero cameras",
            error=(
                "Aware returned zero cameras, so nothing was preserved; a human "
                "must confirm where any footage for this window lives"
            ),
        )
        return

    def norm(value):
        return (value or "").strip().upper()

    missing, empty = [], []
    if named:
        group_names = {norm(group.name) for group in groups}
        camera_sites = {norm(cam.site) for cam in cameras}
        missing = [code for code in named if norm(code) not in group_names]
        empty = [
            code for code in named
            if code not in missing and norm(code) not in camera_sites
        ]
        in_scope = {norm(code) for code in named} - {norm(code) for code in missing + empty}
        scoped = [cam for cam in cameras if norm(cam.site) in in_scope]
    else:
        scoped = list(cameras)

    if not scoped:
        if missing:
            _degrade_footage_hold_to_manual(
                fp, run,
                reason=f"no Aware device group for {triage.describe_sites(missing)}",
                error=(
                    "Aware has no device group for "
                    f"{triage.describe_sites(missing)}, so nothing was preserved "
                    "there; a human must find which cameras cover the site"
                ),
                site_codes=missing,
            )
        elif not _finish_hold_run(fp, run, FootagePreservation.State.NO_CAMERAS):
            return
        if empty:
            _no_cameras_at(fp, empty)
        return

    skip = {CameraHold.State.RELEASED, *CameraHold.BOOKMARKED_STATES}
    for cam in scoped:  # serial: one Aware bookmark at a time
        ch, created = CameraHold.objects.get_or_create(
            preservation=fp,
            camera_id=str(cam.id)[:100],
            defaults={
                "camera_name": (cam.name or "")[:255],
                "site": (cam.site or "")[:255],
                "device_group_id": (cam.device_group_id or "")[:100],
            },
        )
        # A recorded bookmark id means the bookmark exists: never make another.
        # A widen touches only cameras never tried before.
        if (only_new and not created) or ch.state in skip or ch.clip_id \
                or ch.excluded_at is not None:
            continue
        # Claim the camera before calling Aware: a concurrent run that got
        # here first owns it.
        claimed_at = timezone.now()
        if not CameraHold.objects.filter(
            pk=ch.pk, clip_id="", hold_claimed_at__isnull=True,
        ).update(hold_claimed_at=claimed_at):
            continue
        ch.hold_claimed_at = claimed_at
        _heartbeat(fp, run)
        _hold_one_camera(fp, cam, ch)

    holds = fp.camera_holds.filter(excluded_at__isnull=True).exclude(
        state=CameraHold.State.RELEASED,
    )
    counts = Counter(holds.values_list("state", flat=True))
    held = counts[CameraHold.State.HELD]
    gap = counts[CameraHold.State.GAP]
    not_retained = counts[CameraHold.State.NOT_RETAINED]
    failed = counts[CameraHold.State.FAILED] + counts[CameraHold.State.PENDING]

    if held + gap == 0:
        if failed == 0 and not missing:
            if not _finish_hold_run(fp, run, FootagePreservation.State.EXPIRED):
                return
            triage.record_outside_retention_review(
                fp,
                reason=(
                    "Aware holds no footage for the requested window on any of "
                    f"the {not_retained} camera(s) in scope (each camera's oldest "
                    "retained footage is after the window end)."
                ),
            )
        else:
            first_error = (
                holds.filter(state=CameraHold.State.FAILED)
                .exclude(error="").values_list("error", flat=True).first() or ""
            )
            uncertain = holds.filter(
                state=CameraHold.State.FAILED, error__contains=BOOKMARK_MAY_EXIST,
            ).count()
            reason = (
                f"every camera hold failed (0 of {failed} held)"
                if not not_retained else
                f"no camera was bookmarked ({failed} failed, {not_retained} "
                "with no retained footage)"
            )
            _degrade_footage_hold_to_manual(
                fp, run,
                reason=reason,
                error=f"{reason}; first error: {first_error[:300]}" + (
                    f" On {uncertain} camera(s) {BOOKMARK_MAY_EXIST}."
                    if uncertain else ""
                ),
            )
        if empty:
            _no_cameras_at(fp, empty)
        return

    complete = held == holds.count() and not missing and not empty
    if not _finish_hold_run(
        fp, run,
        FootagePreservation.State.HELD if complete else FootagePreservation.State.PARTIAL,
    ):
        return

    scope = f" at {triage.describe_sites(named)}" if named else " (district-wide)"
    StatusEvent.objects.create(
        request=prr,
        note=(
            f"Footage preservation{scope}: {held} camera(s) held, {gap} with "
            f"footage gaps, {failed} failed, {not_retained} with no retained "
            "footage for the window. Review the hold and exclude irrelevant "
            "cameras (reason required)."
        ),
    )
    notifications.notify_footage_hold_complete(
        fp, held=held, gap=gap, failed=failed, not_retained=not_retained,
    )
    short = list(holds.filter(state__in=(
        CameraHold.State.FAILED, CameraHold.State.GAP, CameraHold.State.PENDING,
    )).order_by("pk"))
    if short:
        _notify_manual_hold(
            fp,
            reason=f"{len(short)} camera(s) not confirmed held",
            error=(
                f"{len(short)} camera(s) are not confirmed held; the others "
                "were bookmarked"
            ),
            site_codes=named,
            cameras=[_camera_line(ch) for ch in short],
            condition="partial:" + ",".join(str(ch.pk) for ch in short),
        )
    if not_retained:
        triage.record_not_retained_review(
            fp,
            cameras=[
                _camera_line(ch)
                for ch in holds.filter(state=CameraHold.State.NOT_RETAINED).order_by("pk")
            ],
        )
    if missing:
        _notify_manual_hold(
            fp,
            reason=f"no Aware device group for {triage.describe_sites(missing)}",
            error=(
                f"Aware has no device group for {triage.describe_sites(missing)}; "
                "the other named sites were bookmarked"
            ),
            site_codes=missing,
            condition="missing-groups:" + ",".join(missing),
        )
    if empty:
        _no_cameras_at(fp, empty)


@shared_task(bind=True, max_retries=3)
def build_record_derivative(self, record_item_id: str):
    """Render one non-PDF record into the copy a reviewer can actually read.

    Conversion outcomes are recorded on the record itself rather than raised,
    so a format nothing can render stays visibly unreviewable instead of
    retrying forever. Only an infrastructure fault retries.
    """
    from .derivatives import build_derivative
    from .models import RecordItem

    try:
        item = build_derivative(record_item_id)
    except RecordItem.DoesNotExist:
        return "missing"
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc, countdown=60 * (2**self.request.retries)
            ) from exc
        raise
    return item.derivative_state


@shared_task
def build_pending_record_derivatives(*, limit: int = 25) -> int:
    """Render records that still have no reviewable copy, in bounded batches.

    A safety net rather than the main path: records are normally converted
    when they are created. This catches anything that arrived before the
    converter existed, or whose conversion failed while it was unavailable.
    """
    from .derivatives import build_derivative
    from .handling import CONVERT, handling_for_item
    from .models import RecordItem

    limit = max(1, min(int(limit), 100))
    candidates = RecordItem.objects.filter(
        derivative_state__in=[
            RecordItem.DerivativeState.NONE,
            RecordItem.DerivativeState.PENDING,
            RecordItem.DerivativeState.FAILED,
        ],
        derivative_storage_key="",
    ).order_by("created_at", "pk")

    built = 0
    for item in candidates.iterator():
        if handling_for_item(item) != CONVERT:
            continue
        build_derivative(item.pk)
        built += 1
        if built >= limit:
            break
    return built
