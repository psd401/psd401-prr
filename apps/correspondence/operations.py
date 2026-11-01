# ABOUTME: PII-free correspondence queue metrics and dependency readiness.
# ABOUTME: Exposes only fixed status/error codes suitable for staff operations.
from __future__ import annotations

import datetime as dt

import redis
from django.conf import settings
from django.db.models import (
    Count,
    DateTimeField,
    Min,
    OuterRef,
    Q,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps import secure_files
from apps.configuration import runtime

from . import gmail_sync, services, transport
from .models import (
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDeliveryNotification,
    GmailSyncState,
    InboundCorrespondenceReceipt,
)

SAFE_FAILURE_CATEGORIES = frozenset(
    {
        *services.RETRYABLE_DELIVERY_FAILURES,
        *services.PERMANENT_DELIVERY_FAILURES,
        "lease_expired",
        "outcome_unknown",
        "reconciliation_not_found",
    }
)
ACTIVE_DELIVERY_STATUSES = frozenset(
    {
        CorrespondenceDelivery.Status.PENDING,
        CorrespondenceDelivery.Status.CLAIMED,
        CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
    }
)
OUTSTANDING_DELIVERY_STATUSES = frozenset(
    {
        *ACTIVE_DELIVERY_STATUSES,
        CorrespondenceDelivery.Status.RETRYABLE,
        CorrespondenceDelivery.Status.FAILED,
    }
)
UNMATCHED_INBOUND_STATUSES = frozenset(
    {
        InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
    }
)


def gmail_inbound_status_snapshot(*, now: dt.datetime | None = None) -> dict:
    """Return PII-free Gmail poller configuration and checkpoint freshness."""
    observed_at = now or timezone.now()
    gmail_enabled = bool(runtime.get("PRR_GMAIL_ENABLED"))
    inbound_enabled = bool(
        runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED")
    )
    mailbox = runtime.get("PRR_GMAIL_MAILBOX")
    environment = str(getattr(settings, "PRR_ENVIRONMENT", "")).strip()
    stale_after_seconds = runtime.get("PRR_GMAIL_INBOUND_STALE_SECONDS")
    state = None
    if mailbox and environment:
        state = GmailSyncState.objects.filter(
            mailbox=mailbox,
            environment=environment,
        ).first()
    last_synced_at = state.last_synced_at if state is not None else None
    age_seconds = _age_seconds(observed_at, last_synced_at)

    if not inbound_enabled:
        status = "disabled"
        healthy = False
    elif not gmail_enabled:
        status = "gmail_disabled"
        healthy = False
    elif last_synced_at is None:
        status = "never_synced"
        healthy = False
    elif age_seconds is None or age_seconds > stale_after_seconds:
        status = "stale"
        healthy = False
    else:
        status = "healthy"
        healthy = True

    return {
        "observed_at": observed_at.isoformat(),
        "status": status,
        "healthy": healthy,
        "configuration": {
            "environment": environment,
            "gmail_enabled": gmail_enabled,
            "inbound_poll_enabled": inbound_enabled,
            "mailbox_configured": bool(mailbox),
        },
        "checkpoint": {
            "present": state is not None,
            "history_present": bool(state and state.history_id),
            "last_synced_at": (
                last_synced_at.isoformat() if last_synced_at is not None else None
            ),
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after_seconds,
        },
    }


def _age_seconds(now: dt.datetime, timestamp: dt.datetime | None) -> int | None:
    if timestamp is None:
        return None
    return max(0, int((now - timestamp).total_seconds()))


def _stuck_deliveries(deliveries, *, now: dt.datetime, cutoff: dt.datetime):
    latest_attempt_activity = (
        CorrespondenceDeliveryAttempt.objects.filter(
            delivery_id=OuterRef("pk"),
        )
        .annotate(activity=Coalesce("result_at", "claimed_at"))
        .order_by("-attempt_number")
        .values("activity")[:1]
    )
    expired_claim = Q(
        status__in=[
            CorrespondenceDelivery.Status.CLAIMED,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        ],
        claim_expires_at__lte=now,
    )
    due_unclaimed = (
        Q(
            status__in=[
                CorrespondenceDelivery.Status.PENDING,
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            ],
            claim_owner="",
            claim_expires_at__isnull=True,
            queued_at__lte=cutoff,
        )
        & (
            Q(recovery_not_before__isnull=True)
            | Q(recovery_not_before__lte=now)
        )
        & (
            Q(latest_attempt_activity__isnull=True)
            | Q(latest_attempt_activity__lte=cutoff)
        )
    )
    return deliveries.annotate(
        latest_attempt_activity=Subquery(
            latest_attempt_activity,
            output_field=DateTimeField(),
        )
    ).filter(expired_claim | due_unclaimed)


def _failure_category_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    grouped = (
        CorrespondenceDelivery.objects.filter(
            status__in=OUTSTANDING_DELIVERY_STATUSES,
        )
        .exclude(last_failure_category="")
        .values("last_failure_category")
        .annotate(total=Count("id"))
    )
    for row in grouped:
        category = row["last_failure_category"]
        safe_category = category if category in SAFE_FAILURE_CATEGORIES else "other"
        counts[safe_category] = counts.get(safe_category, 0) + row["total"]
    return dict(sorted(counts.items()))


def operations_snapshot(*, now: dt.datetime | None = None) -> dict:
    """Return aggregate operational state without correspondence content or PII."""
    observed_at = now or timezone.now()
    stuck_seconds = runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS")
    cutoff = observed_at - dt.timedelta(seconds=stuck_seconds)

    deliveries = CorrespondenceDelivery.objects.all()
    stuck_deliveries = _stuck_deliveries(
        deliveries,
        now=observed_at,
        cutoff=cutoff,
    )
    status_counts = deliveries.aggregate(
        queued=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.PENDING),
        ),
        claimed=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.CLAIMED),
        ),
        awaiting_reconciliation=Count(
            "id",
            filter=Q(
                status=(
                    CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
                )
            ),
        ),
        retryable=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.RETRYABLE),
        ),
        failed=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.FAILED),
        ),
        sent=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.SENT),
        ),
        cancelled=Count(
            "id",
            filter=Q(status=CorrespondenceDelivery.Status.CANCELLED),
        ),
    )
    status_counts["stuck"] = stuck_deliveries.count()

    receipts = InboundCorrespondenceReceipt.objects.all()
    inbound_counts = receipts.aggregate(
        inbound=Count("id"),
        unmatched=Count(
            "id",
            filter=Q(matching_status__in=UNMATCHED_INBOUND_STATUSES),
        ),
        quarantined_messages=Count(
            "id",
            filter=(
                Q(quarantined=True)
                | Q(
                    parsing_status=(
                        InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED
                    )
                )
                | Q(
                    matching_status=(
                        InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
                    )
                )
            ),
        ),
    )
    counts = {
        **status_counts,
        **inbound_counts,
        "quarantined_attachments": (
            CorrespondenceAttachment.objects.filter(quarantined=True).count()
        ),
        "delivery_exceptions": (
            CorrespondenceDeliveryNotification.objects.filter(
                action__in=[
                    CorrespondenceDeliveryNotification.Action.FAILED,
                    CorrespondenceDeliveryNotification.Action.DELAYED,
                ]
            ).count()
        ),
    }

    oldest_delivery = deliveries.filter(
        status__in=OUTSTANDING_DELIVERY_STATUSES
    ).aggregate(value=Min("queued_at"))["value"]
    oldest_claim = CorrespondenceDeliveryAttempt.objects.filter(
        result_at__isnull=True,
    ).aggregate(value=Min("claimed_at"))["value"]
    oldest_stuck = stuck_deliveries.aggregate(value=Min("queued_at"))[
        "value"
    ]

    return {
        "generated_at": observed_at.isoformat(),
        "counts": counts,
        "ages_seconds": {
            "oldest_delivery": _age_seconds(observed_at, oldest_delivery),
            "oldest_claim": _age_seconds(observed_at, oldest_claim),
            "oldest_stuck": _age_seconds(observed_at, oldest_stuck),
        },
        "failure_categories": _failure_category_counts(),
    }


def _redis_probe_client():
    client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
    client.connection_pool.connection_kwargs.update(
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=False,
        retry_on_error=[],
        health_check_interval=0,
    )
    return client


def probe_redis(*, _client=None) -> bool:
    """Perform one bounded Redis PING without exposing broker configuration."""
    client = _client
    owns_client = client is None
    if client is None:
        client = _redis_probe_client()
    try:
        return bool(client.ping())
    finally:
        if owns_client:
            client.close()


def _fixed_check(
    component: str,
    ready: bool,
    *,
    ready_code: str,
    not_ready_code: str,
) -> dict[str, str]:
    return {
        "component": component,
        "status": "ready" if ready else "not_ready",
        "code": ready_code if ready else not_ready_code,
    }


def _safe_probe(probe) -> bool:
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - an unreachable dependency reads as not ready
        return False


def readiness_snapshot() -> dict:
    """Probe required dependencies and return only fixed operational codes."""
    outbound_enabled = bool(
        runtime.get("PRR_CORRESPONDENCE_OUTBOUND_ENABLED")
    )
    storage_ready = _safe_probe(secure_files.probe_object_storage)
    scanner_ready = _safe_probe(secure_files.probe_clamav)
    broker_ready = _safe_probe(probe_redis)
    gmail_ready = bool(
        runtime.get("PRR_GMAIL_ENABLED")
        and runtime.get("PRR_GMAIL_MAILBOX")
        and _safe_probe(transport.system_transport_principal)
        and _safe_probe(gmail_sync.system_inbound_principal)
    )

    checks = [
        _fixed_check(
            "outbound_transport",
            outbound_enabled,
            ready_code="enabled",
            not_ready_code="disabled",
        ),
        _fixed_check(
            "object_storage",
            storage_ready,
            ready_code="available",
            not_ready_code="unavailable",
        ),
        _fixed_check(
            "malware_scanner",
            scanner_ready,
            ready_code="available",
            not_ready_code="unavailable",
        ),
        _fixed_check(
            "task_broker",
            broker_ready,
            ready_code="available",
            not_ready_code="unavailable",
        ),
        _fixed_check(
            "gmail_transport",
            gmail_ready,
            ready_code="configured",
            not_ready_code="unavailable",
        ),
    ]
    return {
        "ready": all(check["status"] == "ready" for check in checks),
        "checks": checks,
    }
