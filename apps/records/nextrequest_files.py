# ABOUTME: Plans and commits the NextRequest file migration: verifies staged
# ABOUTME: bytes, expands archives, dedupes on sha256, scans, and stores.
"""Migrate the files the CSV row importer deliberately left behind.

The row importer moves no files at all - it records that they stayed in the
source system and why. This module is the separately approved path that moves
them, and it keeps the same shape as its sibling: a dry run produces a plan
with a SHA-256 digest, and only that reviewed digest can be committed.

Three properties matter more than throughput here:

* **A requester upload never becomes a responsive record.** The source
  classification decides which model a file lands in, and an unresolved
  classification stops the run rather than guessing. Getting this wrong
  fabricates a responsive record out of the requester's own evidence.
* **Archives are expanded, not stored.** A ZIP cannot be previewed, OCR'd or
  redacted (`preview.py` renders PDF alone), so storing a container hands
  reviewers something they cannot review, and a reviewer who cannot open a
  file cannot withhold from it either.
* **Byte-identical files collapse onto one stored item.** Every source
  document that collapsed still gets its own provenance row, so dedupe stays
  visible instead of looking like files went missing.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from django.core.exceptions import ValidationError
from django.db import transaction

from apps import secure_files
from apps.audit.models import audit
from apps.configuration import runtime

from .models import (
    IntakeAttachment,
    LegacyFileImport,
    LegacyRequestImport,
    RecordItem,
    StatusEvent,
)
from .scanned_records import (
    create_scanned_record_item,
    guess_record_content_type,
    retain_scanned_record_bytes,
)
from .storage import READ_CHUNK_SIZE

#: How deep nested archives are followed. Real district collections nest a
#: level or two (a year folder zipped inside a topic zip); anything deeper is
#: far more likely to be a decompression bomb than a record.
MAX_ARCHIVE_DEPTH = 3

#: Members that exist only because a file was zipped on a Mac or Windows.
#: They carry no record content and would otherwise land in front of a
#: reviewer as unopenable noise.
_ARCHIVE_NOISE_PREFIXES = ("__MACOSX/",)
_ARCHIVE_NOISE_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", "icon"}

#: Source classifications and the model each one may become. The mapping is
#: explicit because the two directions are not symmetric: turning a staff
#: collection into an IntakeAttachment merely hides it, while turning a
#: requester upload into a RecordItem invents a responsive record.
CLASSIFICATION_RECORD = "hand_gathered"
CLASSIFICATION_ATTACHMENT = "requester_upload"
CLASSIFICATION_AMBIGUOUS = "ambiguous"
RESOLVABLE_CLASSIFICATIONS = (CLASSIFICATION_RECORD, CLASSIFICATION_ATTACHMENT)

SOURCE_SYSTEM = "nextrequest_legacy"


class LegacyFileMigrationError(ValidationError):
    pass


@dataclass(frozen=True)
class PlannedItem:
    """One thing that will be stored, or one reason it will not be."""

    source_document_id: str
    source_number: str
    source_title: str
    member_path: str
    source_visibility: str
    classification: str
    sha256: str
    size: int
    content_type: str
    #: Set when this item is byte-identical to one already planned or stored.
    duplicate_of_document_id: str = ""
    skip_reason: str = ""

    @property
    def stored(self) -> bool:
        return not self.duplicate_of_document_id and not self.skip_reason

    def digest_row(self) -> dict:
        return {
            "source_document_id": self.source_document_id,
            "source_number": self.source_number,
            "member_path": self.member_path,
            "sha256": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
            "classification": self.classification,
            "duplicate_of": self.duplicate_of_document_id,
            "skip_reason": self.skip_reason,
        }


@dataclass
class FilePlan:
    items: list[PlannedItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest_sha256: str = ""
    plan_sha256: str = ""

    @property
    def stored_items(self) -> list[PlannedItem]:
        return [item for item in self.items if item.stored]

    def counts(self) -> dict:
        stored = self.stored_items
        return {
            "manifest_rows": len({item.source_document_id for item in self.items}),
            "planned_items": len(self.items),
            "to_store": len(stored),
            "duplicates": sum(
                1 for item in self.items if item.duplicate_of_document_id
            ),
            "skipped": sum(1 for item in self.items if item.skip_reason),
            "record_items": sum(
                1 for item in stored if item.classification == CLASSIFICATION_RECORD
            ),
            "intake_attachments": sum(
                1
                for item in stored
                if item.classification == CLASSIFICATION_ATTACHMENT
            ),
            "bytes_to_store": sum(item.size for item in stored),
        }


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _guess_content_type(name: str) -> str:
    return guess_record_content_type(name)


def _is_archive(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() == ".zip"


def _is_archive_noise(name: str) -> bool:
    if any(name.startswith(prefix) for prefix in _ARCHIVE_NOISE_PREFIXES):
        return True
    return PurePosixPath(name).name.lower() in _ARCHIVE_NOISE_NAMES


def _reject_unsafe_member(info) -> None:
    """Refuse zip-slip, absolute, and encrypted members.

    Mirrors the Vault ingest guard. ``flag_bits & 0x1`` marks an encrypted
    member, which cannot be scanned for malware and so must never be stored.
    """
    path = PurePosixPath(info.filename)
    if (
        not info.filename
        or path.is_absolute()
        or ".." in path.parts
        or info.flag_bits & 0x1
    ):
        raise LegacyFileMigrationError(
            f"Archive member {info.filename!r} is unsafe to expand."
        )


def _iter_archive_members(source, *, prefix: str, depth: int):
    """Yield ``(member_path, temp_file, size, sha256)`` for every real member.

    Bounded on member count, expanded bytes, and nesting depth, so a crafted
    archive cannot exhaust the host. Nested archives recurse; everything else
    is yielded as a leaf.
    """
    if depth > MAX_ARCHIVE_DEPTH:
        raise LegacyFileMigrationError(
            f"Archive {prefix!r} nests deeper than {MAX_ARCHIVE_DEPTH} levels."
        )
    source.seek(0)
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise LegacyFileMigrationError(
            f"Archive {prefix!r} is not a readable ZIP."
        ) from exc

    with archive:
        members = archive.infolist()
        if len(members) > runtime.get("PRR_VAULT_ARCHIVE_MAX_MEMBERS"):
            raise LegacyFileMigrationError(
                f"Archive {prefix!r} has more members than the configured limit."
            )
        # Read once, before walking a legacy archive: the limits bound this
        # decompression as a whole, so every member is judged against the same
        # pair, and a store read per member (or per chunk!) would be a cache
        # round trip per member.
        expanded_limit = runtime.get("PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES")
        memory_limit = runtime.get("PRR_VERIFIED_OBJECT_MEMORY_BYTES")
        expanded = 0
        for info in members:
            if info.is_dir():
                continue
            _reject_unsafe_member(info)
            if _is_archive_noise(info.filename):
                continue
            expanded += info.file_size
            if expanded > expanded_limit:
                raise LegacyFileMigrationError(
                    f"Archive {prefix!r} expands beyond the configured limit."
                )
            member_path = f"{prefix}/{info.filename}" if prefix else info.filename
            with (
                archive.open(info) as member,
                tempfile.SpooledTemporaryFile(
                    max_size=memory_limit,
                    mode="w+b",
                ) as staged,
            ):
                digest = hashlib.sha256()
                size = 0
                while chunk := member.read(READ_CHUNK_SIZE):
                    size += len(chunk)
                    if size > expanded_limit:
                        raise LegacyFileMigrationError(
                            f"Archive member {member_path!r} exceeds its limit."
                        )
                    digest.update(chunk)
                    staged.write(chunk)
                staged.seek(0)
                if _is_archive(info.filename):
                    yield from _iter_archive_members(
                        staged,
                        prefix=member_path,
                        depth=depth + 1,
                    )
                    continue
                yield member_path, staged, size, digest.hexdigest()


def read_manifest(manifest_path: Path) -> tuple[list[dict], str]:
    raw = Path(manifest_path).read_bytes()
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    required = {
        "document_id", "request", "classification", "visibility",
        "title", "path", "bytes", "sha256", "status",
    }
    missing = required - set(rows[0] if rows else {})
    if missing:
        raise LegacyFileMigrationError(
            f"Manifest is missing required columns: {sorted(missing)}."
        )
    return rows, manifest_sha256


def build_plan(
    manifest_path,
    files_root,
    *,
    ambiguous_classification: str = "",
    expand_archives: bool = True,
) -> FilePlan:
    """Resolve every manifest row into the items that would be stored.

    Nothing is written and nothing is uploaded. Every failure is collected as
    a blocker so one run reports the whole picture instead of stopping at the
    first bad row.
    """
    if ambiguous_classification and ambiguous_classification not in RESOLVABLE_CLASSIFICATIONS:
        raise LegacyFileMigrationError(
            "An ambiguous resolution must name a real classification: "
            f"{list(RESOLVABLE_CLASSIFICATIONS)}."
        )
    rows, manifest_sha256 = read_manifest(Path(manifest_path))
    files_root = Path(files_root)
    plan = FilePlan(manifest_sha256=manifest_sha256)

    requests_by_number = {
        item.source_number: item
        for item in LegacyRequestImport.objects.select_related("request")
        if item.source_number
    }
    #: sha256 -> the document that will own the stored copy, per request.
    owner: dict[tuple[str, str], str] = {}

    for row in sorted(rows, key=lambda r: (r["request"], r["document_id"])):
        document_id = row["document_id"].strip()
        source_number = row["request"].strip()
        title = row["title"].strip()
        classification = row["classification"].strip()

        if row.get("status", "").strip() != "ok":
            plan.blockers.append(
                f"{document_id}: manifest row is not marked ok."
            )
            continue
        if classification == CLASSIFICATION_AMBIGUOUS:
            if not ambiguous_classification:
                plan.blockers.append(
                    f"{document_id} ({title}): classification is ambiguous and no "
                    "resolution was supplied. Resolve it explicitly rather than "
                    "letting the importer guess."
                )
                continue
            classification = ambiguous_classification
        if classification not in RESOLVABLE_CLASSIFICATIONS:
            plan.blockers.append(
                f"{document_id}: unknown classification {classification!r}."
            )
            continue

        imported = requests_by_number.get(source_number)
        if imported is None:
            plan.blockers.append(
                f"{document_id}: source request {source_number} was never imported."
            )
            continue

        path = files_root / row["path"]
        if not path.is_file():
            plan.blockers.append(f"{document_id}: staged file {row['path']} is missing.")
            continue
        size, digest = _sha256_file(path)
        if digest != row["sha256"].strip() or size != int(row["bytes"]):
            plan.blockers.append(
                f"{document_id}: staged bytes do not match the manifest digest."
            )
            continue

        leaves: list[tuple[str, int, str, str]] = []
        if expand_archives and _is_archive(title):
            try:
                with path.open("rb") as handle:
                    for member_path, _staged, member_size, member_digest in (
                        _iter_archive_members(handle, prefix="", depth=1)
                    ):
                        leaves.append((
                            member_path,
                            member_size,
                            member_digest,
                            _guess_content_type(member_path),
                        ))
            except LegacyFileMigrationError as exc:
                plan.blockers.append(f"{document_id}: {exc.messages[0]}")
                continue
            if not leaves:
                plan.items.append(PlannedItem(
                    source_document_id=document_id,
                    source_number=source_number,
                    source_title=title,
                    member_path="",
                    source_visibility=row["visibility"].strip(),
                    classification=classification,
                    sha256=digest,
                    size=size,
                    content_type=_guess_content_type(title),
                    skip_reason="Archive carried no members to store.",
                ))
                plan.warnings.append(
                    f"{document_id} ({title}) is an empty archive and stores nothing."
                )
                continue
        else:
            leaves.append(("", size, digest, _guess_content_type(title)))

        for member_path, member_size, member_digest, content_type in leaves:
            skip_reason = ""
            if member_size == 0:
                skip_reason = "File is empty."
            key = (source_number, member_digest)
            duplicate_of = ""
            if not skip_reason:
                if key in owner:
                    duplicate_of = owner[key]
                else:
                    existing = _existing_stored_sha256(
                        imported.request_id, member_digest, classification
                    )
                    if existing:
                        duplicate_of = existing
                    else:
                        owner[key] = document_id
            plan.items.append(PlannedItem(
                source_document_id=document_id,
                source_number=source_number,
                source_title=title,
                member_path=member_path,
                source_visibility=row["visibility"].strip(),
                classification=classification,
                sha256=member_digest,
                size=member_size,
                content_type=content_type,
                duplicate_of_document_id=duplicate_of,
                skip_reason=skip_reason,
            ))

    plan.items.sort(key=lambda item: (
        item.source_number, item.source_document_id, item.member_path
    ))
    plan.plan_sha256 = hashlib.sha256(json.dumps(
        {
            "manifest_sha256": manifest_sha256,
            "expand_archives": expand_archives,
            "ambiguous_classification": ambiguous_classification,
            "rows": [item.digest_row() for item in plan.items],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return plan


def _existing_stored_sha256(request_id, digest, classification) -> str:
    """Has this exact content already been stored for this request?

    Makes a re-run idempotent without a separate bookkeeping pass, and stops a
    file that Vault ingest already collected from landing a second time.
    """
    if classification == CLASSIFICATION_RECORD:
        exists = RecordItem.objects.filter(
            request_id=request_id, sha256=digest
        ).exists()
    else:
        exists = IntakeAttachment.objects.filter(
            request_id=request_id, sha256=digest
        ).exists()
    if not exists:
        return ""
    prior = (
        LegacyFileImport.objects.filter(request_id=request_id, sha256=digest)
        .order_by("imported_at", "pk")
        .first()
    )
    return prior.source_document_id if prior else "already stored"


def _quarantine_key(request_id) -> str:
    return f"quarantine/legacy-files/{uuid.UUID(str(request_id))}/{uuid.uuid4()}"


def _open_planned_bytes(item: PlannedItem, files_root: Path, row: dict):
    """Re-derive one planned item's bytes from the staged source file.

    The plan holds digests, not content, so the commit re-reads and re-hashes.
    A file that changed between planning and commit fails its digest check
    here rather than being stored under a reviewed plan it no longer matches.
    """
    path = files_root / row["path"]
    memory_limit = runtime.get("PRR_VERIFIED_OBJECT_MEMORY_BYTES")
    if not item.member_path:
        staged = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - the spool outlives this block
            max_size=memory_limit, mode="w+b"
        )
        with path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK_SIZE):
                staged.write(chunk)
        staged.seek(0)
        return staged
    with path.open("rb") as handle:
        for member_path, member, _size, _digest in _iter_archive_members(
            handle, prefix="", depth=1
        ):
            if member_path == item.member_path:
                detached = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - the spool outlives this block
                    max_size=memory_limit, mode="w+b"
                )
                while chunk := member.read(READ_CHUNK_SIZE):
                    detached.write(chunk)
                detached.seek(0)
                return detached
    raise LegacyFileMigrationError(
        f"{item.source_document_id}: member {item.member_path!r} vanished from the archive."
    )


def _scan_or_fail(storage_key, *, size, digest, client):
    return secure_files.scan_object_for_malware(
        storage_key,
        expected_size=size,
        expected_sha256=digest,
        max_bytes=runtime.get("PRR_LEGACY_FILE_MAX_BYTES"),
        _client=client,
    )


def _store_record_item(item: PlannedItem, source, request, *, client):
    display_name = item.member_path or item.source_title
    storage_key, detail = retain_scanned_record_bytes(
        request.id,
        source,
        quarantine_key=_quarantine_key(request.id),
        original_filename=display_name,
        size=item.size,
        sha256=item.sha256,
        content_type=item.content_type,
        max_bytes=runtime.get("PRR_LEGACY_FILE_MAX_BYTES"),
        client=client,
    )
    if storage_key is None:
        return None, detail
    record = create_scanned_record_item(
        request,
        source_system=SOURCE_SYSTEM,
        original_filename=display_name,
        storage_key=storage_key,
        sha256=item.sha256,
        size=item.size,
        content_type=item.content_type,
    )
    return record, detail


def _store_intake_attachment(item: PlannedItem, source, request, *, client):
    attachment = IntakeAttachment.objects.create(
        request=request,
        original_filename=(item.member_path or item.source_title)[:255],
        content_type=item.content_type[:255],
        size=item.size,
        sha256=item.sha256,
        storage_key="pending",
    )
    attachment.storage_key = f"quarantine/intake/{request.id}/{attachment.id}"
    attachment.save(update_fields=["storage_key"])
    secure_files.store_quarantined_object(
        attachment.storage_key,
        source,
        content_length=item.size,
        sha256=item.sha256,
        _client=client,
    )
    clean, detail = _scan_or_fail(
        attachment.storage_key, size=item.size, digest=item.sha256, client=client
    )
    from django.utils import timezone

    if clean:
        destination = f"intake/{request.id}/{attachment.id}"
        secure_files.promote_clean_object(
            attachment.storage_key, destination, _client=client
        )
        attachment.storage_key = destination
        attachment.scan_status = IntakeAttachment.ScanStatus.CLEAN
    else:
        secure_files.delete_quarantined_object(
            attachment.storage_key, _client=client
        )
        attachment.scan_status = IntakeAttachment.ScanStatus.INFECTED
    attachment.scan_detail = detail[:500]
    attachment.scanned_at = timezone.now()
    attachment.save(
        update_fields=["storage_key", "scan_status", "scan_detail", "scanned_at"]
    )
    return (attachment if clean else None), detail


def _timeline_note(source_number: str, counts: dict) -> str:
    return (
        f"Migrated {counts['stored']} file(s) from NextRequest for source request "
        f"{source_number}: {counts['records']} responsive record(s) and "
        f"{counts['attachments']} requester upload(s). "
        f"{counts['duplicates']} byte-identical copy(ies) collapsed onto stored "
        f"items and {counts['skipped']} empty file(s) stored nothing. Archives "
        "were expanded so their contents can be reviewed and redacted. This "
        "supersedes the earlier note that these files remained in the source "
        "system. The recorded source attachment count still describes what "
        "NextRequest held at export and is unchanged."
    )


def commit_plan(
    plan: FilePlan,
    manifest_path,
    files_root,
    *,
    operator: str,
    _client=None,
):
    """Store every planned item, then correct each case's timeline.

    Storage happens outside the per-request transaction because object writes
    are not transactional; the provenance row for an item is written in the
    same atomic block as the item itself, so the two can never disagree.
    """
    if plan.blockers:
        raise LegacyFileMigrationError(
            "A plan with blockers cannot be committed."
        )
    rows, _ = read_manifest(Path(manifest_path))
    rows_by_id = {row["document_id"].strip(): row for row in rows}
    files_root = Path(files_root)
    client = secure_files._object_store_client() if _client is None else _client

    imports = {
        item.source_number: item
        for item in LegacyRequestImport.objects.select_related("request")
        if item.source_number
    }
    per_request: dict[str, dict] = {}
    results = {"created": 0, "duplicate": 0, "skipped": 0, "infected": 0}

    for item in plan.items:
        imported = imports[item.source_number]
        request = imported.request
        tally = per_request.setdefault(
            item.source_number,
            {"request": request, "stored": 0, "records": 0,
             "attachments": 0, "duplicates": 0, "skipped": 0, "infected": 0},
        )
        if LegacyFileImport.objects.filter(
            source_document_id=item.source_document_id,
            member_path=item.member_path,
        ).exists():
            continue

        if item.skip_reason or item.duplicate_of_document_id:
            outcome = (
                LegacyFileImport.Outcome.SKIPPED
                if item.skip_reason
                else LegacyFileImport.Outcome.DUPLICATE
            )
            LegacyFileImport.objects.create(
                request=request,
                source_document_id=item.source_document_id,
                source_title=item.source_title[:500],
                member_path=item.member_path[:1000],
                source_visibility=item.source_visibility[:50],
                sha256=item.sha256,
                size=item.size,
                outcome=outcome,
                detail=(
                    item.skip_reason
                    or f"Byte-identical to {item.duplicate_of_document_id}."
                )[:500],
            )
            key = "skipped" if item.skip_reason else "duplicates"
            tally[key] += 1
            results["skipped" if item.skip_reason else "duplicate"] += 1
            continue

        source = _open_planned_bytes(item, files_root, rows_by_id[item.source_document_id])
        try:
            with source:
                if item.classification == CLASSIFICATION_RECORD:
                    stored, detail = _store_record_item(
                        item, source, request, client=client
                    )
                    record, attachment = stored, None
                else:
                    stored, detail = _store_intake_attachment(
                        item, source, request, client=client
                    )
                    record, attachment = None, stored
        except Exception as exc:
            raise LegacyFileMigrationError(
                f"{item.source_document_id} ({item.member_path or item.source_title}): "
                f"{exc}"
            ) from exc

        LegacyFileImport.objects.create(
            request=request,
            source_document_id=item.source_document_id,
            source_title=item.source_title[:500],
            member_path=item.member_path[:1000],
            source_visibility=item.source_visibility[:50],
            sha256=item.sha256,
            size=item.size,
            outcome=(
                LegacyFileImport.Outcome.CREATED
                if stored is not None
                else LegacyFileImport.Outcome.INFECTED
            ),
            record_item=record,
            intake_attachment=attachment,
            detail=("" if stored is not None else detail)[:500],
        )
        if stored is None:
            tally["infected"] += 1
            results["infected"] += 1
            continue
        tally["stored"] += 1
        tally["records" if record is not None else "attachments"] += 1
        results["created"] += 1

    for source_number, tally in sorted(per_request.items()):
        if not any(
            tally[key] for key in ("stored", "duplicates", "skipped", "infected")
        ):
            continue
        request = tally["request"]
        with transaction.atomic():
            StatusEvent.objects.create(
                request=request,
                from_status=request.status,
                to_status=request.status,
                note=_timeline_note(source_number, tally),
                visible_to_requester=False,
            )
            audit(
                "records.legacy_files_migrated",
                target=str(request.id),
                source_number=source_number,
                operator=operator,
                stored=tally["stored"],
                record_items=tally["records"],
                intake_attachments=tally["attachments"],
                duplicates=tally["duplicates"],
                skipped=tally["skipped"],
                infected=tally["infected"],
                plan_sha256=plan.plan_sha256,
                manifest_sha256=plan.manifest_sha256,
            )
    return results
