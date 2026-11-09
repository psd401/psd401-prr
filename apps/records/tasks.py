"""
Background jobs. The deadline watchdog is the single most important piece of
Phase 1 — RCW 42.56.550 penalties accrue per day, so a missed 5-day
acknowledgment is the district's most expensive possible bug.
"""
import logging
from importlib import import_module

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
def prepare_scope_letters(*, limit: int = 10) -> int:
    """Draft the informational scope letter for cases with a finished count.

    Drafts only: a person approves and sends each letter.
    """
    from .scope_letters import prepare_scope_letters as prepare

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


#: Vault's export-creation quota (HTTP 429) refuses the third or fourth export
#: created within a minute (production, 2026-11-08). A refused pull waits two
#: minutes per try, about an hour in all, instead of the short fault budget.
VAULT_RATE_LIMIT_RETRIES = 30
#: Any other provider fault: the original short budget, counted on its own so
#: earlier rate-limit waits never use it up.
VAULT_FAULT_RETRIES = 4


@shared_task(bind=True, max_retries=VAULT_RATE_LIMIT_RETRIES + VAULT_FAULT_RETRIES)
def execute_vault_collection(
    self, collection_task_id: int, rate_limit_retries: int = 0, fault_retries: int = 0,
):
    """Start one confirmed Vault hold/export with bounded safe retries."""
    from apps.integrations.vault.client import is_rate_limited

    from .vault_collections import fail_vault_collection, start_vault_collection

    try:
        start_vault_collection(collection_task_id)
    except Exception as exc:  # noqa: BLE001 - a provider fault retries, then fails the collection
        limited = is_rate_limited(exc)
        if limited:
            spent, budget = rate_limit_retries, VAULT_RATE_LIMIT_RETRIES
        else:
            spent, budget = fault_retries, VAULT_FAULT_RETRIES
        if spent >= budget:
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
        countdown = 120 if limited else min(30 * (2 ** fault_retries), 300)
        raise self.retry(
            exc=_VaultCollectionRetry(
                "Vault collection provider is temporarily unavailable."
            ),
            countdown=countdown,
            kwargs={
                "rate_limit_retries": rate_limit_retries + int(limited),
                "fault_retries": fault_retries + int(not limited),
            },
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
            from .models import VaultCountRun

            run = VaultCountRun.objects.only("source_counts").get(pk=run_id)
            if "mail" in (run.source_counts or {}):
                # Gmail is counted; a Drive or Chat count-only export can
                # take longer than this fast poll. The export poller in
                # poll_vault_exports finishes it, bounded by its own timeout.
                return
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
    _advance_vault_counts()


def _advance_vault_counts():
    """Finish Drive and Chat counts, and delete count-only exports."""
    from .vault_counts import advance_vault_count, runs_waiting_on_sources

    for run_id in list(runs_waiting_on_sources()):
        try:
            advance_vault_count(run_id)
        except Exception:  # noqa: BLE001 - one run's provider fault must not stop the others
            # Provider text can echo the query; keep the log to the run id.
            logger.error(
                "Vault count could not be advanced; provider detail suppressed",
                extra={"vault_count_run": str(run_id)},
            )


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


@shared_task
def execute_footage_hold(preservation_id: int):
    """Create saved clips on EVERY camera for the preservation window.
    Broad by default — narrowing is a human decision (triage.exclude_camera).
    If the Aware API isn't configured/reachable, degrade to a MANUAL hold:
    urgent email to facilities with exact instructions, because the retention
    clock does not care about our API status."""
    from apps.integrations.avigilon import client as alta

    from .models import CameraHold, FootagePreservation, StatusEvent

    fp = FootagePreservation.objects.select_related("request").get(pk=preservation_id)
    prr = fp.request

    try:
        cameras = alta.list_cameras()
    except alta.AltaError as exc:
        fp.state = FootagePreservation.State.MANUAL
        fp.save(update_fields=["state"])
        notification_result = notifications.notify_footage_manual_hold(
            fp,
            error=str(exc),
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
            request=prr,
            note=(
                "Footage preservation: API unavailable — URGENT manual hold "
                + notification_note
            ),
        )
        return

    held, failed = 0, 0
    for cam in cameras:
        ch = CameraHold.objects.create(
            preservation=fp, camera_id=str(cam["id"]),
            camera_name=cam.get("name", ""), site=cam.get("site", ""),
        )
        try:
            clip_id = alta.create_clip_hold(
                str(cam["id"]), fp.window_start, fp.window_end,
                label=f"{prr.number} PRA hold",
            )
            ch.clip_id = clip_id
            ch.state = CameraHold.State.HELD
            held += 1
        except Exception as exc:  # noqa: BLE001 — per-camera isolation
            ch.state = CameraHold.State.FAILED
            ch.error = str(exc)[:500]
            failed += 1
        ch.save()

    fp.state = (FootagePreservation.State.HELD if failed == 0
                else FootagePreservation.State.PARTIAL)
    fp.save(update_fields=["state"])

    StatusEvent.objects.create(
        request=prr,
        note=f"Footage preservation: {held} camera(s) held, {failed} failed. "
             "Review the hold and exclude irrelevant cameras (reason required).",
    )
    notifications.notify_footage_hold_complete(fp, held=held, failed=failed)


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
