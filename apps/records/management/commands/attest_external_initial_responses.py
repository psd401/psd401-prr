# ABOUTME: Records, on a named Records Officer's statement, first responses that went
# ABOUTME: out through NextRequest or another channel this system never delivered.
"""Backfill first-response attestations for cases answered somewhere else.

Every migrated case whose NextRequest export carried no response date shows
"five-day response not sent", sits in the statutory backlog, and would draw an
automatic second acknowledgment the day the safety net is switched on. The
officer knows those requesters were answered; nothing let them say so.

Three sources, one statement each, all through
``services.record_external_initial_response()`` so every case gets the same
timeline note and audit entry it would get from the Overview button:

* ``--csv FILE``: one row per case, prepared by the officer. Columns:
  ``number`` (this system's number or the NextRequest number), optional
  ``responded_on`` (YYYY-MM-DD), optional ``sent_via``, optional ``evidence``.
* ``--archive FILE``: the NextRequest closure archive JSON. For each closed
  migrated case still lacking a first response, the earliest requester-visible
  "External Message" is the response: a staff-authored one when there is one,
  otherwise the tenant's own automated message, named as such.
* ``--remaining-migrated``: every migrated case still lacking a first response
  is recorded as answered from NextRequest before the cutover, date not on
  record. This is the officer's blanket statement and the evidence text says
  so; use it last, after the two sources above.

Dry run by default. ``--commit`` writes. ``--attested-by`` names the Records
Officer whose statement this is; the service refuses anyone else.
"""
import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.records import services
from apps.records.legacy_timeline import parse_stamp
from apps.records.models import (
    InitialResponseAttestation,
    LegacyRequestImport,
    PublicRecordsRequest,
)

NEXTREQUEST_CUTOVER = "2026-08-31"

REMAINING_EVIDENCE = (
    "Records Officer statement: the first response was sent from NextRequest "
    f"before the {NEXTREQUEST_CUTOVER} cutover. The export carried no response "
    "date and the tenant is no longer reachable."
)


@dataclass(frozen=True)
class Planned:
    request: PublicRecordsRequest
    sent_via: str
    responded_on: dt.date | None
    evidence: str
    source: str


def _needs_attestation(request):
    if request.initial_response_delivered:
        return False
    return not InitialResponseAttestation.objects.filter(request=request).exists()


def _find_request(number):
    number = (number or "").strip()
    if not number:
        return None
    request = PublicRecordsRequest.objects.filter(number__iexact=number).first()
    if request is not None:
        return request
    imported = (
        LegacyRequestImport.objects.select_related("request")
        .filter(source_number__iexact=number)
        .first()
    )
    return imported.request if imported else None


def _parse_date(value, *, row_label):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{row_label}: responded_on must be YYYY-MM-DD.") from exc


def _plan_csv(path, *, default_via, default_evidence):
    planned, skipped = [], []
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "number" not in reader.fieldnames:
            raise CommandError("The CSV needs a 'number' column.")
        for index, row in enumerate(reader, start=2):
            label = f"row {index}"
            request = _find_request(row.get("number"))
            if request is None:
                skipped.append((label, row.get("number", ""), "no such case"))
                continue
            if not _needs_attestation(request):
                skipped.append((label, request.number, "already has a first response"))
                continue
            planned.append(
                Planned(
                    request=request,
                    sent_via=(row.get("sent_via") or default_via).strip(),
                    responded_on=_parse_date(row.get("responded_on"), row_label=label),
                    evidence=(row.get("evidence") or default_evidence).strip(),
                    source=f"csv {label}",
                )
            )
    return planned, skipped


def _first_external_message(events):
    """The earliest requester-visible message the district sent, if any."""
    dated = []
    for event in events:
        if event.get("timeline_name") != "External Message":
            continue
        stamp, author = parse_stamp(event.get("timeline_byline", ""))
        if stamp is None:
            continue
        staff = "(Staff)" in author
        automated = author == ""
        if not (staff or automated):
            continue  # the requester's own message
        dated.append((stamp, staff, event))
    if not dated:
        return None
    staff_only = [item for item in dated if item[1]]
    stamp, staff, event = min(staff_only or dated, key=lambda item: item[0])
    kind = "staff" if staff else "automated NextRequest"
    return stamp.date(), (
        f"Earliest {kind} message to the requester in the NextRequest closure "
        f"archive captured {NEXTREQUEST_CUTOVER} (timeline id "
        f"{event.get('timeline_id', '?')}, {event.get('timeline_byline', '')})."
    )


def _plan_archive(path):
    data = json.loads(Path(path).read_text())
    closed = data.get("closed_requests")
    if not isinstance(closed, dict):
        raise CommandError("The archive has no 'closed_requests' object.")
    planned, skipped = [], []
    imports = LegacyRequestImport.objects.select_related("request").order_by("source_number")
    for imported in imports:
        entry = closed.get(imported.source_number)
        if entry is None:
            continue
        request = imported.request
        if not _needs_attestation(request):
            skipped.append(("archive", request.number, "already has a first response"))
            continue
        events = (entry.get("timeline") or {}).get("timeline") or []
        found = _first_external_message(events)
        if found is None:
            skipped.append(("archive", request.number, "no district message in the archive"))
            continue
        responded_on, evidence = found
        planned.append(
            Planned(
                request=request,
                sent_via=InitialResponseAttestation.SentVia.NEXTREQUEST,
                responded_on=responded_on,
                evidence=evidence,
                source=f"archive {imported.source_number}",
            )
        )
    return planned, skipped


def _plan_remaining(*, evidence):
    planned = []
    imports = LegacyRequestImport.objects.select_related("request").order_by("source_number")
    for imported in imports:
        request = imported.request
        if not _needs_attestation(request):
            continue
        planned.append(
            Planned(
                request=request,
                sent_via=InitialResponseAttestation.SentVia.NEXTREQUEST,
                responded_on=None,
                evidence=evidence,
                source=f"remaining {imported.source_number}",
            )
        )
    return planned, []


class Command(BaseCommand):
    help = "Record first responses sent outside this system, on a Records Officer's statement."

    def add_arguments(self, parser):
        parser.add_argument("--attested-by", required=True, help="Username of the Records Officer making the statement.")
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--csv", help="CSV of number, responded_on, sent_via, evidence.")
        source.add_argument("--archive", help="NextRequest closure archive JSON.")
        source.add_argument("--remaining-migrated", action="store_true", help="Every migrated case still lacking a first response, date not on record.")
        parser.add_argument("--sent-via", default=InitialResponseAttestation.SentVia.NEXTREQUEST, choices=list(InitialResponseAttestation.SentVia.values), help="Default channel for CSV rows without one.")
        parser.add_argument("--evidence", default=REMAINING_EVIDENCE, help="Default statement for rows and cases without their own.")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        officer = get_user_model().objects.filter(username=options["attested_by"]).first()
        if officer is None:
            raise CommandError(f"No user named {options['attested_by']!r}.")
        evidence = options["evidence"].strip()
        if len(evidence) < services.MIN_ATTESTATION_EVIDENCE:
            raise CommandError("The default evidence statement is too short.")

        if options["csv"]:
            planned, skipped = _plan_csv(options["csv"], default_via=options["sent_via"], default_evidence=evidence)
        elif options["archive"]:
            planned, skipped = _plan_archive(options["archive"])
        else:
            planned, skipped = _plan_remaining(evidence=evidence)

        self.stdout.write(f"Statement by: {officer.username}")
        self.stdout.write(f"  to record : {len(planned)}")
        self.stdout.write(f"  skipped   : {len(skipped)}")
        for label, number, why in skipped:
            self.stdout.write(f"    skip {number:16} {why} ({label})")
        for item in planned:
            when = item.responded_on.isoformat() if item.responded_on else "date not on record"
            self.stdout.write(f"    {item.request.number:16} {item.sent_via:15} {when:18} {item.source}")

        if not options["commit"]:
            self.stdout.write("Dry run; nothing written. Add --commit to record these statements.")
            return

        recorded, failed = 0, 0
        for item in planned:
            try:
                with transaction.atomic():
                    services.record_external_initial_response(
                        item.request,
                        actor=officer,
                        sent_via=item.sent_via,
                        evidence=item.evidence,
                        responded_on=item.responded_on,
                    )
            except (ValidationError, PermissionDenied) as exc:
                failed += 1
                detail = "; ".join(getattr(exc, "messages", [str(exc)]))
                self.stderr.write(f"    FAILED {item.request.number}: {detail}")
                if isinstance(exc, PermissionDenied):
                    raise CommandError("The named user is not a current Records Officer; nothing further recorded.") from exc
                continue
            recorded += 1
        self.stdout.write(f"Recorded {recorded}; failed {failed}.")
