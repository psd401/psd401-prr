"""
Intake triage — the auto-hold / never-auto-deny engine.

Decision table for footage requests (district retention: CAMERA_RETENTION_DAYS):

  Window vs retention        Machine does                       Human does
  ─────────────────────      ────────────────────────────────   ──────────────────────
  Any part still inside  →   HOLD EVERY CAMERA immediately      Exclude irrelevant
                             (saved clips), notify facilities   cameras (reason req'd)
  Entirely outside       →   Queue human review + adequate-     Work checklist, decide,
                             search checklist. NEVER auto-send. draft, approve, send
  No parseable date      →   Hold the FULL current retention    Refine the window
                             window (preservation-first)

Rationale: a late hold destroys evidence (RCW 42.56.100 — records responsive
to a pending request must be preserved); an auto-denial performs no search
and fails the Neighborhood Alliance adequacy standard. So holds are automatic
and denials never are.
"""
from __future__ import annotations

import datetime as dt
import re

from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from . import notifications
from .models import FootagePreservation, PublicRecordsRequest, StatusEvent
from .services import _require_current_records_officer

FOOTAGE_KEYWORDS = re.compile(
    r"\b(footage|video|camera|cctv|surveillance|recording|recorded)\b", re.IGNORECASE
)

_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (re.compile(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE), "mon"),
]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def extract_dates(text: str) -> list[dt.date]:
    found = []
    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                if kind == "ymd":
                    found.append(dt.date(int(m[1]), int(m[2]), int(m[3])))
                elif kind == "mdy":
                    found.append(dt.date(int(m[3]), int(m[1]), int(m[2])))
                else:
                    found.append(dt.date(int(m[3]), _MONTHS[m[1].lower()], int(m[2])))
            except ValueError:
                continue
    return sorted(set(found))


def is_footage_request(description: str) -> bool:
    return bool(FOOTAGE_KEYWORDS.search(description))


def triage_footage(prr: PublicRecordsRequest) -> FootagePreservation | None:
    """Called at intake. Returns the preservation record, or None if this
    isn't a footage request."""
    if not is_footage_request(prr.description):
        return None

    # Migration activation and operator recovery may safely retry triage.
    # The one-to-one preservation row is the durable idempotency boundary;
    # never enqueue a second broad hold for the same legal request.
    existing = FootagePreservation.objects.filter(request=prr).first()
    if existing is not None:
        return existing

    from .tasks import execute_footage_hold  # avoid circular import

    now = timezone.now()
    # Read once: the window this decision was made against is the window the
    # StatusEvent records, so the note can never describe a different rule
    # from the one that was applied.
    retention_days = runtime.get("CAMERA_RETENTION_DAYS")
    retention_floor = now - dt.timedelta(days=retention_days)
    dates = extract_dates(prr.description)
    tz = timezone.get_current_timezone()

    if dates:
        window_start = dt.datetime.combine(min(dates), dt.time.min, tzinfo=tz)
        window_end = dt.datetime.combine(max(dates), dt.time.max, tzinfo=tz)
        note = f"Dates parsed from request: {', '.join(map(str, dates))}"
    else:
        # Preservation-first: no parseable date -> hold everything that exists.
        window_start, window_end = retention_floor, now
        note = "No dates parseable — holding full retention window pending human refinement."

    fp = FootagePreservation.objects.create(
        request=prr, window_start=window_start, window_end=window_end,
        detected_dates_note=note,
    )

    if window_end < retention_floor:
        # Entirely outside retention: human review path. NEVER auto-deny.
        fp.state = FootagePreservation.State.EXPIRED
        fp.save(update_fields=["state"])
        StatusEvent.objects.create(
            request=prr,
            note=(
                "TRIAGE: Requested footage window predates the "
                f"{retention_days}-day retention period. "
                "HUMAN REVIEW REQUIRED — no requester response has been drafted "
                "or queued. HUMAN MUST COMPLETE the adequate-search checklist "
                "before deciding and drafting any response: "
                "(1) incident-hold clips for the window? (2) previously exported "
                "clips (Aware saved clips / downloads)? (3) email & Drive search "
                "for shared copies? (4) SRO / front office copies? "
                "Cite the retention schedule in the response."
            ),
        )
        audit("footage.expired_window", target=prr.number,
              window=f"{window_start:%Y-%m-%d}..{window_end:%Y-%m-%d}")
        notifications.notify_adequate_search_review(fp)
        return fp

    # Some or all footage may still exist: hold NOW, clamp to what exists.
    fp.window_start = max(window_start, retention_floor)
    fp.save(update_fields=["window_start"])
    execute_footage_hold.delay(fp.id)
    audit("footage.hold_initiated", target=prr.number)
    return fp


@transaction.atomic
def exclude_camera(camera_hold, reason: str, *, actor=None):
    """Narrow a hold: release one camera. Human + substantive reason required
    — same gate pattern as estimate revisions."""
    from apps.integrations.avigilon import client as alta

    from .models import CameraHold

    actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Current Records Officer authority is required to exclude a camera."
        ),
    )
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError(
            "Excluding a camera from a preservation hold requires a substantive "
            "reason (e.g. 'camera faces staff parking lot; request concerns the "
            "gym entrance'). The exclusion record is part of the adequate-search "
            "documentation."
        )
    reason_max_length = CameraHold._meta.get_field(
        "exclusion_reason",
    ).max_length
    if reason_max_length is not None and len(reason) > reason_max_length:
        raise ValueError(
            "A camera-exclusion reason must contain at most "
            f"{reason_max_length} characters."
        )
    clip_id = camera_hold.clip_id

    camera_hold.state = CameraHold.State.RELEASED
    camera_hold.excluded_at = timezone.now()
    camera_hold.excluded_by = actor
    camera_hold.exclusion_reason = reason
    camera_hold.save()

    prr = camera_hold.preservation.request
    audit("footage.camera_excluded", target=prr.number, actor=actor,
          camera=camera_hold.camera_name or camera_hold.camera_id, reason=reason)
    if clip_id:
        def delete_clip_after_evidence_commits():
            try:
                alta.delete_clip(clip_id)
            except alta.AltaError as exc:
                audit(
                    "footage.camera_release_failed",
                    target=prr.number,
                    actor=actor,
                    camera=camera_hold.camera_name or camera_hold.camera_id,
                    error=str(exc)[:500],
                )

        transaction.on_commit(delete_clip_after_evidence_commits)
    return camera_hold


def route_to_systems(prr: PublicRecordsRequest) -> list:
    """Match the request against the RecordSystem registry and create a
    manual CollectionTask per matching system, pre-loaded with that system's
    adequate-search checklist and custodian. urgent_hold systems (retention
    races, e.g. bus DVRs) additionally fire an immediate preservation email —
    same principle as the Avigilon hold: preserve first, scope later."""
    from .deadlines import add_business_days
    from .models import CollectionTask, RecordSystem

    created = []
    for system in RecordSystem.objects.filter(active=True):
        if not system.triage_keywords or not system.matches(prr.description):
            continue

        task, task_created = CollectionTask.objects.get_or_create(
            request=prr,
            system=system,
            kind=CollectionTask.Kind.MANUAL,
            defaults={
                "instructions": (
                    f"[{system.name}] Custodian: "
                    f"{system.custodian_title or 'UNASSIGNED'}"
                    f"{' <' + system.custodian_email + '>' if system.custodian_email else ''}\n"
                    f"Retention: {system.retention_note or 'n/a'}\n\n"
                    f"Checklist:\n{system.search_checklist}"
                ),
                "due_date": add_business_days(
                    timezone.localdate(), 1 if system.urgent_hold else 5
                ),
                "assigned_to": system.default_assignee,
            },
        )
        if not task_created:
            continue
        created.append(task)

        from apps.work_queue.integration import (
            schedule_source_work_item_reconciliation,
        )

        schedule_source_work_item_reconciliation("collections", task.pk)

        StatusEvent.objects.create(
            request=prr,
            note=f"TRIAGE: routed to {system.name} "
                 f"(custodian: {system.custodian_title or 'UNASSIGNED'})"
                 + (" — URGENT preservation required." if system.urgent_hold else "."),
        )
        audit("triage.system_matched", target=prr.number, system=system.slug,
              urgent=system.urgent_hold)
        notifications.notify_collection_task(task)
    return created
