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
  Named site(s)          →   Hold those sites only; URGENT      Widen to district-wide
                             scope notice says so               if any place is missed
  Span > the cap         →   Hold the most recent               Refine the window, hold
                             CAMERA_HOLD_MAX_SPAN_DAYS; URGENT  the rest by hand
                             scope notice
  Named site, 0 cameras  →   NO_CAMERAS: nothing bookmarked,    Adequate search: other
                             review + URGENT notice, never      recorders, SRO/office
                             "held"
  Gap/failed/unverified  →   PARTIAL: URGENT notice naming      Bookmark the named
                             each short camera                  cameras by hand
  No camera retains it   →   EXPIRED: review + adequate-search  Work checklist, decide,
                             checklist. NEVER auto-send.        draft, approve, send
  API down / all failed  →   MANUAL: urgent email to            Bookmark by hand in
                             facilities, whatever the window's  Aware ("Manually
                             age                                deleted")
  Task crashed / stalled →   MANUAL: finally-block, then the    Same as MANUAL
                             5-minute sweeper (PENDING > 15m)
  No parseable date      →   Hold the FULL current retention    Refine the window
                             window (preservation-first)
  Release delete fails   →   Camera RELEASE_FAILED, logged      Post release again to
                                                                retry those bookmarks

Rationale: a late hold destroys evidence (RCW 42.56.100 — records responsive
to a pending request must be preserved); an auto-denial performs no search
and fails the Neighborhood Alliance adequacy standard. So holds are automatic
and denials never are.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import cast

from celery import Task
from django.db import transaction
from django.db.models import Q
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
    "footage (Aware bookmarks / downloads), including other users' bookmarks "
    "on each device, which may still hold older footage? (3) email & Drive search "
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


def _cameras(count: int) -> str:
    return f"{count} camera" if count == 1 else f"{count} cameras"


def preservation_confirmation_text(fp: FootagePreservation) -> str | None:
    """One sentence a Records Officer may copy into correspondence, e.g. "12
    cameras at Peninsula High School held from 2026-09-22 00:00 to 2026-09-23
    00:00 Pacific time (Aware bookmarks, no expiry); 1 camera has a footage
    gap; 0 failed." Built from the hold rows, so it says only what Aware
    confirmed. None unless the preservation is HELD or PARTIAL. Never sent
    automatically: a human chooses whether and where to use it."""
    from .models import CameraHold

    if fp.state not in (
        FootagePreservation.State.HELD,
        FootagePreservation.State.PARTIAL,
    ):
        return None
    rows = [hold for hold in fp.camera_holds.all() if hold.excluded_at is None]
    bookmarked = [hold for hold in rows if hold.state in CameraHold.BOOKMARKED_STATES]
    if not bookmarked:   # every bookmarked camera was excluded
        return None
    gaps = sum(hold.state == CameraHold.State.GAP for hold in rows)
    not_retained = sum(hold.state == CameraHold.State.NOT_RETAINED for hold in rows)
    pending = sum(hold.state == CameraHold.State.PENDING for hold in rows)
    failed = sum(hold.state == CameraHold.State.FAILED for hold in rows)

    if fp.site_codes:
        held_sites = {(hold.site or "").strip().upper() for hold in bookmarked}
        held_codes = [code for code in fp.site_codes if code.upper() in held_sites]
        unheld_codes = [code for code in fp.site_codes if code not in held_codes]
        where = "at " + " and ".join(
            footage_sites.site_name(code) for code in held_codes
        )
    else:
        unheld_codes = []
        where = "across the district"

    start = timezone.localtime(fp.window_start)
    end = timezone.localtime(fp.window_end)
    parts = [
        (
            f"{_cameras(len(bookmarked))} {where} held from "
            f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} Pacific time "
            "(Aware bookmarks, no expiry)"
        ),
        f"{_cameras(gaps)} has a footage gap"
        if gaps == 1
        else f"{_cameras(gaps)} have footage gaps",
    ]
    if not_retained:
        parts.append(f"{not_retained} with no retained footage for the window")
    if pending:   # not yet attempted, which is not the same as failed
        parts.append(f"{pending} pending")
    parts.append(f"{failed} failed")
    if unheld_codes:
        parts.append(
            "none held at "
            + " and ".join(footage_sites.site_name(code) for code in unheld_codes)
        )
    return "; ".join(parts) + "."


def _date_range(start: dt.datetime, end: dt.datetime) -> str:
    """Local calendar days of the half-open window [start, end)."""
    first = timezone.localtime(start).date()
    last = timezone.localtime(end - dt.timedelta(microseconds=1)).date()
    return f"{first:%Y-%m-%d}..{last:%Y-%m-%d}"


def _local_dates(fp: FootagePreservation) -> str:
    return _date_range(fp.window_start, fp.window_end)


def record_scope_narrowed(fp: FootagePreservation, *, reasons) -> None:
    """The automatic hold covers less than the request asked for. Say so on
    the timeline and send an URGENT notice: only a human can widen it."""
    reasons = list(reasons)
    StatusEvent.objects.create(
        request=fp.request,
        note=(
            "HOLD SCOPE: the automatic hold is narrower than the request. "
            + " ".join(reasons)
        )[:2000],
    )
    audit("footage.hold_scope_narrowed", target=fp.request.number,
          window=_local_dates(fp), sites=fp.site_codes)
    notifications.notify_footage_scope_narrowed(fp, reasons=reasons)


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


NOT_RETAINED_NOTE = (
    "Check other users' bookmarks on these devices in Aware: they may still "
    "hold older footage, so do not conclude that none exists until you have."
)


def record_not_retained_review(fp: FootagePreservation, *, cameras) -> None:
    """Part of a hold went ahead, but some cameras no longer retain the
    window. Queue the adequate-search checklist for those cameras; the
    preservation keeps its state, and no requester response is drafted."""
    cameras = list(cameras)
    StatusEvent.objects.create(
        request=fp.request,
        note=(
            f"TRIAGE: Aware retains no footage for the window on {len(cameras)} "
            f"camera(s): {'; '.join(cameras)[:1000]}. {NOT_RETAINED_NOTE} "
            f"{OUTSIDE_RETENTION_CHECKLIST}"
        ),
    )
    audit("footage.cameras_not_retained", target=fp.request.number, cameras=len(cameras))
    notifications.notify_adequate_search_review(
        fp, not_retained_cameras=cameras, note=NOT_RETAINED_NOTE,
    )


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

    narrowed = []
    if dates:
        window_start, window_end = calendar_day_window(dates)
        note = f"Dates parsed from request: {', '.join(map(str, dates))}"
        # Count calendar days, not elapsed time: a DST day is 23 or 25 hours.
        cap = runtime.get("CAMERA_HOLD_MAX_SPAN_DAYS")
        first, last = dates[0], dates[-1]
        if (last - first).days + 1 > cap:
            requested = _date_range(window_start, window_end)
            window_start, window_end = calendar_day_window(
                [last - dt.timedelta(days=cap - 1), last],
            )
            held = _date_range(window_start, window_end)
            note = f"{note}. Span clipped to the most recent {cap} days: {held}."
            narrowed.append(
                f"The request's dates span {requested}, longer than the "
                f"{cap}-day automatic hold limit, so only {held} is held. "
                "A human must refine the window and hold any earlier footage "
                "the request covers."
            )
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
        narrowed.append(
            f"The hold covers only {describe_sites(s.code for s in sites)}. "
            "If the request names any other place, a human must widen the "
            "hold to every camera district-wide."
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
        detected_dates_note=note[:500],
        site_codes=[site.code for site in sites],
        site_resolution=resolution[:500],
    )
    audit("footage.hold_initiated", target=prr.number,
          sites=fp.site_codes, window=_local_dates(fp))
    if narrowed:
        record_scope_narrowed(fp, reasons=narrowed)
    cast(Task, execute_footage_hold).delay(fp.id)
    fp.refresh_from_db()
    return fp


@transaction.atomic
def exclude_camera(camera_hold, reason: object, *, actor=None):
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
    reason = _reason_text(reason)
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
    preservation = FootagePreservation.objects.select_for_update().get(
        pk=camera_hold.preservation_id,
    )
    if preservation.state == FootagePreservation.State.RELEASED:
        raise ValueError(
            "This preservation is already released; there is no hold left to "
            "narrow."
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
            from .tasks import _safe_alta_error

            try:
                alta.delete_bookmark(bookmark_id)
            # The exclusion is committed: record the failure, don't raise.
            except Exception as exc:  # noqa: BLE001
                audit(
                    "footage.camera_release_failed",
                    target=prr.number,
                    actor=actor,
                    camera=camera_hold.camera_name or camera_hold.camera_id,
                    error=_safe_alta_error(exc, action="bookmark delete"),
                )

        transaction.on_commit(delete_bookmark_after_evidence_commits)
    return camera_hold


def _reason_text(reason: object) -> str:
    """A reason must be text. Anything else (a list, a number, a JSON object
    from an API body) is refused like an empty one, never coerced."""
    if reason is None:
        return ""
    if not isinstance(reason, str):
        raise ValueError("A reason must be text.")
    return reason.strip()


WIDEN_REASON_MAX_LENGTH = 500


@transaction.atomic
def widen_preservation(
    preservation: FootagePreservation, *, actor, reason: object,
) -> FootagePreservation:
    """Widen a site-narrowed hold to every camera district-wide. Widening
    only preserves more, so it needs a current Records Officer and a reason
    but no handoff. The hold task then bookmarks only cameras that have no
    hold row yet: a camera already tried keeps its outcome, and a failed
    create that may have landed is never repeated."""
    from .tasks import execute_footage_hold  # avoid circular import

    actor = _require_current_records_officer(
        actor=actor,
        message="Current Records Officer authority is required to widen a hold.",
    )
    reason = _reason_text(reason)
    if len(reason) < 10:
        raise ValueError(
            "Widening a preservation hold requires a substantive reason (e.g. "
            "'request also names the bus loop beside the gym')."
        )
    if len(reason) > WIDEN_REASON_MAX_LENGTH:
        raise ValueError(
            "A widen reason must contain at most "
            f"{WIDEN_REASON_MAX_LENGTH} characters."
        )
    fp = FootagePreservation.objects.select_for_update().select_related(
        "request",
    ).get(pk=preservation.pk)
    if fp.state == FootagePreservation.State.RELEASED:
        raise ValueError("A released preservation cannot be widened.")
    if not fp.site_codes:
        raise ValueError("This hold already covers every camera district-wide.")

    previous = list(fp.site_codes)
    fp.site_codes = []
    fp.site_resolution = (
        f"Widened district-wide from {describe_sites(previous)} by "
        f"{actor.get_username()}: {reason}"
    )[:500]
    fp.save(update_fields=["site_codes", "site_resolution"])
    audit("footage.hold_widened", target=fp.request.number, actor=actor,
          previous_sites=previous, reason=reason)
    StatusEvent.objects.create(
        request=fp.request,
        actor=actor,
        note=(
            f"Footage hold widened from {describe_sites(previous)} to every "
            f"camera district-wide. Reason: {reason}"
        ),
    )
    transaction.on_commit(
        lambda: cast(Task, execute_footage_hold).delay(fp.id, only_new=True)
    )
    return fp


RELEASABLE_PRESERVATION_STATES = (
    FootagePreservation.State.HELD,
    FootagePreservation.State.PARTIAL,
)


def is_releasable(fp: FootagePreservation) -> bool:
    """Whether release_preservation would accept this preservation now, the
    actor and reason aside: the request and any case it was merged into are
    closed, and it holds footage or has bookmarks left to retry."""
    from .models import CameraHold
    from .services import CLOSED_STATUSES

    prr = fp.request
    if prr.status not in CLOSED_STATUSES:
        return False
    target = prr.merged_into
    if target is not None and target.status not in CLOSED_STATUSES:
        return False
    if fp.state in RELEASABLE_PRESERVATION_STATES:
        return True
    # all() so a prefetched list is read, not queried again.
    return fp.state == FootagePreservation.State.RELEASED and any(
        hold.state == CameraHold.State.RELEASE_FAILED for hold in fp.camera_holds.all()
    )


@transaction.atomic
def release_preservation(
    preservation: FootagePreservation,
    *,
    actor,
    reason: object,
    attest_no_litigation_hold: object,
) -> FootagePreservation:
    """End a preservation hold for good: Aware deletes every bookmark and the
    footage then ages out. Human only, like exclude_camera: a current Records
    Officer, a substantive reason, an attestation that no litigation hold
    covers the footage, and a closed request. A duplicate's hold still holds
    footage for its case of record, so that case must be closed too."""
    from apps.integrations.avigilon import client as alta

    from .models import CameraHold
    from .services import CLOSED_STATUSES

    actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Current Records Officer authority is required to release a "
            "preservation hold."
        ),
    )
    reason = _reason_text(reason)
    if len(reason) < 10:
        raise ValueError(
            "Releasing a preservation hold requires a substantive reason (e.g. "
            "'request fulfilled; installment produced and the requester "
            "confirmed receipt'). The release record is part of the retention "
            "documentation."
        )
    reason_max_length = FootagePreservation._meta.get_field(
        "release_reason",
    ).max_length
    if reason_max_length is not None and len(reason) > reason_max_length:
        raise ValueError(
            "A preservation-release reason must contain at most "
            f"{reason_max_length} characters."
        )
    if attest_no_litigation_hold is not True:
        raise ValueError(
            "Releasing a preservation hold requires attesting that no "
            "litigation hold, pending lawsuit, or reasonably anticipated "
            "litigation covers this footage."
        )

    fp = (
        FootagePreservation.objects.select_for_update()
        .select_related("request__merged_into")
        .get(pk=preservation.pk)
    )
    retry = fp.state == FootagePreservation.State.RELEASED and fp.camera_holds.filter(
        state=CameraHold.State.RELEASE_FAILED,
    ).exists()
    if fp.state not in RELEASABLE_PRESERVATION_STATES and not retry:
        raise ValueError(
            "Only a held or partly held preservation can be released; this "
            f"one is {FootagePreservation.State(fp.state).label.lower()}."
        )
    prr = fp.request
    if prr.status not in CLOSED_STATUSES:
        raise ValueError(
            f"{prr.number} is still open ({prr.get_status_display()}). A "
            "preservation hold is released only after the request closes."
        )
    target = prr.merged_into
    if target is not None and target.status not in CLOSED_STATUSES:
        raise ValueError(
            f"{prr.number} was merged into {target.number}, which is still "
            "open. This hold preserves the footage that case answers for."
        )

    live = fp.camera_holds.select_for_update().filter(excluded_at__isnull=True)
    if retry:
        # Only the bookmarks Aware would not delete last time.
        holds = list(live.filter(state=CameraHold.State.RELEASE_FAILED))
    else:
        # A FAILED hold with an id crashed after Aware made the bookmark.
        holds = list(live.filter(
            Q(state__in=CameraHold.BOOKMARKED_STATES)
            | (Q(state=CameraHold.State.FAILED) & ~Q(clip_id=""))
        ))
    bookmarks = [
        (hold.pk, hold.camera_name or hold.camera_id, hold.clip_id)
        for hold in holds
        if hold.clip_id
    ]
    audit(
        "footage.preservation_release_retried" if retry
        else "footage.preservation_released",
        target=prr.number,
        actor=actor,
        reason=reason,
        attest_no_litigation_hold=True,
        bookmarks=len(bookmarks),
    )
    if not retry:
        fp.state = FootagePreservation.State.RELEASED
        fp.released_at = timezone.now()
        fp.released_by = actor
        fp.release_reason = reason
        fp.save(update_fields=["state", "released_at", "released_by", "release_reason"])
    CameraHold.objects.filter(pk__in=[hold.pk for hold in holds]).update(
        state=CameraHold.State.RELEASED,
    )
    StatusEvent.objects.create(
        request=prr,
        actor=actor,
        note=(
            (
                f"Footage preservation release retried: {len(bookmarks)} Aware "
                "bookmark(s) that could not be deleted are queued for deletion "
                "again."
                if retry else
                f"Footage preservation released: {len(bookmarks)} Aware "
                "bookmark(s) queued for deletion after the request closed."
            )
            + " The Records Officer attested that no litigation hold, pending "
            "lawsuit, or reasonably anticipated litigation covers this "
            f"footage. Reason: {reason}"
        ),
    )

    if bookmarks:
        def delete_bookmarks_after_release_commits():
            from .tasks import _safe_alta_error

            failed = []
            for hold_pk, camera, bookmark_id in bookmarks:
                try:
                    alta.delete_bookmark(bookmark_id)
                # Any failure, not only AltaError: the rest still get deleted.
                except Exception as exc:  # noqa: BLE001
                    failed.append(camera)
                    CameraHold.objects.filter(pk=hold_pk).update(
                        state=CameraHold.State.RELEASE_FAILED,
                    )
                    audit(
                        "footage.bookmark_release_failed",
                        target=prr.number,
                        actor=actor,
                        camera=camera,
                        bookmark=bookmark_id,
                        error=_safe_alta_error(exc, action="bookmark delete"),
                    )
            if failed:
                StatusEvent.objects.create(
                    request=prr,
                    actor=actor,
                    note=(
                        f"Footage preservation release: {len(failed)} Aware "
                        "bookmark(s) could not be deleted and may still exist: "
                        f"{'; '.join(failed)[:1000]}. Post the release again "
                        "to retry them."
                    ),
                )

        transaction.on_commit(delete_bookmarks_after_release_commits)
    return fp


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
