"""Resumable Google Vault export retention, conversion, OCR, and dedupe."""

from __future__ import annotations

import hashlib
import html
import io
import mailbox
import re
import tempfile
import textwrap
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps import secure_files
from apps.audit.models import audit
from apps.configuration import runtime

from .email_metadata import parse_delivered_to, parse_email_metadata
from .models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    StatusEvent,
    VaultExportObject,
    VaultIngestRun,
    VaultMessageArtifact,
)
from .storage import (
    READ_CHUNK_SIZE,
    generated_record_storage_key,
    generated_vault_export_storage_key,
    generated_vault_message_storage_key,
    load_verified_object,
    store_immutable_object,
)
from .vault_metadata import (
    MetadataCsv,
    custodian_accounts,
    custodian_accounts_for_message,
    is_metadata_csv_object,
    normalize_message_id,
    read_metadata_csv,
)

_METADATA_SUFFIXES = {".csv", ".xml", ".json", ".txt"}
_HTML_TAG = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class ConvertedVaultRecord:
    content: bytes
    filename: str
    content_type: str
    ocr_text: str


class VaultIngestError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class VaultIngestPermanentError(VaultIngestError):
    pass


class _BoundedWriter:
    def __init__(self, destination, limit):
        self.destination = destination
        self.limit = limit
        self.bytes_written = 0

    def write(self, value):
        self.bytes_written += len(value)
        if self.bytes_written > self.limit:
            raise VaultIngestPermanentError(
                "export_too_large",
                "A Vault export object exceeded the configured byte limit.",
            )
        return self.destination.write(value)

    def flush(self):
        return self.destination.flush()

    def tell(self):
        return self.destination.tell()


def _identity_sha256(bucket_name: str, object_name: str) -> str:
    return hashlib.sha256(
        f"{bucket_name}\0{object_name}".encode()
    ).hexdigest()


def safe_error_detail(exc: Exception) -> str:
    """One bounded line of an exception's text, safe for a terminal or a
    stored detail column: control characters and line breaks collapsed."""
    detail = " ".join(_CONTROL.sub(" ", str(exc)).split())
    return detail[:500] or type(exc).__name__


def _export_files(export: dict) -> list[dict]:
    sink = export.get("cloudStorageSink")
    if not isinstance(sink, dict):
        return []
    files = []
    for value in sink.get("files", []):
        if not isinstance(value, dict):
            raise VaultIngestPermanentError(
                "export_manifest_invalid",
                "The Vault export file manifest is invalid.",
            )
        bucket_name = str(value.get("bucketName", "")).strip()
        object_name = str(value.get("objectName", "")).strip()
        try:
            size = int(value.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise VaultIngestPermanentError(
                "export_manifest_invalid",
                "The Vault export file size is invalid.",
            ) from exc
        if not bucket_name or not object_name or size < 0:
            raise VaultIngestPermanentError(
                "export_manifest_invalid",
                "The Vault export file manifest is incomplete.",
            )
        files.append({
            "bucket_name": bucket_name,
            "object_name": object_name,
            "size": size,
        })
    return files


def _snapshot(export: dict, files: list[dict]) -> dict:
    return {
        "id": str(export.get("id", "")),
        "status": str(export.get("status", "")),
        "files": files,
    }


def _existing_completed_run(collection_task_id):
    return (
        VaultIngestRun.objects.filter(
            collection_task_id=collection_task_id,
            state=VaultIngestRun.State.COMPLETE,
        )
        .select_related("collection_task__request")
        .first()
    )


@transaction.atomic
def _claim_run(task, export, files):
    now = timezone.now()
    run, _ = VaultIngestRun.objects.select_for_update().get_or_create(
        collection_task=task,
    )
    if run.state == VaultIngestRun.State.COMPLETE:
        return run, None
    fresh_after = now - timezone.timedelta(
        seconds=runtime.get("PRR_VAULT_INGEST_LEASE_SECONDS"),
    )
    if (
        run.state == VaultIngestRun.State.RUNNING
        and run.heartbeat_at is not None
        and run.heartbeat_at >= fresh_after
    ):
        return run, None
    lease_token = uuid.uuid4()
    run.state = VaultIngestRun.State.RUNNING
    run.lease_token = lease_token
    run.heartbeat_at = now
    run.export_snapshot = _snapshot(export, files)
    run.files_total = len(files)
    run.attempt_count += 1
    run.started_at = run.started_at or now
    run.completed_at = None
    run.last_error_code = ""
    run.last_error_detail = ""
    run.save(update_fields=[
        "state",
        "lease_token",
        "heartbeat_at",
        "export_snapshot",
        "files_total",
        "attempt_count",
        "started_at",
        "completed_at",
        "last_error_code",
        "last_error_detail",
        "updated_at",
    ])
    return run, lease_token


def _heartbeat(run, lease_token):
    changed = VaultIngestRun.objects.filter(
        pk=run.pk,
        state=VaultIngestRun.State.RUNNING,
        lease_token=lease_token,
    ).update(heartbeat_at=timezone.now(), updated_at=timezone.now())
    if not changed:
        raise VaultIngestError(
            "ingest_lease_lost",
            "The Vault ingest lease is no longer owned by this worker.",
        )


def _default_download(bucket_name, object_name, destination):
    from google.cloud import storage as gcs

    from apps.integrations.vault.client import vault_credentials

    credentials = vault_credentials()
    target = runtime.get("PRR_VAULT_IMPERSONATION_TARGET")
    service_account_suffix = ".iam.gserviceaccount.com"
    project = None
    if "@" in target and target.endswith(service_account_suffix):
        project = target.rsplit("@", 1)[1][:-len(service_account_suffix)]
    client = gcs.Client(project=project, credentials=credentials)
    blob = client.bucket(bucket_name).blob(object_name)
    blob.download_to_file(
        destination,
        checksum="auto",
        timeout=runtime.get("PRR_VAULT_GCS_TIMEOUT_SECONDS"),
    )


def _digest_file(source):
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(READ_CHUNK_SIZE):
        size += len(chunk)
        digest.update(chunk)
    source.seek(0)
    return size, digest.hexdigest()


def _retained_export_file(
    source_object,
    task,
    *,
    download,
    client,
):
    if source_object.storage_key:
        return load_verified_object(
            source_object.storage_key,
            expected_size=source_object.size,
            expected_sha256=source_object.sha256,
            _client=client,
        )

    source = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - the spool outlives this block
        max_size=runtime.get("PRR_VERIFIED_OBJECT_MEMORY_BYTES"),
        mode="w+b",
    )
    try:
        download(
            source_object.bucket_name,
            source_object.object_name,
            _BoundedWriter(source, runtime.get("PRR_VAULT_EXPORT_MAX_BYTES")),
        )
        size, sha256 = _digest_file(source)
        if size != source_object.expected_size:
            raise VaultIngestPermanentError(
                "export_integrity_failed",
                "The downloaded Vault export size did not match its manifest.",
            )
        storage_key = generated_vault_export_storage_key(
            task.request_id,
            task.pk,
            object_name=source_object.object_name,
        )
        store_immutable_object(
            storage_key,
            source,
            content_length=size,
            sha256=sha256,
            content_type="application/octet-stream",
            _client=client,
        )
        source_object.storage_key = storage_key
        source_object.sha256 = sha256
        source_object.size = size
        source_object.state = VaultExportObject.State.RETAINED
        source_object.retained_at = timezone.now()
        source_object.last_error_code = ""
        source_object.last_error_detail = ""
        source_object.save(update_fields=[
            "storage_key",
            "sha256",
            "size",
            "state",
            "retained_at",
            "last_error_code",
            "last_error_detail",
        ])
        source.seek(0)
        return source
    except Exception:
        source.close()
        raise


def _mailbox_messages(source):
    with tempfile.NamedTemporaryFile(suffix=".mbox") as mbox_file:
        source.seek(0)
        while chunk := source.read(READ_CHUNK_SIZE):
            mbox_file.write(chunk)
        mbox_file.flush()
        parsed = mailbox.mbox(mbox_file.name, create=False)
        # Read once: one mailbox can hold tens of thousands of messages, and a
        # store read per message would be a cache round trip per message.
        message_limit = runtime.get("PRR_VAULT_MESSAGE_MAX_BYTES")
        try:
            for message in parsed:
                raw = message.as_bytes(policy=policy.SMTP)
                if len(raw) > message_limit:
                    raise VaultIngestPermanentError(
                        "message_too_large",
                        "A Vault message exceeded the configured byte limit.",
                    )
                yield raw
        finally:
            parsed.close()


def _safe_archive_member(info):
    path = PurePosixPath(info.filename)
    if (
        not info.filename
        or path.is_absolute()
        or ".." in path.parts
        or info.flag_bits & 0x1
    ):
        raise VaultIngestPermanentError(
            "archive_unsafe",
            "The Vault export archive contains an unsafe member.",
        )


def _iter_export_messages(source, object_name):
    suffix = PurePosixPath(object_name).suffix.lower()
    if suffix == ".mbox":
        yield from _mailbox_messages(source)
        return
    if suffix in _METADATA_SUFFIXES:
        return
    if suffix != ".zip":
        raise VaultIngestPermanentError(
            "export_format_unsupported",
            "The completed Vault mail export is not MBOX or ZIP.",
        )

    source.seek(0)
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise VaultIngestPermanentError(
            "archive_invalid",
            "The Vault export ZIP is invalid.",
        ) from exc
    with archive:
        members = archive.infolist()
        if len(members) > runtime.get("PRR_VAULT_ARCHIVE_MAX_MEMBERS"):
            raise VaultIngestPermanentError(
                "archive_too_large",
                "The Vault export archive has too many members.",
            )
        # Read once, before walking a potentially hostile archive: the limits
        # are what bound this decompression, so every member is judged against
        # the same pair, and a store read per member (or per chunk!) would be
        # a cache round trip per member.
        expanded_limit = runtime.get("PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES")
        memory_limit = runtime.get("PRR_VERIFIED_OBJECT_MEMORY_BYTES")
        expanded = 0
        for info in members:
            _safe_archive_member(info)
            expanded += info.file_size
            if expanded > expanded_limit:
                raise VaultIngestPermanentError(
                    "archive_too_large",
                    "The Vault export archive expands beyond its limit.",
                )
            if info.is_dir() or PurePosixPath(info.filename).suffix.lower() != ".mbox":
                continue
            with (
                archive.open(info) as member,
                tempfile.SpooledTemporaryFile(
                    max_size=memory_limit,
                    mode="w+b",
                ) as mbox_source,
            ):
                copied = 0
                while chunk := member.read(READ_CHUNK_SIZE):
                    copied += len(chunk)
                    if copied > expanded_limit:
                        raise VaultIngestPermanentError(
                            "archive_too_large",
                            "A Vault export archive member exceeded its limit.",
                        )
                    mbox_source.write(chunk)
                mbox_source.seek(0)
                yield from _mailbox_messages(mbox_source)


def _message_text(raw_message: bytes) -> tuple[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    headers = []
    for label in ("Date", "From", "To", "Cc", "Subject", "Message-ID"):
        value = str(message.get(label, "")).strip()
        if value:
            headers.append(f"{label}: {value}")

    plain_parts = []
    html_parts = []
    attachment_lines = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        if content_disposition == "attachment" or filename:
            attachment_lines.append(
                f"Attachment: {filename or 'unnamed'} ({content_type}, {len(payload)} bytes)"
            )
            if content_type.startswith("text/") and len(payload) <= 1024 * 1024:
                charset = part.get_content_charset() or "utf-8"
                plain_parts.append(payload.decode(charset, errors="replace"))
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(html.unescape(_HTML_TAG.sub(" ", text)))
    body = "\n\n".join(plain_parts or html_parts)
    text = "\n".join([*headers, "", body, *attachment_lines])
    text = _CONTROL.sub(" ", text)[: runtime.get("PRR_VAULT_OCR_TEXT_MAX_CHARS")]
    return str(message.get("Message-ID", "")).strip()[:998], text


def _render_text_pdf(text: str) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    page = None
    y = 54
    for paragraph in text.splitlines() or [""]:
        lines = textwrap.wrap(
            paragraph,
            width=92,
            replace_whitespace=False,
            drop_whitespace=False,
        ) or [""]
        for line in lines:
            if page is None or y > 788:
                page = document.new_page(width=612, height=792)
                y = 54
            safe_line = line.encode("latin-1", errors="replace").decode("latin-1")
            page.insert_text((54, y), safe_line, fontsize=10, fontname="helv")
            y += 13
        y += 4
    try:
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


def _ocr_pdf(pdf_bytes: bytes) -> bytes:
    import ocrmypdf

    with (
        tempfile.NamedTemporaryFile(suffix=".pdf") as input_pdf,
        tempfile.NamedTemporaryFile(suffix=".pdf") as output_pdf,
    ):
            input_pdf.write(pdf_bytes)
            input_pdf.flush()
            ocrmypdf.ocr(
                input_pdf.name,
                output_pdf.name,
                skip_text=True,
                output_type="pdf",
                optimize=1,
                progress_bar=False,
                jobs=1,
            )
            output_pdf.seek(0)
            return output_pdf.read()


def convert_message_to_searchable_pdf(raw_message: bytes) -> ConvertedVaultRecord:
    _message_id, text = _message_text(raw_message)
    subject = BytesParser(policy=policy.default).parsebytes(raw_message).get(
        "Subject",
        "Vault message",
    )
    display_name = _CONTROL.sub(" ", str(subject)).strip()[:450] or "Vault message"
    pdf = _ocr_pdf(_render_text_pdf(text))
    return ConvertedVaultRecord(
        content=pdf,
        filename=f"{display_name}.pdf",
        content_type="application/pdf",
        ocr_text=text,
    )


def _vault_message_quarantine_key(artifact, task) -> str:
    request_id = uuid.UUID(str(task.request_id))
    return (
        f"quarantine/vault-messages/{request_id}/{int(task.pk)}/"
        f"{int(artifact.pk)}.eml"
    )


def _scan_vault_message(
    storage_key,
    *,
    expected_size,
    expected_sha256,
    client,
):
    return secure_files.scan_object_for_malware(
        storage_key,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        max_bytes=runtime.get("PRR_VAULT_MESSAGE_MAX_BYTES"),
        _client=client,
    )


def _cleanup_vault_message_quarantine(storage_key, *, client):
    secure_files.delete_quarantined_object(storage_key, _client=client)


def _mark_message_scan_failed(artifact, *, code, detail):
    artifact.outcome = VaultMessageArtifact.Outcome.FAILED
    artifact.last_error_code = code
    artifact.last_error_detail = detail
    artifact.save(update_fields=[
        "outcome",
        "last_error_code",
        "last_error_detail",
    ])


def _retain_raw_message(
    artifact,
    raw_message,
    task,
    *,
    client,
    scan,
    cleanup_quarantine,
):
    raw_sha256 = hashlib.sha256(raw_message).hexdigest()
    if artifact.raw_sha256 and artifact.raw_sha256 != raw_sha256:
        raise VaultIngestPermanentError(
            "message_identity_changed",
            "A retained Vault message changed at an existing ordinal.",
        )
    if artifact.raw_storage_key:
        return raw_sha256
    quarantine_key = _vault_message_quarantine_key(artifact, task)
    secure_files.store_quarantined_object(
        quarantine_key,
        io.BytesIO(raw_message),
        content_length=len(raw_message),
        sha256=raw_sha256,
        _client=client,
    )
    try:
        clean, _scan_detail = scan(
            quarantine_key,
            expected_size=len(raw_message),
            expected_sha256=raw_sha256,
            client=client,
        )
    except secure_files.AttachmentScanUnavailable as exc:
        detail = "ClamAV could not classify the retained Vault message."
        _mark_message_scan_failed(
            artifact,
            code="message_malware_scan_unavailable",
            detail=detail,
        )
        audit(
            "vault.message_scan_unavailable",
            target=task.request.number,
            collection_task_id=task.pk,
            message_artifact_id=artifact.pk,
            ordinal=artifact.ordinal,
        )
        raise VaultIngestError(
            "message_malware_scan_unavailable",
            detail,
        ) from exc
    if not clean:
        detail = "A retained Vault message failed malware scanning."
        _mark_message_scan_failed(
            artifact,
            code="message_malware_detected",
            detail=detail,
        )
        audit(
            "vault.message_malware_detected",
            target=task.request.number,
            collection_task_id=task.pk,
            message_artifact_id=artifact.pk,
            ordinal=artifact.ordinal,
        )
        raise VaultIngestPermanentError(
            "message_malware_detected",
            detail,
        )
    # Delete the untrusted generation before creating the immutable retained
    # object. A failed final write can safely retry quarantine and scanning;
    # no clean-path quarantine orphan is left behind.
    cleanup_quarantine(quarantine_key, client=client)
    raw_key = generated_vault_message_storage_key(task.request_id, task.pk)
    store_immutable_object(
        raw_key,
        io.BytesIO(raw_message),
        content_length=len(raw_message),
        sha256=raw_sha256,
        content_type="message/rfc822",
        _client=client,
    )
    message_id, _ = _message_text(raw_message)
    artifact.raw_storage_key = raw_key
    artifact.raw_sha256 = raw_sha256
    artifact.raw_size = len(raw_message)
    artifact.message_id = message_id
    artifact.outcome = VaultMessageArtifact.Outcome.RAW_RETAINED
    artifact.last_error_code = ""
    artifact.last_error_detail = ""
    artifact.save(update_fields=[
        "raw_storage_key",
        "raw_sha256",
        "raw_size",
        "message_id",
        "outcome",
        "last_error_code",
        "last_error_detail",
    ])
    return raw_sha256


def task_custodians(task):
    """The mailboxes one collection task searched, in a stable order."""
    return [
        str(value).strip()[:255]
        for value in dict.fromkeys(task.custodian_emails or [])
        if str(value).strip()
    ]


def attach_custodians(item, task, extra_custodians=()):
    """Record every custodian ``task`` found ``item`` in.

    Called on creation *and* on every deduplication, because the point of
    deduplicating one message across N mailboxes is that the surviving record
    still says it was in all N. The primary ``custodian`` field is repaired
    when it was empty so the record always names at least where it came from.

    ``extra_custodians`` carries the mailboxes the export's own metadata CSV
    names for this message. Callers filter them to the configured staff
    domains first, and this function filters them again through
    :func:`apps.records.vault_metadata.custodian_accounts` before writing, so
    the boundary that writes a custodian row is the one that enforces the
    rule, not only each caller. They are added to, never instead of, the
    task's own custodian list: a task with an empty ``custodian_emails`` is
    exactly how PRR-2026-0052 lost its custody evidence, and a task that names
    its mailboxes still names them. The task's list is the officer's own and
    is not second-guessed here. Nothing here reviews, releases, or
    communicates.
    """
    from .models import RecordItemCustodian

    custodians = list(
        dict.fromkeys(
            task_custodians(task) + custodian_accounts(extra_custodians)[0]
        )
    )
    if not custodians:
        return
    RecordItemCustodian.objects.bulk_create(
        [
            RecordItemCustodian(
                record_item=item,
                custodian=custodian,
                collection_task=task,
            )
            for custodian in custodians
        ],
        ignore_conflicts=True,
    )
    if not item.custodian:
        item.custodian = custodians[0]
        item.save(update_fields=["custodian"])


def attach_csv_sets_to_sole_records(run, task, csv_metadata):
    """Give the CSV's Message-ID set to each record this run touched that is,
    now that every message of the export has been read, the sole live record
    of its request carrying that Message-ID.

    The CSV is keyed on Message-ID alone, so its set describes *every* copy
    of a message. That is the right thing to attach only when every other
    copy was folded into this one. The decision cannot be made per message:
    the first copy of a personalized bulk mailing is, at the moment it is
    read, the only live record carrying its Message-ID, and the other
    ninety-nine have not been read yet. It is made here, once, after the run
    has read them all, under the request's row lock so two runs of one
    request cannot each decide it while blind to the other's copies.

    Returns how many records received a set. Filtering to the configured
    staff domains is left to :func:`attach_custodians`.
    """
    if csv_metadata is None:
        return 0
    record_ids = set()
    for record_id, duplicate_of_id in VaultMessageArtifact.objects.filter(
        export_object__ingest_run=run,
    ).values_list("record_item_id", "duplicate_of_id"):
        record_ids.update(
            value for value in (record_id, duplicate_of_id) if value
        )
    if not record_ids:
        return 0
    attached = 0
    with transaction.atomic():
        PublicRecordsRequest.objects.select_for_update().get(
            pk=task.request_id,
        )
        records = list(
            RecordItem.objects.filter(pk__in=record_ids)
            .exclude(review_state=RecordItem.ReviewState.DUPLICATE)
            .exclude(email_message_id="")
            .order_by("created_at", "pk")
        )
        live_copies = Counter(
            RecordItem.objects.filter(
                request_id=task.request_id,
                email_message_id__in={
                    record.email_message_id for record in records
                },
            )
            .exclude(review_state=RecordItem.ReviewState.DUPLICATE)
            .values_list("email_message_id", flat=True)
        )
        for record in records:
            if live_copies[record.email_message_id] != 1:
                continue
            keep, _declined = custodian_accounts_for_message(
                csv_metadata, record.email_message_id,
            )
            if not keep:
                continue
            attach_custodians(record, task, keep)
            attached += 1
    return attached


def _message_duplicate(request_id, metadata):
    """The record this message already is, or None.

    Identity is the Message-ID *and* the body digest together. A Message-ID
    that matches while the body differs is not the same message, and the
    second one becomes its own reviewable record rather than disappearing into
    the first.
    """
    if not metadata.message_id or not metadata.body_sha256:
        # No Message-ID, or nothing to fingerprint. Either way the pair that
        # makes this safe is incomplete, so the message keeps the byte-hash
        # path and becomes its own reviewable record rather than being folded
        # into one on the strength of a header alone.
        return None
    return (
        RecordItem.objects.filter(
            request_id=request_id,
            email_message_id=metadata.message_id,
            email_body_sha256=metadata.body_sha256,
        )
        .order_by("created_at", "pk")
        .first()
    )


def _mark_duplicate(artifact, duplicate):
    artifact.outcome = VaultMessageArtifact.Outcome.DUPLICATE
    artifact.duplicate_of = duplicate
    artifact.record_item = None
    artifact.completed_at = timezone.now()
    artifact.last_error_code = ""
    artifact.last_error_detail = ""
    artifact.save(update_fields=[
        "outcome",
        "duplicate_of",
        "record_item",
        "completed_at",
        "last_error_code",
        "last_error_detail",
    ])


def _process_message(
    source_object,
    ordinal,
    raw_message,
    task,
    *,
    client,
    convert,
    scan,
    cleanup_quarantine,
    csv_metadata=None,
):
    """Ingest one message. Returns True when it named no usable mailbox.

    The return value is the run summary's ``delivered_to_missing`` tally: a
    message that carries no single usable ``Delivered-To`` fell back to the
    task list and, when its record is sole, the CSV set, and an operator
    reading the run should be able to see how often that happened.
    """
    artifact, _ = VaultMessageArtifact.objects.get_or_create(
        export_object=source_object,
        ordinal=ordinal,
    )
    raw_sha256 = hashlib.sha256(raw_message).hexdigest()
    if artifact.raw_sha256 and artifact.raw_sha256 != raw_sha256:
        raise VaultIngestPermanentError(
            "message_identity_changed",
            "A retained Vault message changed at an existing ordinal.",
        )
    if artifact.outcome in {
        VaultMessageArtifact.Outcome.CREATED,
        VaultMessageArtifact.Outcome.DUPLICATE,
    }:
        return False
    _retain_raw_message(
        artifact,
        raw_message,
        task,
        client=client,
        scan=scan,
        cleanup_quarantine=cleanup_quarantine,
    )
    # This copy's own mailbox. Read here so every path below -- creation and
    # all four deduplication paths -- attaches the same answer, and
    # headers-only so it costs one parse of the envelope rather than of a
    # multi-megabyte body. The CSV's Message-ID set is not attached per
    # message at all: see attach_csv_sets_to_sole_records, which decides it
    # once the whole export has been read.
    try:
        delivered_to = parse_delivered_to(raw_message)
    except Exception:  # noqa: BLE001 - a header this copy cannot prove
        # A message whose headers will not parse names no mailbox. That is a
        # fact to fall back from, not an ingest failure: the message below
        # still becomes a reviewable record, and the run counts it.
        delivered_to = ""
    if delivered_to and csv_metadata is not None:
        # Delivered-To is a header a sender can write. When the export's own
        # metadata names the mailboxes that held this message, the header
        # must agree with it; one that does not is treated as absent.
        known = csv_metadata.accounts.get(
            normalize_message_id(artifact.message_id), (),
        )
        if known and delivered_to not in known:
            delivered_to = ""

    def attach(record):
        attach_custodians(record, task, [delivered_to] if delivered_to else [])

    raw_duplicate = (
        VaultMessageArtifact.objects.filter(
            export_object__ingest_run__collection_task__request_id=task.request_id,
            raw_sha256=raw_sha256,
            outcome=VaultMessageArtifact.Outcome.CREATED,
            record_item__isnull=False,
        )
        .exclude(pk=artifact.pk)
        .select_related("record_item")
        .order_by("pk")
        .first()
    )
    if raw_duplicate:
        attach(raw_duplicate.record_item)
        _mark_duplicate(artifact, raw_duplicate.record_item)
        return not delivered_to

    # Message-ID plus the body digest catches what a byte hash cannot: the
    # per-recipient copies of one message sitting in N mailboxes, which differ
    # in their Received and To headers but say the same thing and are one
    # public record. The body digest is what keeps this honest -- Message-ID is
    # a sender-supplied header, and folding a genuinely different message into
    # an existing record on the strength of a reused or forged one is how a
    # responsive record would go missing from a production. Checked before
    # conversion because conversion is the expensive step.
    try:
        metadata = parse_email_metadata(raw_message)
    except Exception as exc:  # noqa: BLE001 - one message, not the export
        # A message this module cannot read is one artifact's problem. Letting
        # it out fails the whole export object, which is then retried whole,
        # hits the same message, and never completes -- so one malformed
        # message would stall an entire custodian's collection on a statutory
        # clock. The message is recorded as failed, by name, and the rest of
        # the export proceeds.
        artifact.outcome = VaultMessageArtifact.Outcome.FAILED
        artifact.last_error_code = "message_metadata_unreadable"
        artifact.last_error_detail = safe_error_detail(exc)
        artifact.save(update_fields=[
            "outcome",
            "last_error_code",
            "last_error_detail",
        ])
        return not delivered_to
    existing = _message_duplicate(task.request_id, metadata)
    if existing is not None:
        attach(existing)
        _mark_duplicate(artifact, existing)
        return not delivered_to

    try:
        converted = convert(raw_message)
    except Exception as exc:
        artifact.outcome = VaultMessageArtifact.Outcome.FAILED
        artifact.last_error_code = "message_conversion_failed"
        artifact.last_error_detail = safe_error_detail(exc)
        artifact.save(update_fields=[
            "outcome",
            "last_error_code",
            "last_error_detail",
        ])
        raise VaultIngestError(
            "message_conversion_failed",
            safe_error_detail(exc),
        ) from exc
    if not isinstance(converted, ConvertedVaultRecord) or not converted.content:
        raise VaultIngestPermanentError(
            "message_conversion_invalid",
            "The Vault message converter returned no record bytes.",
        )

    rendered_sha256 = hashlib.sha256(converted.content).hexdigest()
    converted_duplicate = RecordItem.objects.filter(
        request_id=task.request_id,
        sha256=rendered_sha256,
    ).order_by("created_at", "pk").first()
    if converted_duplicate:
        artifact.rendered_sha256 = rendered_sha256
        artifact.save(update_fields=["rendered_sha256"])
        attach(converted_duplicate)
        _mark_duplicate(artifact, converted_duplicate)
        return not delivered_to

    storage_key = generated_record_storage_key(
        task.request_id,
        original_filename=converted.filename,
    )
    store_immutable_object(
        storage_key,
        io.BytesIO(converted.content),
        content_length=len(converted.content),
        sha256=rendered_sha256,
        content_type=converted.content_type,
        _client=client,
    )
    try:
        # One transaction, not two: a record and the custody, bookkeeping and
        # scheduling that make it a reviewable record commit together or not
        # at all. Split across two commits, a crash in between would leave a
        # record that is already matchable by the deduplication key but names
        # no custodian and whose artifact was never marked CREATED -- and the
        # retry would resolve it as a duplicate of that orphan, so the
        # bookkeeping would never run at all.
        with transaction.atomic():
            item = RecordItem.objects.create(
                request_id=task.request_id,
                collection_task=task,
                source_system="google_vault_gmail",
                # Left empty on purpose: attach_custodians repairs it from
                # the union of the task's custodians and the CSV's accounts,
                # so the primary field and the custodian rows are decided in
                # one place and cannot disagree.
                custodian="",
                original_filename=_CONTROL.sub(" ", converted.filename)[:500],
                storage_key=storage_key,
                sha256=rendered_sha256,
                content_length=len(converted.content),
                content_type=converted.content_type[:255],
                ocr_text=converted.ocr_text[
                    : runtime.get("PRR_VAULT_OCR_TEXT_MAX_CHARS")
                ],
                **metadata.as_record_item_fields(),
            )
            attach(item)
            from apps.work_queue.integration import (
                schedule_source_work_item_reconciliation,
            )

            schedule_source_work_item_reconciliation("records", item.pk)
            artifact.rendered_sha256 = rendered_sha256
            artifact.outcome = VaultMessageArtifact.Outcome.CREATED
            artifact.record_item = item
            artifact.duplicate_of = None
            artifact.completed_at = timezone.now()
            artifact.last_error_code = ""
            artifact.last_error_detail = ""
            artifact.save(update_fields=[
                "rendered_sha256",
                "outcome",
                "record_item",
                "duplicate_of",
                "completed_at",
                "last_error_code",
                "last_error_detail",
            ])
            from .advisory_ai import defer_automatic_record_advisory
            from .models import AdvisoryRun

            defer_automatic_record_advisory(
                item.pk,
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            )
    except IntegrityError:
        # Two custodian mailboxes of one request ingest concurrently, and both
        # saw no duplicate before either committed. The unique constraint on
        # (request, Message-ID, body digest) decides it; the loser records the
        # custodian it found the message in and stops.
        winner = _message_duplicate(task.request_id, metadata)
        if winner is None:
            raise
        artifact.rendered_sha256 = rendered_sha256
        artifact.save(update_fields=["rendered_sha256"])
        attach(winner)
        _mark_duplicate(artifact, winner)
        return not delivered_to
    return not delivered_to


def _refresh_counts(run):
    messages = VaultMessageArtifact.objects.filter(
        export_object__ingest_run=run,
    )
    run.files_processed = run.export_objects.filter(
        state=VaultExportObject.State.COMPLETE,
    ).count()
    run.messages_seen = messages.count()
    run.items_created = messages.filter(
        outcome=VaultMessageArtifact.Outcome.CREATED,
    ).count()
    run.duplicates_skipped = messages.filter(
        outcome=VaultMessageArtifact.Outcome.DUPLICATE,
    ).count()


def _mark_failed(run_id, lease_token, exc):
    code = getattr(exc, "code", "vault_ingest_failed")
    with transaction.atomic():
        run = (
            VaultIngestRun.objects.select_for_update()
            .select_related("collection_task__request")
            .get(pk=run_id)
        )
        if run.lease_token != lease_token:
            return
        _refresh_counts(run)
        run.state = VaultIngestRun.State.FAILED
        run.lease_token = None
        run.heartbeat_at = timezone.now()
        run.last_error_code = code
        run.last_error_detail = safe_error_detail(exc)
        run.save(update_fields=[
            "state",
            "lease_token",
            "heartbeat_at",
            "last_error_code",
            "last_error_detail",
            "files_processed",
            "messages_seen",
            "items_created",
            "duplicates_skipped",
            "updated_at",
        ])
        audit(
            "vault.ingest.failed",
            target=run.collection_task.request.number,
            collection_task_id=run.collection_task_id,
            attempt=run.attempt_count,
            error_code=code,
        )


@transaction.atomic
def _mark_complete(run_id, lease_token):
    run = (
        VaultIngestRun.objects.select_for_update()
        .select_related("collection_task__request")
        .get(pk=run_id)
    )
    if run.lease_token != lease_token:
        raise VaultIngestError(
            "ingest_lease_lost",
            "The Vault ingest lease was lost before completion.",
        )
    _refresh_counts(run)
    now = timezone.now()
    run.state = VaultIngestRun.State.COMPLETE
    run.lease_token = None
    run.heartbeat_at = now
    run.completed_at = now
    run.last_error_code = ""
    run.last_error_detail = ""
    run.save(update_fields=[
        "state",
        "lease_token",
        "heartbeat_at",
        "completed_at",
        "last_error_code",
        "last_error_detail",
        "files_processed",
        "messages_seen",
        "items_created",
        "duplicates_skipped",
        "updated_at",
    ])
    task = run.collection_task
    task.state = CollectionTask.State.COMPLETE
    task.completed_at = now
    task.save(update_fields=["state", "completed_at", "updated_at"])
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("collections", task.pk)
    StatusEvent.objects.create(
        request=task.request,
        note=(
            f"Vault collection completed: {run.messages_seen} message(s) "
            f"processed, {run.items_created} review item(s) created, and "
            f"{run.duplicates_skipped} duplicate(s) retained as evidence."
        ),
    )
    audit(
        "vault.ingest.completed",
        target=task.request.number,
        collection_task_id=task.pk,
        files_processed=run.files_processed,
        messages_seen=run.messages_seen,
        items_created=run.items_created,
        duplicates_skipped=run.duplicates_skipped,
    )
    audit(
        "collection.export_complete",
        target=task.request.number,
        collection_task_id=task.pk,
        export_kind=task.kind,
        files_processed=run.files_processed,
        messages_seen=run.messages_seen,
        items_created=run.items_created,
        duplicates_skipped=run.duplicates_skipped,
    )
    return run


def _export_object_for(run, descriptor):
    """The retained-object row for one manifest entry, identity-checked."""
    identity = _identity_sha256(
        descriptor["bucket_name"],
        descriptor["object_name"],
    )
    source_object, created = VaultExportObject.objects.get_or_create(
        ingest_run=run,
        object_identity_sha256=identity,
        defaults={
            "bucket_name": descriptor["bucket_name"],
            "object_name": descriptor["object_name"],
            "expected_size": descriptor["size"],
        },
    )
    if not created and (
        source_object.bucket_name != descriptor["bucket_name"]
        or source_object.object_name != descriptor["object_name"]
        or source_object.expected_size != descriptor["size"]
    ):
        raise VaultIngestPermanentError(
            "export_manifest_changed",
            "The Vault export manifest changed between ingest attempts.",
        )
    return source_object


def _mark_export_object_failed(source_object, exc):
    """Record why one export object could not be retained or processed."""
    source_object.state = VaultExportObject.State.FAILED
    source_object.last_error_code = getattr(
        exc,
        "code",
        "export_processing_failed",
    )
    source_object.last_error_detail = safe_error_detail(exc)
    source_object.save(update_fields=[
        "state",
        "last_error_code",
        "last_error_detail",
    ])


def _record_custodian_metadata(run, summary):
    """Say on the run what the export's metadata CSV did and did not give.

    Kept on the run's own export snapshot rather than in ``last_error_*``:
    a metadata CSV that is absent or unreadable is not an ingest failure, and
    recording it as one would make an otherwise complete export look broken.
    """
    snapshot = dict(run.export_snapshot or {})
    snapshot["custodian_metadata"] = summary
    run.export_snapshot = snapshot
    run.save(update_fields=["export_snapshot", "updated_at"])


def _record_delivered_to_missing(run, missing, *, csv_sets_attached=0):
    """Say on the run how many messages named no mailbox of their own.

    Merged into the same ``custodian_metadata`` snapshot the CSV writes, so
    one place on the run answers "what custodial evidence did this export
    give?". Counted over the messages *this pass* processed: a resumed run
    skips the artifacts an earlier pass already completed, and inventing a
    total for messages this pass never read would be a number about nothing.
    """
    snapshot = dict(run.export_snapshot or {})
    summary = dict(snapshot.get("custodian_metadata") or {})
    summary["delivered_to_missing"] = missing
    summary["csv_sets_attached"] = csv_sets_attached
    snapshot["custodian_metadata"] = summary
    run.export_snapshot = snapshot
    run.save(update_fields=["export_snapshot", "updated_at"])


def _load_custodian_metadata(run, task, files, *, download, client):
    """Retain and parse the export's metadata CSV before any message is read.

    Google writes one row per *copy* of every exported message, naming the
    account whose mailbox held it. Parsing it first is what lets every message
    below -- created or deduplicated -- name every mailbox it was actually
    found in, even when the collection task itself carries no custodian list.

    Absent, unreadable or unparseable: this degrades to the behaviour that
    existed before the CSV was read at all, says so on the run, and never
    fails the export. A download failure is left to the ordinary object loop,
    which is where a real retention problem belongs.
    """
    # The metadata objects are deliberately left in the main loop's hands
    # afterwards, which retains them again and marks them COMPLETE. The second
    # pass costs one object-store read -- _retained_export_file finds the
    # storage key already set and never re-downloads from Vault -- and in
    # exchange every export object's state transitions in exactly one place.
    descriptors = [
        descriptor
        for descriptor in files
        if is_metadata_csv_object(descriptor["object_name"])
    ]
    if not descriptors:
        _record_custodian_metadata(run, {"status": "absent", "objects": 0})
        return None

    combined = MetadataCsv()
    unreadable = []
    # Read once: every metadata object in this run is read against the same
    # ceiling, and a store read per object would be a cache round trip per
    # object.
    object_limit = runtime.get("PRR_VAULT_EXPORT_MAX_BYTES")
    for descriptor in descriptors:
        source_object = _export_object_for(run, descriptor)
        # Retention is not parsing. A download that times out, a credential
        # that expired, a retained object that fails its digest -- these are
        # problems with the *object*, identical to the ones the main loop
        # marks failed and re-raises so the run's ordinary retry picks them up
        # on the next attempt. Swallowing them here would permanently degrade
        # an otherwise healthy run to "no custodian evidence" on a transient
        # network blip, which is the very failure this module exists to fix.
        try:
            retained = _retained_export_file(
                source_object,
                task,
                download=download,
                client=client,
            )
        except Exception as exc:
            _mark_export_object_failed(source_object, exc)
            raise
        # From here the bytes are in hand and verified. What is left is what
        # the CSV *says*, and a file this module cannot make sense of degrades
        # to the behaviour that existed before it was read at all.
        try:
            try:
                parsed = read_metadata_csv(
                    retained,
                    max_bytes=object_limit,
                )
            finally:
                retained.close()
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            unreadable.append({
                "object_name": descriptor["object_name"][:1000],
                "detail": safe_error_detail(exc),
            })
            continue
        combined = combined.merged_with(parsed)

    # Counted, never listed: a run can say how many mailboxes it declined to
    # attach as custodians without naming one. Summed per message because one
    # account may hold several messages and each is its own declined copy.
    declined_total = sum(
        custodian_accounts(found_in)[1]
        for found_in in combined.accounts.values()
    )
    summary = {
        "status": "parsed" if combined.accounts else "empty",
        "objects": len(descriptors),
        "rows": combined.rows,
        "message_ids": combined.message_ids,
        "blank_message_id_rows": combined.blank_message_id_rows,
        # Rows that named a message but not the mailbox that held it: the CSV
        # knew of the copy and could not say where it was.
        "blank_account_rows": combined.blank_account_rows,
        "accounts_outside_domains": declined_total,
    }
    if unreadable:
        summary["status"] = "parsed" if combined.accounts else "unreadable"
        summary["unreadable"] = unreadable
    _record_custodian_metadata(run, summary)
    if not combined.accounts:
        return None
    return combined


def ingest_completed_export(
    collection_task_id,
    *,
    export=None,
    _download=None,
    _client=None,
    _convert=None,
    _scan=None,
    _cleanup_quarantine=None,
):
    """Idempotently retain and ingest one completed Vault mail export."""
    completed = _existing_completed_run(collection_task_id)
    if completed is not None:
        return completed
    task = CollectionTask.objects.select_related("request").get(
        pk=collection_task_id,
    )
    if task.kind != CollectionTask.Kind.VAULT_MAIL:
        raise VaultIngestPermanentError(
            "collection_kind_unsupported",
            "This ingest pipeline currently requires a Vault mail task.",
        )
    if export is None:
        from apps.integrations.vault import client as vault

        export = vault.get_export(task.vault_matter_id, task.vault_export_id)
    files = _export_files(export)
    run, lease_token = _claim_run(task, export, files)
    if lease_token is None:
        return run

    try:
        if export.get("status") != "COMPLETED":
            raise VaultIngestPermanentError(
                "export_not_completed",
                "The Vault export is not completed and cannot be ingested.",
            )
        export_id = str(export.get("id", ""))
        if export_id and export_id != task.vault_export_id:
            raise VaultIngestPermanentError(
                "export_identity_mismatch",
                "The Vault export identity does not match the collection task.",
            )
        total_size = sum(value["size"] for value in files)
        if total_size > runtime.get("PRR_VAULT_EXPORT_TOTAL_MAX_BYTES"):
            raise VaultIngestPermanentError(
                "export_too_large",
                "The Vault export exceeds the configured total byte limit.",
            )
        download = _default_download if _download is None else _download
        convert = convert_message_to_searchable_pdf if _convert is None else _convert
        scan = _scan_vault_message if _scan is None else _scan
        cleanup_quarantine = (
            _cleanup_vault_message_quarantine
            if _cleanup_quarantine is None
            else _cleanup_quarantine
        )
        csv_metadata = _load_custodian_metadata(
            run,
            task,
            files,
            download=download,
            client=_client,
        )
        delivered_to_missing = 0
        for descriptor in files:
            _heartbeat(run, lease_token)
            source_object = _export_object_for(run, descriptor)
            if source_object.state == VaultExportObject.State.COMPLETE:
                continue
            try:
                retained = _retained_export_file(
                    source_object,
                    task,
                    download=download,
                    client=_client,
                )
                try:
                    for ordinal, raw_message in enumerate(
                        _iter_export_messages(
                            retained,
                            source_object.object_name,
                        ),
                        start=1,
                    ):
                        delivered_to_missing += _process_message(
                            source_object,
                            ordinal,
                            raw_message,
                            task,
                            client=_client,
                            convert=convert,
                            scan=scan,
                            cleanup_quarantine=cleanup_quarantine,
                            csv_metadata=csv_metadata,
                        )
                        _heartbeat(run, lease_token)
                finally:
                    retained.close()
                source_object.state = VaultExportObject.State.COMPLETE
                source_object.processed_at = timezone.now()
                source_object.last_error_code = ""
                source_object.last_error_detail = ""
                source_object.save(update_fields=[
                    "state",
                    "processed_at",
                    "last_error_code",
                    "last_error_detail",
                ])
            except Exception as exc:
                _mark_export_object_failed(source_object, exc)
                raise
        _record_delivered_to_missing(
            run,
            delivered_to_missing,
            csv_sets_attached=attach_csv_sets_to_sole_records(
                run, task, csv_metadata,
            ),
        )
        return _mark_complete(run.pk, lease_token)
    except Exception as exc:
        _mark_failed(run.pk, lease_token, exc)
        raise
