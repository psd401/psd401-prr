# ABOUTME: The one path untrusted bytes take to become a RecordItem: private
# ABOUTME: quarantine, a ClamAV verdict, immutable storage, then the row.
"""Turn untrusted bytes into a candidate record, or refuse to.

Shared by every source that hands the application loose files it did not
collect itself -- the NextRequest file migration and a Records Officer's
upload onto a case. Vault ingest has its own message-shaped path.

The order is the whole point:

1. The bytes go to a generated key under a private ``quarantine/`` prefix.
2. ClamAV reads them back from there. ``scan_object_for_malware`` also checks
   the size and digest it streamed against what the caller computed, so a
   verdict is always about the exact bytes that will be retained.
3. Only a clean verdict copies the bytes to an immutable ``records/`` key.
4. The quarantine object is deleted whatever happened.
5. Only then is a ``RecordItem`` written, so no row ever points at bytes that
   were not scanned, and an infected file leaves nothing behind.

A scanner that cannot be reached raises ``AttachmentScanUnavailable`` out of
step 2; nothing is retained and the caller decides how to report it.
"""
from __future__ import annotations

import mimetypes

from apps import secure_files

from .models import RecordItem
from .storage import generated_record_storage_key, store_immutable_object


def guess_record_content_type(filename: str) -> str:
    """The media type a record is handled as, decided from its name.

    Never the type a browser or an archive declared: that is a claim by the
    sender, and ``handling.py`` routes review on this value.
    """
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def retain_scanned_record_bytes(
    request_id,
    source,
    *,
    quarantine_key: str,
    original_filename: str,
    size: int,
    sha256: str,
    content_type: str,
    max_bytes: int,
    client,
) -> tuple[str | None, str]:
    """Quarantine, scan, and retain one file's bytes.

    Returns ``(storage_key, detail)`` for clean bytes and ``(None, detail)``
    for infected ones, where ``detail`` is the scanner's verdict. Raises
    ``AttachmentScanUnavailable`` when no verdict could be had.
    """
    secure_files.store_quarantined_object(
        quarantine_key,
        source,
        content_length=size,
        sha256=sha256,
        _client=client,
    )
    try:
        clean, detail = secure_files.scan_object_for_malware(
            quarantine_key,
            expected_size=size,
            expected_sha256=sha256,
            max_bytes=max_bytes,
            _client=client,
        )
        if not clean:
            return None, detail
        storage_key = generated_record_storage_key(
            request_id, original_filename=original_filename
        )
        source.seek(0)
        store_immutable_object(
            storage_key,
            source,
            content_length=size,
            sha256=sha256,
            content_type=content_type,
            _client=client,
        )
    finally:
        secure_files.delete_quarantined_object(quarantine_key, _client=client)
    return storage_key, detail


def create_scanned_record_item(
    request,
    *,
    source_system: str,
    original_filename: str,
    storage_key: str,
    sha256: str,
    size: int,
    content_type: str,
) -> RecordItem:
    """Write the row for bytes ``retain_scanned_record_bytes`` retained."""
    record = RecordItem.objects.create(
        request=request,
        source_system=source_system,
        original_filename=original_filename[:500],
        storage_key=storage_key,
        sha256=sha256,
        content_length=size,
        content_type=content_type[:255],
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("records", record.pk)
    queue_record_derivative(record)
    return record


def queue_record_derivative(record):
    """Ask for a reviewable rendition when the record is not already a PDF.

    Queued rather than converted inline: a migration run stores hundreds of
    files and an upload may carry dozens, and LibreOffice is slow enough that
    doing it here would turn a transfer into a long job that fails as one unit.
    """
    from .handling import CONVERT, handling_for_item

    if handling_for_item(record) != CONVERT:
        return
    from .tasks import build_record_derivative

    record.derivative_state = RecordItem.DerivativeState.PENDING
    record.save(update_fields=["derivative_state"])
    build_record_derivative.delay(str(record.pk))
