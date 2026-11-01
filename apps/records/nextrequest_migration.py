"""Deterministic, human-gated migration from a mapped NextRequest CSV export.

The planner is pure and fail-closed. Committing an exact reviewed digest creates
only local legal/evidence rows; live triage and preservation are a separate,
explicit activation step so importing historic data cannot accidentally email a
requester, notify a custodian, or start an external hold.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import audit

from .deadlines import statutory_response_due
from .models import (
    Channel,
    LegacyImportBatch,
    LegacyRequestActivation,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)
from .services import CLOSED_STATUSES, _require_current_records_officer, transition
from .triage import route_to_systems, triage_footage

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
MAX_FIELD_CHARACTERS = 250_000

REQUIRED_COLUMN_KEYS = frozenset(
    {
        "source_id",
        "source_number",
        "description",
        "received_at",
        "status",
        "status_changed_at",
        "closed_at",
        "closure_reason",
        "initial_response_at",
        "estimated_completion",
        "attachment_count",
        "channel",
        "name",
        "email",
        "phone",
        "mailing_address",
        "company",
    }
)

SAFE_TARGET_STATUSES = frozenset(
    {
        RequestStatus.RECEIVED,
        RequestStatus.GATHERING,
        RequestStatus.REVIEW,
        RequestStatus.INSTALLMENT,
        RequestStatus.FULFILLED,
        RequestStatus.CLOSED_WITHDRAWN,
        RequestStatus.CLOSED_ABANDONED,
    }
)

DELIVERY_BACKED_STATUSES = frozenset(
    {
        RequestStatus.ACKNOWLEDGED,
        RequestStatus.CLARIFICATION,
        RequestStatus.DENIED,
    }
)


@dataclass(frozen=True)
class PlanIssue:
    row_number: int | None
    field: str
    message: str


@dataclass(frozen=True)
class PlannedLegacyRequest:
    row_number: int
    source_id: str
    source_number: str
    source_status: str
    target_status: str
    description: str
    received_at: datetime
    status_changed_at: datetime | None
    closed_at: datetime | None
    closure_reason: str
    initial_response_at: datetime | None
    estimated_completion: date | None
    attachment_count: int
    channel: str
    name: str
    email: str
    phone: str
    mailing_address: str
    company: str
    source_row_sha256: str


@dataclass(frozen=True)
class NextRequestPlan:
    source_sha256: str
    mapping_sha256: str
    plan_sha256: str
    mapping_snapshot: Mapping[str, Any]
    rows: tuple[PlannedLegacyRequest, ...]
    issues: tuple[PlanIssue, ...]

    @property
    def ready(self) -> bool:
        return bool(self.rows) and not self.issues


@dataclass(frozen=True)
class CutoverReport:
    ready: bool
    plan_sha256: str
    source_rows: int
    imported_rows: int
    activated_open_rows: int
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.isoformat(),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


#: Minimum length for the written reason that must accompany an
#: attachments-retained acknowledgement. This is a statement a Records Officer
#: has to defend later, not a checkbox.
MIN_ATTACHMENT_ACKNOWLEDGEMENT_REASON = 40


def legacy_response_gap_note(*, source_id, source_status, retaining_system):
    """The note recorded when the source system shows no first response.

    Deliberately says what the *source* recorded rather than asserting that
    nobody replied. A response sent by direct email and never logged would not
    appear in an export, and the district should look for one before treating
    the case as untouched - but it also cannot rely on a response it cannot
    produce.
    """
    return (
        f"{retaining_system} recorded NO initial response for source ID "
        f"{source_id}; its workflow status at export was "
        f"'{source_status}'. The statutory RCW 42.56.520 five-business-day "
        "response is therefore outstanding on this record. If a response was "
        "sent outside that system, locate it and record it here before "
        "treating this case as answered."
    )


def _validate_attachment_acknowledgement(snapshot: Mapping[str, Any]) -> None:
    """Validate the optional, explicit attachments-retained-in-source block.

    Absent, the importer blocks every row that reports attachments - that is
    the default and it stays the default. Present, it records that the source
    files are knowingly staying where they are, who said so, and why. It does
    NOT migrate a single file, and it never converts a requester upload into a
    responsive record.
    """
    acknowledgement = snapshot.get("attachments_retained_in_source")
    if acknowledgement is None:
        return
    if not isinstance(acknowledgement, dict):
        raise ValidationError(
            "attachments_retained_in_source must be an object when provided."
        )
    if acknowledgement.get("acknowledged") is not True:
        raise ValidationError(
            "attachments_retained_in_source requires acknowledged: true."
        )
    source_system = acknowledgement.get("source_system")
    if not isinstance(source_system, str) or not source_system.strip():
        raise ValidationError(
            "attachments_retained_in_source requires the retaining source_system."
        )
    reason = acknowledgement.get("reason")
    if (
        not isinstance(reason, str)
        or len(reason.strip()) < MIN_ATTACHMENT_ACKNOWLEDGEMENT_REASON
    ):
        raise ValidationError(
            "attachments_retained_in_source requires a substantive reason of at "
            f"least {MIN_ATTACHMENT_ACKNOWLEDGEMENT_REASON} characters."
        )


def _attachments_retained(mapping: Mapping[str, Any]) -> bool:
    acknowledgement = mapping.get("attachments_retained_in_source")
    return isinstance(acknowledgement, dict) and acknowledgement.get("acknowledged") is True


def _validate_mapping(mapping: Mapping[str, Any]) -> tuple[dict[str, Any], ZoneInfo]:
    if not isinstance(mapping, Mapping):
        raise ValidationError("The migration mapping must be a JSON object.")
    snapshot = json.loads(_canonical_json(mapping).decode("utf-8"))
    if snapshot.get("version") != 1:
        raise ValidationError("The migration mapping version must be 1.")
    columns = snapshot.get("columns")
    if not isinstance(columns, dict):
        raise ValidationError("The migration mapping requires a columns object.")
    missing_keys = sorted(REQUIRED_COLUMN_KEYS.difference(columns))
    if missing_keys:
        raise ValidationError(
            f"The migration mapping is missing column keys: {', '.join(missing_keys)}."
        )
    if any(not isinstance(columns[key], str) or not columns[key].strip() for key in REQUIRED_COLUMN_KEYS):
        raise ValidationError("Every mapped column name must be a non-empty string.")
    if len(set(columns.values())) != len(columns):
        raise ValidationError("Each migration field must map to a distinct source column.")
    status_map = snapshot.get("status_map")
    if not isinstance(status_map, dict) or not status_map:
        raise ValidationError("The migration mapping requires a non-empty status_map.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in status_map.items()):
        raise ValidationError("Every status_map entry must map strings to strings.")
    channel_map = snapshot.get("channel_map", {})
    if not isinstance(channel_map, dict):
        raise ValidationError("channel_map must be an object when provided.")
    _validate_attachment_acknowledgement(snapshot)
    try:
        source_zone = ZoneInfo(str(snapshot.get("source_timezone", "")))
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("source_timezone must be a valid IANA timezone.") from exc
    return snapshot, source_zone


def _parse_datetime(value: str, *, field: str, source_zone: ZoneInfo) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required.")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 date and time.") from exc
    if parsed.tzinfo is not None:
        return parsed

    first = parsed.replace(tzinfo=source_zone, fold=0)
    second = parsed.replace(tzinfo=source_zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            f"{field} is ambiguous in {source_zone.key}; include an explicit UTC offset."
        )
    round_trip = first.astimezone(ZoneInfo("UTC")).astimezone(source_zone).replace(tzinfo=None)
    if round_trip != parsed:
        raise ValueError(
            f"{field} does not exist in {source_zone.key}; include a valid offset timestamp."
        )
    return first


def _optional_datetime(value: str, *, field: str, source_zone: ZoneInfo) -> datetime | None:
    return _parse_datetime(value, field=field, source_zone=source_zone) if value.strip() else None


def _optional_date(value: str, *, field: str) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 date (YYYY-MM-DD).") from exc


def _field(row: Mapping[str, str | None], columns: Mapping[str, str], key: str) -> str:
    return (row.get(columns[key]) or "").strip()


def _normalize_row(
    row: Mapping[str, str | None],
    *,
    row_number: int,
    mapping: Mapping[str, Any],
    source_zone: ZoneInfo,
) -> tuple[PlannedLegacyRequest | None, list[PlanIssue]]:
    columns = mapping["columns"]
    issues: list[PlanIssue] = []

    def issue(field: str, message: str) -> None:
        issues.append(PlanIssue(row_number=row_number, field=field, message=message))

    source_id = _field(row, columns, "source_id")
    if not source_id:
        issue("source_id", "Source ID is required.")
    elif len(source_id) > 255:
        issue("source_id", "Source ID must contain at most 255 characters.")
    description = _field(row, columns, "description")
    if not description:
        issue("description", "The original request text is required.")
    elif len(description) > MAX_FIELD_CHARACTERS:
        issue("description", "The original request text exceeds the migration field limit.")

    source_status = _field(row, columns, "status")
    target_status = mapping["status_map"].get(source_status, "")
    if not target_status:
        issue("status", f"Source status {source_status!r} is not mapped.")
    elif target_status in DELIVERY_BACKED_STATUSES:
        issue(
            "status",
            f"Target {target_status!r} is a delivery-backed status and cannot be reconstructed from a CSV row.",
        )
    elif target_status not in SAFE_TARGET_STATUSES:
        issue("status", f"Target status {target_status!r} is not allowed by the safe importer.")

    channel_source = _field(row, columns, "channel")
    channel = mapping.get("channel_map", {}).get(channel_source, channel_source)
    if channel not in Channel.values:
        issue("channel", f"Source channel {channel_source!r} is not mapped to a valid channel.")

    parsed: dict[str, Any] = {}
    for key, optional in (
        ("received_at", False),
        ("status_changed_at", True),
        ("closed_at", True),
        ("initial_response_at", True),
    ):
        try:
            value = _field(row, columns, key)
            parsed[key] = (
                _optional_datetime(value, field=columns[key], source_zone=source_zone)
                if optional
                else _parse_datetime(value, field=columns[key], source_zone=source_zone)
            )
        except ValueError as exc:
            parsed[key] = None
            issue(key, str(exc))
    try:
        parsed["estimated_completion"] = _optional_date(
            _field(row, columns, "estimated_completion"),
            field=columns["estimated_completion"],
        )
    except ValueError as exc:
        parsed["estimated_completion"] = None
        issue("estimated_completion", str(exc))

    attachment_raw = _field(row, columns, "attachment_count")
    try:
        attachment_count = int(attachment_raw or "0")
        if attachment_count < 0:
            raise ValueError
    except ValueError:
        attachment_count = 0
        issue("attachment_count", "Attachment count must be a non-negative integer.")
    if attachment_count and not _attachments_retained(mapping):
        issue(
            "attachment_count",
            "Source attachments are present; migrate and malware-scan a classified attachment manifest before this row.",
        )

    received_at = parsed.get("received_at")
    status_changed_at = parsed.get("status_changed_at")
    closed_at = parsed.get("closed_at")
    initial_response_at = parsed.get("initial_response_at")
    closure_reason = _field(row, columns, "closure_reason")
    now = timezone.now()
    if received_at and received_at > now:
        issue("received_at", "Received timestamp cannot be in the future.")
    if target_status and target_status != RequestStatus.RECEIVED and status_changed_at is None:
        issue("status_changed_at", "A non-received target status requires its historic transition timestamp.")
    if received_at and status_changed_at and status_changed_at < received_at:
        issue("status_changed_at", "Status transition cannot predate receipt.")
    if status_changed_at and status_changed_at > now:
        issue("status_changed_at", "Status transition cannot be in the future.")
    if initial_response_at and received_at and initial_response_at < received_at:
        issue("initial_response_at", "Initial response cannot predate receipt.")
    if initial_response_at and initial_response_at > now:
        issue("initial_response_at", "Initial response cannot be in the future.")
    if target_status in SAFE_TARGET_STATUSES.difference({RequestStatus.RECEIVED, *CLOSED_STATUSES}) and initial_response_at is None:
        issue(
            "initial_response_at",
            "An active workflow status beyond received requires historic initial-response evidence.",
        )
    if target_status in CLOSED_STATUSES:
        if closed_at is None:
            issue("closed_at", "A terminal status requires its historic closure timestamp.")
        if len(closure_reason) < 10:
            issue("closure_reason", "A terminal status requires a substantive historic closure reason.")
        if closed_at and status_changed_at and closed_at != status_changed_at:
            issue("closed_at", "Closure and terminal status timestamps must match exactly.")
    elif closed_at is not None:
        issue("closed_at", "An open status cannot have a closure timestamp.")

    email = _field(row, columns, "email")
    phone = _field(row, columns, "phone")
    mailing_address = _field(row, columns, "mailing_address")
    if not any((email, phone, mailing_address)):
        issue("requester", "At least one historic requester response method is required.")

    if issues:
        return None, issues

    normalized = {
        "source_id": source_id,
        "source_number": _field(row, columns, "source_number"),
        "source_status": source_status,
        "target_status": target_status,
        "description": description,
        "received_at": received_at,
        "status_changed_at": status_changed_at,
        "closed_at": closed_at,
        "closure_reason": closure_reason,
        "initial_response_at": initial_response_at,
        "estimated_completion": parsed["estimated_completion"],
        "attachment_count": attachment_count,
        "channel": channel,
        "name": _field(row, columns, "name"),
        "email": email,
        "phone": phone,
        "mailing_address": mailing_address,
        "company": _field(row, columns, "company"),
    }
    return PlannedLegacyRequest(
        row_number=row_number,
        source_row_sha256=_sha256(_canonical_json(normalized)),
        **normalized,
    ), issues


def plan_nextrequest_import(source: bytes, mapping: Mapping[str, Any]) -> NextRequestPlan:
    """Parse and normalize an export without reading or writing the database."""
    if not isinstance(source, bytes):
        raise ValidationError("The NextRequest source must be provided as bytes.")
    if not source or len(source) > MAX_SOURCE_BYTES:
        raise ValidationError(f"The source must contain 1 to {MAX_SOURCE_BYTES} bytes.")
    if b"\x00" in source:
        raise ValidationError("The source contains a NUL byte and is not a valid CSV export.")
    try:
        text = source.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError("The source must be strict UTF-8 CSV.") from exc

    mapping_snapshot, source_zone = _validate_mapping(mapping)
    mapping_sha256 = _sha256(_canonical_json(mapping_snapshot))
    source_sha256 = _sha256(source)
    issues: list[PlanIssue] = []
    rows: list[PlannedLegacyRequest] = []
    seen_ids: dict[str, int] = {}

    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_FIELD_CHARACTERS)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames or []
        if not headers:
            raise ValidationError("The source CSV requires a header row.")
        if len(headers) != len(set(headers)):
            raise ValidationError("The source CSV contains duplicate header names.")
        missing_headers = sorted(set(mapping_snapshot["columns"].values()).difference(headers))
        if missing_headers:
            raise ValidationError(
                f"The source CSV is missing mapped headers: {', '.join(missing_headers)}."
            )
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > MAX_ROWS:
                raise ValidationError(f"The source exceeds the {MAX_ROWS}-row safety limit.")
            if None in row:
                issues.append(
                    PlanIssue(row_number, "row", "The row contains more fields than the header.")
                )
                continue
            planned, row_issues = _normalize_row(
                row,
                row_number=row_number,
                mapping=mapping_snapshot,
                source_zone=source_zone,
            )
            issues.extend(row_issues)
            source_id = _field(row, mapping_snapshot["columns"], "source_id")
            if source_id:
                previous = seen_ids.get(source_id)
                if previous is not None:
                    issues.append(
                        PlanIssue(
                            row_number,
                            "source_id",
                            f"Row has duplicate source ID {source_id!r}; it was first used on row {previous}.",
                        )
                    )
                else:
                    seen_ids[source_id] = row_number
            if planned is not None:
                rows.append(planned)
    except csv.Error as exc:
        raise ValidationError(f"The source is not valid CSV: {exc}.") from exc
    finally:
        csv.field_size_limit(previous_limit)

    if not rows and not issues:
        issues.append(PlanIssue(None, "source", "The source contains no data rows."))
    digest_payload = {
        "version": 1,
        "source_sha256": source_sha256,
        "mapping_sha256": mapping_sha256,
        "rows": [asdict(row) for row in rows],
        "issues": [asdict(issue) for issue in issues],
    }
    return NextRequestPlan(
        source_sha256=source_sha256,
        mapping_sha256=mapping_sha256,
        plan_sha256=_sha256(_canonical_json(digest_payload)),
        mapping_snapshot=MappingProxyType(mapping_snapshot),
        rows=tuple(rows),
        issues=tuple(issues),
    )


@transaction.atomic
def commit_nextrequest_plan(
    plan: NextRequestPlan,
    *,
    actor,
    expected_plan_sha256: str,
) -> LegacyImportBatch:
    """Commit only the exact, issue-free plan a Records Officer reviewed."""
    actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required for legacy migration.",
    )
    if not hmac.compare_digest(plan.plan_sha256, (expected_plan_sha256 or "").strip().lower()):
        raise ValidationError("The approved plan digest does not match this import plan.")
    if not plan.ready:
        raise ValidationError("The migration plan has blocked rows; no rows were imported.")

    existing = LegacyImportBatch.objects.filter(plan_sha256=plan.plan_sha256).first()
    if existing is not None:
        return existing
    source_ids = [row.source_id for row in plan.rows]
    conflicts = list(
        LegacyRequestImport.objects.filter(source_id__in=source_ids).values_list("source_id", flat=True)
    )
    if conflicts:
        raise ValidationError(
            f"Source IDs were already imported by another plan: {', '.join(sorted(conflicts))}."
        )

    acknowledgement = plan.mapping_snapshot.get("attachments_retained_in_source") or {}
    retaining_system = str(acknowledgement.get("source_system") or "the source system")
    retained_attachment_rows = [row for row in plan.rows if row.attachment_count]

    try:
        batch = LegacyImportBatch.objects.create(
            source_sha256=plan.source_sha256,
            mapping_sha256=plan.mapping_sha256,
            plan_sha256=plan.plan_sha256,
            row_count=len(plan.rows),
            mapping_snapshot=dict(plan.mapping_snapshot),
            imported_by=actor,
        )
        for row in plan.rows:
            requester = Requester.objects.create(
                name=row.name,
                email=row.email,
                phone=row.phone,
                mailing_address=row.mailing_address,
                company=row.company,
            )
            request = PublicRecordsRequest.objects.create(
                requester=requester,
                description=row.description,
                channel=row.channel,
                received_at=row.received_at,
                statutory_response_due=statutory_response_due(row.received_at),
            )
            imported = LegacyRequestImport.objects.create(
                batch=batch,
                request=request,
                source_id=row.source_id,
                source_number=row.source_number,
                source_status=row.source_status,
                source_row_sha256=row.source_row_sha256,
                status_changed_at=row.status_changed_at,
                legacy_initial_response_at=row.initial_response_at,
                legacy_estimated_completion=row.estimated_completion,
                legacy_closed_at=row.closed_at,
                closure_reason=row.closure_reason,
                source_attachment_count=row.attachment_count,
            )
            StatusEvent.objects.create(
                request=request,
                actor=actor,
                occurred_at=row.received_at,
                note=(
                    f"Legacy NextRequest intake imported from source ID {row.source_id}; "
                    f"statutory 5-business-day response due {request.statutory_response_due}."
                ),
            )
            if row.initial_response_at is not None:
                StatusEvent.objects.create(
                    request=request,
                    actor=actor,
                    occurred_at=row.initial_response_at,
                    note=(
                        "Legacy source reports an initial requester response at this time; "
                        f"source row digest {row.source_row_sha256}."
                    ),
                )
            else:
                # The absence of a response note is not a usable signal. An
                # officer triaging a migrated backlog has to be told which
                # cases the old system shows as never answered, because that
                # is a live statutory exposure rather than a migration
                # artefact - and the two look identical from a silent
                # timeline.
                StatusEvent.objects.create(
                    request=request,
                    actor=actor,
                    occurred_at=row.received_at,
                    note=legacy_response_gap_note(
                        source_id=row.source_id,
                        source_status=row.source_status,
                        retaining_system=retaining_system,
                    ),
                )
            if row.attachment_count:
                # The officer who opens this case has to be told, on the case
                # timeline rather than only in an audit row, that responsive
                # files exist somewhere this system cannot reach.
                StatusEvent.objects.create(
                    request=request,
                    actor=actor,
                    occurred_at=row.received_at,
                    note=(
                        f"{row.attachment_count} source file(s) were NOT migrated and "
                        f"remain in {retaining_system} under source ID {row.source_id}. "
                        "Retrieve them there until they are moved through the scanned "
                        "attachment manifest. Do not treat this case as complete on "
                        "the migrated record alone."
                    ),
                )
            audit(
                "request.created",
                target=request.number,
                actor=actor,
                integration_source="nextrequest_migration",
                source_id=row.source_id,
                source_row_sha256=row.source_row_sha256,
            )
            if row.target_status != RequestStatus.RECEIVED:
                transition(
                    request,
                    row.target_status,
                    actor=actor,
                    occurred_at=row.status_changed_at,
                    note=(
                        row.closure_reason
                        if row.target_status in CLOSED_STATUSES
                        else (
                            "Legacy NextRequest import restored workflow status "
                            f"{row.source_status!r} from source ID {row.source_id}."
                        )
                    ),
                )
            audit(
                "legacy.nextrequest_request_imported",
                target=request.number,
                actor=actor,
                batch_id=str(batch.pk),
                import_id=str(imported.pk),
                source_id=row.source_id,
                source_number=row.source_number,
                source_status=row.source_status,
                target_status=row.target_status,
                source_row_sha256=row.source_row_sha256,
                source_attachment_count=row.attachment_count,
                attachments_retained_in_source=bool(row.attachment_count),
            )
    except IntegrityError as exc:
        raise ValidationError(
            "The migration collided with existing evidence; the transaction was rolled back."
        ) from exc

    audit(
        "legacy.nextrequest_batch_imported",
        target=str(batch.pk),
        actor=actor,
        source_sha256=plan.source_sha256,
        mapping_sha256=plan.mapping_sha256,
        plan_sha256=plan.plan_sha256,
        row_count=len(plan.rows),
        rows_with_retained_attachments=len(retained_attachment_rows),
        retained_attachment_files=sum(
            row.attachment_count for row in retained_attachment_rows
        ),
        attachments_retaining_system=(
            retaining_system if retained_attachment_rows else ""
        ),
    )
    return batch


@transaction.atomic
def activate_legacy_request(import_id, *, actor) -> LegacyRequestActivation:
    """Explicitly put one imported open request through live preservation/triage."""
    actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required for migration activation.",
    )
    imported = (
        LegacyRequestImport.objects.select_related("request")
        .select_for_update(of=("self",))
        .get(pk=import_id)
    )
    existing = LegacyRequestActivation.objects.filter(request_import=imported).first()
    if existing is not None:
        return existing
    if imported.request.status in CLOSED_STATUSES:
        raise ValidationError("Closed legacy requests cannot be activated for live triage.")

    preservation = triage_footage(imported.request)
    tasks = route_to_systems(imported.request)
    activation = LegacyRequestActivation.objects.create(
        request_import=imported,
        activated_by=actor,
        footage_preservation_created=preservation is not None,
        collection_tasks_created=len(tasks),
    )
    audit(
        "legacy.nextrequest_request_activated",
        target=imported.request.number,
        actor=actor,
        import_id=str(imported.pk),
        activation_id=str(activation.pk),
        footage_preservation=preservation is not None,
        collection_tasks_created=len(tasks),
    )
    return activation


def assess_nextrequest_cutover(plan: NextRequestPlan) -> CutoverReport:
    """Fail-closed readiness assessment for one exact source/mapping plan."""
    blockers: list[str] = []
    warnings: list[str] = []
    if not plan.ready:
        blockers.extend(
            f"Row {issue.row_number or 'source'} ({issue.field}): {issue.message}"
            for issue in plan.issues
        )
    batch = LegacyImportBatch.objects.filter(plan_sha256=plan.plan_sha256).first()
    imported_rows = 0
    activated_open_rows = 0
    if batch is None:
        blockers.append("This exact source and mapping plan has not been committed.")
    else:
        imports = list(
            batch.request_imports.select_related("request").prefetch_related("activation")
        )
        imported_rows = len(imports)
        if imported_rows != len(plan.rows):
            blockers.append(
                f"Committed batch has {imported_rows} rows but the reviewed plan has {len(plan.rows)}."
            )
        plan_ids = {row.source_id for row in plan.rows}
        imported_ids = {item.source_id for item in imports}
        if plan_ids != imported_ids:
            blockers.append("Committed source IDs do not exactly match the reviewed plan.")
        for imported in imports:
            if imported.request.status not in CLOSED_STATUSES:
                try:
                    imported.activation  # noqa: B018 - the access is the probe
                except LegacyRequestActivation.DoesNotExist:
                    blockers.append(
                        f"Open source request {imported.source_id} is imported but not activated."
                    )
                else:
                    activated_open_rows += 1
            if imported.legacy_estimated_completion is not None:
                warnings.append(
                    f"Source request {imported.source_id} retains estimate "
                    f"{imported.legacy_estimated_completion}; verify requester correspondence "
                    "before creating or revising any active-system estimate."
                )
            if imported.source_attachment_count:
                # A warning, not a blocker: the acknowledgement already made
                # this an accepted, reasoned decision. It appears here so the
                # signed cutover record states exactly what did not move.
                warnings.append(
                    f"Source request {imported.source_id} left "
                    f"{imported.source_attachment_count} file(s) in the source system; "
                    "they are not in this application and must be migrated through "
                    "the scanned attachment manifest before the source is retired."
                )
    return CutoverReport(
        ready=not blockers,
        plan_sha256=plan.plan_sha256,
        source_rows=len(plan.rows),
        imported_rows=imported_rows,
        activated_open_rows=activated_open_rows,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )
