# ABOUTME: A Records Officer uploads files onto a case as candidate records,
# ABOUTME: each one scanned, retained immutably, deduped, and audited.
"""Staff record uploads: files a Records Officer adds to a case directly.

Records reach a case three ways: Vault ingest, the NextRequest file
migration, and this -- a file the officer already has (a department emailed
it, a custodian handed over a USB stick, a scan of a paper file). Every
uploaded file becomes an ordinary unreviewed ``RecordItem`` with
``source_system="upload"``; it is a *candidate*, and review decides whether it
is responsive, exactly as for anything Vault collected.

What this path guarantees:

* **Fail closed.** Bytes go through the shared quarantine and ClamAV path in
  :mod:`apps.records.scanned_records`. An infected file is refused and leaves
  nothing outside quarantine; a scanner that cannot be reached refuses the
  file too. Neither is ever retained "for now".
* **Server-side facts.** Size and SHA-256 are computed here from the bytes
  received. The media type is decided from the filename the way every other
  record source decides it, never from the browser's declared type.
* **Records Officer only, open cases only.** Adding candidate records to a
  closed case would change what a closed response rested on. Reopen first,
  the same rule installments and deposit gates follow.
* **Custodian left blank.** ``RecordItem.custodian`` names where a record was
  found (a mailbox, a system), not who handled it. The uploader is recorded
  in the append-only audit log instead, once per record.
* **No duplicates.** A file byte-identical to a record already on the case
  (or earlier in the same upload) is reported, not added a second time. The
  check runs twice: once before scanning, to skip the work, and again under
  the case's row lock immediately before the row is written, so two
  concurrent uploads of one file to one case serialize and the loser is
  reported as a duplicate.
* **Every file is judged on its own.** Authority and the case's open state are
  re-checked before each file and again under the lock before its row is
  written. A case that closes, or an officer who loses the role, part-way
  through a batch refuses the remaining files with their own outcome; the
  files already added stay added and are reported.

Why the lock does not span storage: the case row lock is the one every
lifecycle write on the case takes, and holding it across a scan and a
100 MiB object write would stall those writes for the length of an upload.
So the bytes are retained first and the row decision is made under the lock.
The cost is a narrow race: a copy retained under ``records/`` whose row then
loses (to a concurrent duplicate, a closure, or a revoked role). The runtime
role has no delete permission under ``records/`` -- the bucket is versioned
and retained records are deliberately undeletable by the application -- so
that copy is left in place, unreferenced, and a
``records.record_upload_orphaned`` audit entry names its key for an operator.
The pre-scan checks keep this to genuine races rather than the common case.
"""
from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import asdict, dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps import secure_files
from apps.audit.models import audit
from apps.configuration import runtime

from .models import PublicRecordsRequest, RecordItem
from .scanned_records import (
    create_scanned_record_item,
    guess_record_content_type,
    retain_scanned_record_bytes,
)
from .services import CLOSED_STATUSES, _require_current_records_officer
from .storage import READ_CHUNK_SIZE

SOURCE_SYSTEM = "upload"
QUARANTINE_PREFIX = "quarantine/record-uploads/"

OUTCOME_ADDED = "added"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_EMPTY = "empty"
OUTCOME_INFECTED = "infected"
OUTCOME_SCANNER_UNAVAILABLE = "scanner_unavailable"
OUTCOME_CASE_CLOSED = "case_closed"
OUTCOME_NOT_AUTHORIZED = "not_authorized"
#: Every outcome one file can have. The API's schema enum
#: (``RecordUploadOutcomeEnum`` in ``config/settings.py``) lists the same set.
OUTCOMES = (
    OUTCOME_ADDED,
    OUTCOME_DUPLICATE,
    OUTCOME_EMPTY,
    OUTCOME_INFECTED,
    OUTCOME_SCANNER_UNAVAILABLE,
    OUTCOME_CASE_CLOSED,
    OUTCOME_NOT_AUTHORIZED,
)

_AUTHORITY_MESSAGE = "Records Officer authority is required to upload records."


class RecordUploadClosedCase(ValidationError):
    """The case is closed; it must be reopened before records are added."""


class RecordUploadLimitExceeded(ValidationError):
    """The upload is larger or longer than the district's configured limits."""


@dataclass(frozen=True)
class UploadResult:
    filename: str
    outcome: str
    detail: str = ""
    record_item_id: str | None = None
    sha256: str = ""
    size: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _quarantine_key(request_id) -> str:
    return f"{QUARANTINE_PREFIX}{uuid.UUID(str(request_id))}/{uuid.uuid4()}"


def _display_name(upload) -> str:
    """The filename a reviewer sees. Display only; never part of a key.

    Django's multipart parser already reduces a browser's name to its last
    path segment, but this is also the service an agent or a script reaches,
    so it does not rely on that: control characters go, then everything up
    to the last ``/`` or ``\\``, and a name left as dots is no name at all.
    """
    raw = str(getattr(upload, "name", "") or "")
    cleaned = "".join(
        character
        for character in raw
        if not unicodedata.category(character).startswith("C")
    )
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not cleaned.strip("."):
        cleaned = ""
    return (cleaned or "upload")[:500]


def _digest(upload) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    upload.seek(0)
    while chunk := upload.read(READ_CHUNK_SIZE):
        size += len(chunk)
        digest.update(chunk)
    upload.seek(0)
    return size, digest.hexdigest()


def _authorize(actor, prr_id):
    """Check authority and that the case is open, under the case's row lock."""
    _require_current_records_officer(actor=actor, message=_AUTHORITY_MESSAGE)
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr_id)
    if prr.status in CLOSED_STATUSES:
        raise RecordUploadClosedCase(
            f"This request is closed ({prr.status}); reopen it before "
            "adding records."
        )
    return prr


def _check_limits(uploads):
    if not uploads:
        raise ValidationError("Choose at least one file to upload.")
    max_count = runtime.get("PRR_RECORD_UPLOAD_MAX_COUNT")
    max_bytes = runtime.get("PRR_RECORD_UPLOAD_MAX_BYTES")
    max_total = runtime.get("PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES")
    if len(uploads) > max_count:
        raise RecordUploadLimitExceeded(
            f"At most {max_count} files may be uploaded at once."
        )
    sizes = [int(getattr(upload, "size", 0) or 0) for upload in uploads]
    if any(size > max_bytes for size in sizes):
        raise RecordUploadLimitExceeded(
            "A file exceeds the per-file size limit."
        )
    if sum(sizes) > max_total:
        raise RecordUploadLimitExceeded(
            "The files exceed the combined size limit."
        )
    return max_bytes


def upload_records(prr, uploads, *, actor, _client=None) -> list[UploadResult]:
    """Add each uploaded file to ``prr`` as a candidate record.

    Refuses the whole upload (raising) when the actor is not a current
    Records Officer, the case is closed, or a limit is exceeded. After that,
    each file has its own outcome, so one infected file does not discard the
    clean ones beside it.
    """
    uploads = list(uploads)
    with transaction.atomic():
        prr = _authorize(actor, prr.pk)
    max_bytes = _check_limits(uploads)
    client = secure_files._object_store_client() if _client is None else _client

    results: list[UploadResult] = []
    seen: dict[str, str] = {}
    for upload in uploads:
        results.append(  # noqa: PERF401 - `seen` carries across iterations
            _upload_one(
                prr,
                upload,
                actor=actor,
                max_bytes=max_bytes,
                seen=seen,
                client=client,
            )
        )
    return results


def _reject(prr, *, actor, filename, size, sha256, outcome, detail):
    audit(
        "records.record_upload_rejected",
        target=str(prr.pk),
        actor=actor,
        request_number=prr.number,
        filename=filename,
        sha256=sha256,
        size=size,
        reason=outcome,
        detail=detail[:500],
    )
    return UploadResult(
        filename=filename,
        outcome=outcome,
        detail=detail,
        sha256=sha256,
        size=size,
    )


def _refusal_outcome(exc) -> tuple[str, str]:
    if isinstance(exc, RecordUploadClosedCase):
        return OUTCOME_CASE_CLOSED, exc.messages[0]
    return (
        OUTCOME_NOT_AUTHORIZED,
        "You no longer hold Records Officer authority for this case.",
    )


def _existing_duplicate(prr, sha256, seen):
    return seen.get(sha256) or (
        RecordItem.objects.filter(request=prr, sha256=sha256)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
        .first()
    )


def _duplicate_result(filename, existing, sha256, size) -> UploadResult:
    return UploadResult(
        filename=filename,
        outcome=OUTCOME_DUPLICATE,
        detail="A byte-identical record is already on this case.",
        record_item_id=str(existing),
        sha256=sha256,
        size=size,
    )


def _upload_one(prr, upload, *, actor, max_bytes, seen, client) -> UploadResult:
    filename = _display_name(upload)
    size, sha256 = _digest(upload)
    if size == 0:
        return UploadResult(
            filename=filename,
            outcome=OUTCOME_EMPTY,
            detail="The file is empty.",
            sha256=sha256,
        )

    # Before any bytes move: a case closed, or a role lost, since the last
    # file refuses this one without retaining anything.
    try:
        with transaction.atomic():
            _authorize(actor, prr.pk)
    except (PermissionDenied, RecordUploadClosedCase) as exc:
        outcome, detail = _refusal_outcome(exc)
        return _reject(
            prr, actor=actor, filename=filename, size=size, sha256=sha256,
            outcome=outcome, detail=detail,
        )

    existing = _existing_duplicate(prr, sha256, seen)
    if existing:
        return _duplicate_result(filename, existing, sha256, size)

    content_type = guess_record_content_type(filename)
    try:
        storage_key, detail = retain_scanned_record_bytes(
            prr.id,
            upload,
            quarantine_key=_quarantine_key(prr.id),
            original_filename=filename,
            size=size,
            sha256=sha256,
            content_type=content_type,
            max_bytes=max_bytes,
            client=client,
        )
    except secure_files.AttachmentScanUnavailable as exc:
        return _reject(
            prr,
            actor=actor,
            filename=filename,
            size=size,
            sha256=sha256,
            outcome=OUTCOME_SCANNER_UNAVAILABLE,
            detail=(
                "Malware scanning is unavailable, so the file was not added. "
                f"({exc})"
            ),
        )
    if storage_key is None:
        return _reject(
            prr,
            actor=actor,
            filename=filename,
            size=size,
            sha256=sha256,
            outcome=OUTCOME_INFECTED,
            detail=f"Malware was detected: {detail}",
        )

    refusal = None
    duplicate_of = None
    record = None
    with transaction.atomic():
        # Re-checked under the case row lock: the case may have closed, the
        # officer may have lost the role, or a concurrent upload of the same
        # bytes may have committed while these were being scanned. Every
        # upload to this case takes this lock, so the duplicate check and
        # the insert below cannot interleave with another upload's.
        try:
            locked = _authorize(actor, prr.pk)
        except (PermissionDenied, RecordUploadClosedCase) as exc:
            refusal = _refusal_outcome(exc)
        if refusal is None:
            duplicate_of = _existing_duplicate(locked, sha256, seen)
        if refusal is None and duplicate_of is None:
            record = create_scanned_record_item(
                locked,
                source_system=SOURCE_SYSTEM,
                original_filename=filename,
                storage_key=storage_key,
                sha256=sha256,
                size=size,
                content_type=content_type,
            )
            audit(
                "records.record_uploaded",
                target=str(record.pk),
                actor=actor,
                request_number=prr.number,
                filename=record.original_filename,
                sha256=sha256,
                size=size,
                content_type=record.content_type,
                source_system=SOURCE_SYSTEM,
            )

    if record is None:
        reason = refusal[0] if refusal else OUTCOME_DUPLICATE
        _record_orphan(
            prr, actor=actor, filename=filename, size=size, sha256=sha256,
            storage_key=storage_key, reason=reason,
        )
        if refusal:
            return _reject(
                prr, actor=actor, filename=filename, size=size, sha256=sha256,
                outcome=refusal[0], detail=refusal[1],
            )
        return _duplicate_result(filename, duplicate_of, sha256, size)

    seen[sha256] = str(record.pk)
    return UploadResult(
        filename=filename,
        outcome=OUTCOME_ADDED,
        record_item_id=str(record.pk),
        sha256=sha256,
        size=size,
    )


def _record_orphan(prr, *, actor, filename, size, sha256, storage_key, reason):
    """Name a retained copy whose row lost, so an operator can find it.

    The runtime role cannot delete under ``records/`` by design, so this is
    the only trace the copy gets; see the module docstring.
    """
    audit(
        "records.record_upload_orphaned",
        target=str(prr.pk),
        actor=actor,
        request_number=prr.number,
        filename=filename,
        sha256=sha256,
        size=size,
        storage_key=storage_key,
        reason=reason,
    )
