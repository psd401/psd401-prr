"""
Intake triage — the auto-hold / never-auto-deny engine.

Decision table for footage requests. Scope is every camera at each site the
request names (footage_sites), or every camera district-wide when it names
none. Retention is per camera: Aware's oldest retained footage (pruned_to).

  Situation                  Machine does                       Human does
  ─────────────────────      ────────────────────────────────   ──────────────────────
  Footage retained       →   Aware bookmark per camera, no      Exclude irrelevant
                             expiry, from the later of window   cameras (reason req'd);
                             start and oldest footage; GAP      release only after the
                             when coverage is under 95%         request closes
  Named site, 0 cameras  →   NO_CAMERAS: nothing bookmarked,    Adequate search: other
                             review queued, never "held"        recorders, SRO/office
  No camera retains it   →   EXPIRED: review + adequate-search  Work checklist, decide,
                             checklist. NEVER auto-send.        draft, approve, send
  API down / all failed  →   MANUAL: urgent email to            Bookmark by hand in
                             facilities (EXPIRED instead when   Aware ("Manually
                             the window predates the setting)   deleted")
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

from . import footage_sites, notifications
from .models import FootagePreservation, PublicRecordsRequest, StatusEvent
from .services import _require_current_records_officer

BOOKMARK_NAME_MAX_LENGTH = 100
BOOKMARK_DESCRIPTION = (
    "Public Records Act preservation hold. Do not delete. "
    "Managed by the PRR system."
)
OUTSIDE_RETENTION_CHECKLIST = (
    "HUMAN REVIEW REQUIRED — no requester response has been drafted "
    "or queued. HUMAN MUST COMPLETE the adequate-search checklist "
    "before deciding and drafting any response: "
    "(1) incident-hold clips for the window? (2) previously exported "
    "footage (Aware bookmarks / downloads)? (3) email & Drive search "
    "for shared copies? (4) SRO / front office copies? "
    "Cite the retention schedule in the response."
)

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


def calendar_day_window(dates: list[dt.date]) -> tuple[dt.datetime, dt.datetime]:
    """[first date 00:00, day after the last date 00:00) in district time,
    returned in UTC. A DST day is 23 or 25 hours; zoneinfo resolves the
    offsets, and UTC results keep later arithmetic in elapsed time."""
    tz = timezone.get_current_timezone()
    start = dt.datetime.combine(min(dates), dt.time.min, tzinfo=tz)
    end = dt.datetime.combine(max(dates) + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def describe_sites(codes) -> str:
    return ", ".join(f"{footage_sites.site_name(code)} ({code})" for code in codes)


def bookmark_name(prr: PublicRecordsRequest, camera_name: str) -> str:
    prefix = f"{prr.number} PRA hold — "
    name = prefix + (camera_name or "").strip()
    if len(name) <= BOOKMARK_NAME_MAX_LENGTH:
        return name
    return name[: BOOKMARK_NAME_MAX_LENGTH - 1].rstrip() + "…"


def _local_dates(fp: FootagePreservation) -> str:
    start = timezone.localtime(fp.window_start).date()
    last = timezone.localtime(fp.window_end - dt.timedelta(microseconds=1)).date()
    return f"{start:%Y-%m-%d}..{last:%Y-%m-%d}"


def record_outside_retention_review(fp: FootagePreservation, *, reason: str) -> FootagePreservation:
    """EXPIRED: nothing is held for the window. Queue the human review and the
    adequate-search checklist. NEVER auto-deny; no requester response. Triage
    and the hold task both come through here so the checklist never drifts."""
    fp.state = FootagePreservation.State.EXPIRED
    fp.save(update_fields=["state"])
    StatusEvent.objects.create(
        request=fp.request,
        note=f"TRIAGE: {reason} {OUTSIDE_RETENTION_CHECKLIST}",
    )
    audit("footage.expired_window", target=fp.request.number, window=_local_dates(fp))
    notifications.notify_adequate_search_review(fp, note=reason)
    return fp


def record_no_cameras_review(fp: FootagePreservation, *, site_codes) -> None:
    """A named site has no cameras in Aware: nothing to bookmark there, but
    footage may sit on another recorder. Queue the adequate-search review;
    never describe this as preserved, and never answer the requester."""
    site_codes = list(site_codes)
    StatusEvent.objects.create(
        request=fp.request,
        note=(
            f"Footage preservation: Aware has no cameras at "
            f"{describe_sites(site_codes)}, so nothing was bookmarked there. "
            "HUMAN REVIEW REQUIRED — no requester response has been drafted "
            "or queued. Complete the adequate-search review before deciding "
            "any response: (1) another recorder at the site (standalone "
            "DVR/NVR, doorbell or intercom camera)? (2) front-office or SRO "
            "copies? (3) email & Drive search for shared copies? "
            "(4) facilities confirmation of what, if anything, records the "
            "location?"
        ),
    )
    audit("footage.no_cameras_at_site", target=fp.request.number, sites=site_codes)
    notifications.notify_adequate_search_review(fp, no_camera_sites=site_codes)


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
    retention_floor = now - dt.timedelta(days=runtime.get("CAMERA_RETENTION_DAYS"))
    dates = extract_dates(prr.description)

    if dates:
        window_start, window_end = calendar_day_window(dates)
        note = f"Dates parsed from request: {', '.join(map(str, dates))}"
    else:
        # Preservation-first: no parseable date -> hold everything that exists.
        window_start, window_end = retention_floor, now
        note = "No dates parseable — holding full retention window pending human refinement."

    # rule 9: the description is the immutable legal record, so the scope
    # decision can always be reproduced from it.
    sites = footage_sites.resolve_sites(prr.description)
    if sites:
        resolution = (
            f"Sites named in the request: {describe_sites(s.code for s in sites)}. "
            "Holding every camera in those Aware groups."
        )
    else:
        resolution = (
            "No district site named in the request: holding every camera "
            "district-wide. A Records Officer narrows the hold by excluding "
            "cameras with a reason."
        )

    # The REQUESTED window is stored unclamped. Retention is per camera
    # (Aware pruned_to), so even a window older than the flat setting goes to
    # the hold task: some cameras keep footage far longer.
    fp = FootagePreservation.objects.create(
        request=prr, window_start=window_start, window_end=window_end,
        detected_dates_note=note,
        site_codes=[site.code for site in sites],
        site_resolution=resolution[:500],
    )
    audit("footage.hold_initiated", target=prr.number,
          sites=fp.site_codes, window=_local_dates(fp))
    execute_footage_hold.delay(fp.id)
    fp.refresh_from_db()
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
    bookmark_id = camera_hold.clip_id

    camera_hold.state = CameraHold.State.RELEASED
    camera_hold.excluded_at = timezone.now()
    camera_hold.excluded_by = actor
    camera_hold.exclusion_reason = reason
    camera_hold.save()

    prr = camera_hold.preservation.request
    audit("footage.camera_excluded", target=prr.number, actor=actor,
          camera=camera_hold.camera_name or camera_hold.camera_id, reason=reason)
    if bookmark_id:
        def delete_bookmark_after_evidence_commits():
            try:
                alta.delete_bookmark(bookmark_id)
            except alta.AltaError as exc:
                audit(
                    "footage.camera_release_failed",
                    target=prr.number,
                    actor=actor,
                    camera=camera_hold.camera_name or camera_hold.camera_id,
                    error=str(exc)[:500],
                )

        transaction.on_commit(delete_bookmark_after_evidence_commits)
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
