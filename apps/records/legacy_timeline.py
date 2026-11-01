# ABOUTME: Imports the correspondence and note history from a retired source
# ABOUTME: system onto migrated cases, as append-only case-timeline evidence.
"""Bring a retired system's case history across, not just its request rows.

The CSV importer carried request-level fields: who asked, when, for what. It
carried none of the conversation - and on a public records request the
conversation *is* the record of what was promised, what was already released,
what was invoiced, and what remains owed. An officer opening a migrated case
saw the original request and nothing else, which is not enough to answer it.

This module imports that history as ``StatusEvent`` rows: append-only, dated to
when the thing actually happened, and carrying the full body rather than a
summary. Requester-visible entries are marked as such, so the distinction
between what was said to the requester and what was noted internally survives
the move.

It deliberately does **not** change any request's status. Whether a migrated
acknowledgment satisfies RCW 42.56.520 for a given case is a records officer's
determination made through ``services.transition()``, not something an importer
should infer from parsed text.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit

from .models import LegacyRequestImport, StatusEvent

#: Marks an imported entry so a re-run can recognise its own work.
MARKER = "[legacy timeline]"

#: The trailing attribution line the source system appends to every entry,
#: e.g. "August 14, 2026, 5:02pm by Public Records Officer (Staff)". The
#: author half is optional: automated sends carry a date and nothing else.
_STAMP = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4}),\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?P<mer>am|pm)"
    r"(?:\s+by\s+(?P<who>.+?))?\s*$",
    re.IGNORECASE,
)

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

#: Entry types worth carrying. Document-add noise is excluded at harvest time;
#: this is a second gate so an unexpected type cannot slip in silently.
KEEP = (
    "Message to requester",
    "Message from requester",
    "Internal message",
    "Note",
    "Document(s) released to requester",
    "Invoice",
    "Due date changed",
    "Request closed",
    "Request reopened",
)


def _wanted(title: str) -> bool:
    return any(title.startswith(k) for k in KEEP)


def parse_stamp(body: str, zone: str = "America/Los_Angeles"):
    """Pull the occurrence time and author out of an entry's last line.

    Returns ``(datetime | None, author)``. A missing or unparseable stamp is
    not fatal - the entry still carries its text - but it loses its place in
    the chronology, so callers surface how many that happened to.
    """
    tail = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if not tail:
        return None, ""
    m = _STAMP.search(tail[-1])
    if not m:
        return None, ""
    hour = int(m.group("hour")) % 12
    if m.group("mer").lower() == "pm":
        hour += 12
    try:
        naive = datetime(
            int(m.group("year")), _MONTHS[m.group("month").lower()],
            int(m.group("day")), hour, int(m.group("minute")),
        )
    except ValueError:
        return None, ""
    return naive.replace(tzinfo=ZoneInfo(zone)), (m.group("who") or "").strip()


def _note(title: str, author: str, body: str, source_number: str) -> str:
    who = f" by {author}" if author else ""
    return (
        f"{MARKER} {title}{who} (source request {source_number}).\n\n{body}"
    ).strip()


def plan_entries(payload: dict, *, zone: str = "America/Los_Angeles"):
    """Resolve the harvested file into what would be written, and what blocks."""
    imports = {
        item.source_number: item
        for item in LegacyRequestImport.objects.select_related("request")
        if item.source_number
    }
    planned, blockers, undated = [], [], 0
    for source_number in sorted(payload):
        entry = payload[source_number]
        cards = entry.get("cards") if isinstance(entry, dict) else entry
        if cards is None:
            blockers.append(f"{source_number}: no timeline entries in the file.")
            continue
        imported = imports.get(source_number)
        if imported is None:
            blockers.append(f"{source_number}: not an imported request.")
            continue
        for card in cards:
            title = (card.get("t") or "").strip()
            if not _wanted(title):
                continue
            raw = (card.get("x") or "").strip()
            lines = raw.splitlines()
            # The harvested text repeats the visibility and title as its first
            # two lines; drop them so the note is not stuttering.
            while lines and lines[0].strip() in {card.get("v", "").strip(), title}:
                lines.pop(0)
            body = "\n".join(lines).strip()
            if not body:
                continue
            occurred, author = parse_stamp(raw, zone)
            if occurred is None:
                undated += 1
                occurred = imported.request.received_at
            planned.append({
                "imported": imported,
                "source_number": source_number,
                "title": title,
                "occurred_at": occurred,
                "visible": card.get("v", "").strip() == "Requester + Staff",
                "note": _note(title, author, body, source_number),
            })
    return planned, blockers, undated


def load_payload(path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"{path} does not exist.")
    try:
        payload = json.loads(path.read_text())
    except ValueError as exc:
        raise ValidationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("The timeline file must be a non-empty object.")
    return payload


def already_imported(request) -> int:
    return StatusEvent.objects.filter(request=request, note__contains=MARKER).count()


def commit_entries(planned, *, operator: str):
    """Write the planned entries, skipping any already present.

    Idempotent on the exact note text: a re-run of the same harvest adds
    nothing. StatusEvent is append-only, so this can only ever add.
    """
    written = 0
    per_request = {}
    for item in planned:
        request = item["imported"].request
        if StatusEvent.objects.filter(
            request=request, note=item["note"]
        ).exists():
            continue
        with transaction.atomic():
            StatusEvent.objects.create(
                request=request,
                occurred_at=item["occurred_at"],
                note=item["note"],
                visible_to_requester=item["visible"],
            )
        written += 1
        per_request[item["source_number"]] = per_request.get(item["source_number"], 0) + 1

    for source_number, count in sorted(per_request.items()):
        audit(
            "request.legacy_timeline_imported",
            target=source_number,
            operator=operator,
            entries=count,
        )
    return written, per_request


#: Recognises the earlier, and wrong, "no initial response" note so it can be
#: superseded once the imported timeline proves otherwise.
GAP_MARKER = "recorded NO initial response for source ID"
CORRECTION_MARKER = "CORRECTION to the earlier no-response note"

#: Recognises the note the importer writes when source files were knowingly
#: left behind, so the case is not read as complete on the migrated record.
RETAINED_ATTACHMENTS_MARKER = "were NOT migrated and remain in"

#: Every note that changes what an officer must do about a migrated case.
#: These are written backdated to the request's receipt date so they sit with
#: the other import notes, which means a plain newest-first list buries them
#: the moment any routine activity happens. Anything rendering a bounded
#: excerpt of the case timeline must keep these visible on their own merit.
COMPLIANCE_NOTE_MARKERS = (
    GAP_MARKER,
    CORRECTION_MARKER,
    RETAINED_ATTACHMENTS_MARKER,
)


def compliance_flagged_note(note: str | None) -> bool:
    """Whether this timeline note must survive a bounded, recency-cut list."""
    if not note:
        return False
    return any(marker in note for marker in COMPLIANCE_NOTE_MARKERS)


def correction_note(*, source_number, first_message_at, source_system):
    when = first_message_at.strftime("%B %-d, %Y at %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    return (
        f"{CORRECTION_MARKER} on source request {source_number}.\n\n"
        f"An earlier note on this case stated that {source_system} recorded no "
        "initial response. That note was based on the source system's "
        "'initial response' workflow field, which staff had not set. It was "
        "wrong.\n\n"
        f"The imported case timeline shows a message to the requester on "
        f"{when}. Read that message, and the rest of the imported "
        "correspondence below, before drawing any conclusion about this "
        "case's RCW 42.56.520 position.\n\n"
        "The earlier note cannot be deleted - this timeline is append-only - "
        "so it remains above, superseded by this one."
    )


def plan_corrections(payload, *, source_system):
    """Cases carrying the wrong no-response note that the timeline disproves."""
    imports = {
        item.source_number: item
        for item in LegacyRequestImport.objects.select_related("request")
        if item.source_number
    }
    out = []
    for source_number in sorted(payload):
        imported = imports.get(source_number)
        if imported is None:
            continue
        if not StatusEvent.objects.filter(
            request=imported.request, note__contains=GAP_MARKER
        ).exists():
            continue
        if StatusEvent.objects.filter(
            request=imported.request, note__contains=CORRECTION_MARKER
        ).exists():
            continue
        entry = payload[source_number]
        cards = entry.get("cards") if isinstance(entry, dict) else entry
        stamps = []
        for card in cards or []:
            if (card.get("t") or "").strip() != "Message to requester":
                continue
            when, _ = parse_stamp(card.get("x") or "")
            if when is not None:
                stamps.append(when)
        if not stamps:
            continue
        out.append({
            "imported": imported,
            "source_number": source_number,
            "first_message_at": min(stamps),
            "note": correction_note(
                source_number=source_number,
                first_message_at=min(stamps),
                source_system=source_system,
            ),
        })
    return out


def commit_corrections(planned, *, operator):
    written = 0
    for item in planned:
        request = item["imported"].request
        with transaction.atomic():
            StatusEvent.objects.create(
                request=request,
                occurred_at=timezone.now(),
                note=item["note"],
                visible_to_requester=False,
            )
            audit(
                "request.legacy_response_note_corrected",
                target=request.number,
                operator=operator,
                source_number=item["source_number"],
                first_message_at=item["first_message_at"].isoformat(),
            )
        written += 1
    return written
