"""Validation, quarantine storage, malware scanning, and clean downloads.

Requester uploads are hostile until proven otherwise. This module never uses a
requester filename as an object key, stores all bytes as octet-stream content,
and only creates download URLs after ClamAV has promoted an object out of the
private quarantine prefix.
"""

import hashlib
import os
import struct
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from django.conf import settings

from apps import secure_files
from apps.configuration import runtime

from .models import IntakeAttachment
from .tasks import scan_intake_attachment

CLAMAV_CHUNK_SIZE = secure_files.CLAMAV_CHUNK_SIZE
CONTENT_SIGNATURE_BYTES = 8192

_OLE_COMPOUND_FILE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_CONTENT_TYPES = {
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
}
_OOXML_MAIN_PARTS = {
    (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml"
    ): (
        (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "ppt/presentation.xml",
    ),
    (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml"
    ): (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xl/workbook.xml",
    ),
    (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ): (
        (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "word/document.xml",
    ),
}
_OOXML_CONTENT_TYPES = frozenset(
    package_type
    for package_type, _main_part in _OOXML_MAIN_PARTS.values()
)
_OOXML_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_MAX_OOXML_ENTRIES = 10_000
_MAX_OOXML_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024
_MAX_OOXML_CONTENT_TYPES_BYTES = 1024 * 1024
_MAX_OOXML_COMPRESSION_RATIO = 100


class AttachmentValidationError(ValueError):
    pass


AttachmentScanUnavailable = secure_files.AttachmentScanUnavailable


def _has_disallowed_filename_characters(filename):
    return any(unicodedata.category(character).startswith("C") for character in filename)


def _looks_like_text(prefix):
    if b"\x00" in prefix:
        return False
    if any(byte < 32 and byte not in {9, 10, 13} for byte in prefix):
        return False
    try:
        prefix.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Windows-1252 remains common in exported district text/CSV files.
        try:
            prefix.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def _looks_like_email(prefix):
    if not _looks_like_text(prefix):
        return False
    normalized = prefix.replace(b"\r\n", b"\n")
    header_block, separator, _ = normalized.partition(b"\n\n")
    if not separator:
        return False
    headers = []
    for line in header_block.splitlines():
        if line.startswith((b" ", b"\t")):
            continue
        name, delimiter, _ = line.partition(b":")
        if delimiter and name and all(
            character == 45
            or 48 <= character <= 57
            or 65 <= character <= 90
            or 97 <= character <= 122
            for character in name
        ):
            headers.append(name.lower())
    return bool(headers)


def _zip_eocd_metadata(upload):
    size = int(getattr(upload, "size", 0) or 0)
    if size < 22:
        return None
    tail_size = min(size, 22 + 65_535)
    upload.seek(size - tail_size)
    tail = upload.read(tail_size)
    position = tail.rfind(b"PK\x05\x06")
    while position >= 0:
        if position + 22 <= len(tail):
            (
                disk_number,
                directory_disk,
                disk_entries,
                total_entries,
                directory_size,
                directory_offset,
                comment_length,
            ) = struct.unpack_from("<4H2LH", tail, position + 4)
            absolute_position = size - tail_size + position
            if (
                disk_number == 0
                and directory_disk == 0
                and disk_entries == total_entries
                and absolute_position + 22 + comment_length == size
                and directory_offset + directory_size == absolute_position
            ):
                return (
                    total_entries,
                    directory_offset,
                    directory_size,
                )
        position = tail.rfind(b"PK\x05\x06", 0, position)
    return None


def _central_directory_matches_eocd(
    upload,
    *,
    entry_count,
    offset,
    size,
):
    if size > _MAX_OOXML_CENTRAL_DIRECTORY_BYTES:
        return False
    upload.seek(offset)
    directory = upload.read(size)
    if len(directory) != size:
        return False
    cursor = 0
    for _index in range(entry_count):
        if (
            cursor + 46 > len(directory)
            or directory[cursor : cursor + 4] != b"PK\x01\x02"
        ):
            return False
        filename_length, extra_length, comment_length = struct.unpack_from(
            "<3H",
            directory,
            cursor + 28,
        )
        if struct.unpack_from("<H", directory, cursor + 34)[0] != 0:
            return False
        cursor += (
            46
            + filename_length
            + extra_length
            + comment_length
        )
        if cursor > len(directory):
            return False
    return cursor == len(directory)


def _ooxml_content_type(upload):
    metadata = _zip_eocd_metadata(upload)
    if metadata is None:
        upload.seek(0)
        return None
    entry_count, directory_offset, directory_size = metadata
    if (
        entry_count > _MAX_OOXML_ENTRIES
        or not _central_directory_matches_eocd(
            upload,
            entry_count=entry_count,
            offset=directory_offset,
            size=directory_size,
        )
    ):
        upload.seek(0)
        return None
    try:
        upload.seek(0)
        with zipfile.ZipFile(upload) as archive:
            entries = archive.infolist()
            if len(entries) != entry_count:
                return None
            names = {entry.filename.lstrip("/") for entry in entries}
            if len(names) != len(entries):
                return None
            # Both limits are read once, outside the loop: this walks up to
            # ten thousand entries of a hostile archive, and a settings read
            # per entry would be a cache round trip per entry.
            entry_limit = runtime.get("PRR_INTAKE_ATTACHMENT_MAX_BYTES")
            expanded_limit = runtime.get(
                "PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES"
            )
            expanded_total = 0
            for entry in entries:
                if entry.flag_bits & 0x1:
                    return None
                if entry.is_dir():
                    if entry.file_size or entry.compress_size:
                        return None
                    continue
                if entry.file_size > entry_limit:
                    return None
                expanded_total += entry.file_size
                if expanded_total > expanded_limit:
                    return None
                if (
                    entry.file_size
                    and (
                        entry.compress_size == 0
                        or entry.file_size
                        > entry.compress_size
                        * _MAX_OOXML_COMPRESSION_RATIO
                    )
                ):
                    return None
            try:
                manifest_info = archive.getinfo("[Content_Types].xml")
            except KeyError:
                return None
            if (
                manifest_info.file_size
                > _MAX_OOXML_CONTENT_TYPES_BYTES
            ):
                return None
            with archive.open(manifest_info) as manifest_file:
                manifest = manifest_file.read(
                    _MAX_OOXML_CONTENT_TYPES_BYTES + 1
                )
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return None
    finally:
        upload.seek(0)

    if (
        len(manifest) > _MAX_OOXML_CONTENT_TYPES_BYTES
        or b"<!doctype" in manifest.lower()
        or b"<!entity" in manifest.lower()
    ):
        return None
    try:
        root = ElementTree.fromstring(
            manifest,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (
        DefusedXmlException,
        ElementTree.ParseError,
        LookupError,
        RecursionError,
        ValueError,
    ):
        return None
    if root.tag != f"{{{_OOXML_CONTENT_TYPES_NAMESPACE}}}Types":
        return None

    detected = set()
    for element in root.iter():
        if (
            element.tag
            != f"{{{_OOXML_CONTENT_TYPES_NAMESPACE}}}Override"
        ):
            continue
        main_part = _OOXML_MAIN_PARTS.get(
            element.get("ContentType", "")
        )
        if main_part is None:
            continue
        package_type, required_name = main_part
        if (
            element.get("PartName", "").lstrip("/") == required_name
            and required_name in names
        ):
            detected.add(package_type)
    if len(detected) != 1:
        return None
    return detected.pop()


def _content_matches_declared_type(upload, content_type):
    upload.seek(0)
    prefix = upload.read(CONTENT_SIGNATURE_BYTES)
    upload.seek(0)

    if content_type == "application/pdf":
        return prefix.startswith(b"%PDF-")
    if content_type == "application/rtf":
        return prefix.startswith(b"{\\rtf")
    if content_type in _OLE_CONTENT_TYPES:
        return prefix.startswith(_OLE_COMPOUND_FILE_SIGNATURE)
    if content_type in _OOXML_CONTENT_TYPES:
        return (
            prefix.startswith(b"PK\x03\x04")
            and _ooxml_content_type(upload) == content_type
        )
    if content_type == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/tiff":
        return prefix.startswith((b"II*\x00", b"MM\x00*"))
    if content_type == "message/rfc822":
        return _looks_like_email(prefix)
    if content_type in {"text/csv", "text/plain"}:
        return _looks_like_text(prefix)
    return False


@dataclass
class ValidatedAttachment:
    upload: object
    original_filename: str
    content_type: str
    size: int
    sha256: str

    def digest_descriptor(self):
        return {
            "filename": self.original_filename,
            "content_type": self.content_type,
            "size": self.size,
            "sha256": self.sha256,
        }


def validate_attachments(files):
    files = list(files)
    maximum_count = runtime.get("PRR_INTAKE_ATTACHMENT_MAX_COUNT")
    if len(files) > maximum_count:
        raise AttachmentValidationError(
            f"A maximum of {maximum_count} attachments is allowed."
        )

    # Read once, before the loop: every file in one submission is judged
    # against the same limits, whatever an administrator does mid-upload.
    file_limit = runtime.get("PRR_INTAKE_ATTACHMENT_MAX_BYTES")
    combined_limit = runtime.get("PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES")
    validated = []
    total_size = 0
    for upload in files:
        filename = Path(os.path.basename(upload.name or "")).name
        if (
            not filename
            or _has_disallowed_filename_characters(filename)
            or len(filename) > 255
        ):
            raise AttachmentValidationError(
                "Every attachment must have a safe filename of at most 255 characters."
            )

        size = int(getattr(upload, "size", 0) or 0)
        if size < 1:
            raise AttachmentValidationError(f"Attachment {filename!r} is empty.")
        if size > file_limit:
            raise AttachmentValidationError(
                f"Attachment {filename!r} exceeds the per-file size limit."
            )
        total_size += size
        if total_size > combined_limit:
            raise AttachmentValidationError("The combined attachment size limit was exceeded.")

        content_type = (getattr(upload, "content_type", "") or "").lower().strip()
        if content_type not in settings.PRR_INTAKE_ATTACHMENT_ALLOWED_CONTENT_TYPES:
            raise AttachmentValidationError(
                f"Attachment {filename!r} has an unsupported file type."
            )
        if not _content_matches_declared_type(upload, content_type):
            raise AttachmentValidationError(
                f"Attachment {filename!r} content does not match its declared file type."
            )

        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        validated.append(
            ValidatedAttachment(
                upload=upload,
                original_filename=filename,
                content_type=content_type,
                size=size,
                sha256=digest.hexdigest(),
            )
        )

    return validated


def attachment_digest_descriptors(attachments):
    return sorted(
        (attachment.digest_descriptor() for attachment in attachments),
        key=lambda row: (
            row["sha256"], row["filename"], row["content_type"], row["size"]
        ),
    )


def persist_intake_attachments(prr, attachments):
    persisted = []
    for validated in attachments:
        attachment = IntakeAttachment.objects.create(
            request=prr,
            original_filename=validated.original_filename,
            content_type=validated.content_type,
            size=validated.size,
            sha256=validated.sha256,
            storage_key="pending",
        )
        attachment.storage_key = (
            f"quarantine/intake/{prr.id}/{attachment.id}"
        )
        attachment.save(update_fields=["storage_key"])
        try:
            store_quarantined_attachment(attachment, validated.upload)
            scan_intake_attachment.delay(str(attachment.id))
        except Exception:
            attachment.scan_status = IntakeAttachment.ScanStatus.ERROR
            attachment.scan_detail = (
                "The attachment could not be stored and queued for malware scanning."
            )
            attachment.save(update_fields=["scan_status", "scan_detail"])
            raise
        persisted.append(attachment)
    return persisted


def _object_store_client():
    """Compatibility seam retained for intake attachment tests/callers."""
    return secure_files._object_store_client()


def store_quarantined_attachment(attachment, upload):
    secure_files.store_quarantined_object(
        attachment.storage_key,
        upload,
        content_length=attachment.size,
        sha256=attachment.sha256,
        _client=_object_store_client(),
    )


def _clamav_scan(chunks):
    """Compatibility seam retained for intake attachment tests/callers."""
    return secure_files._clamav_scan(chunks)


def scan_object_for_malware(storage_key, *, expected_size, expected_sha256):
    return secure_files.scan_object_for_malware(
        storage_key,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        max_bytes=runtime.get("PRR_INTAKE_ATTACHMENT_MAX_BYTES"),
        _client=_object_store_client(),
        _scanner=_clamav_scan,
    )


def clean_storage_key(attachment):
    return f"intake/{attachment.request_id}/{attachment.id}"


def promote_clean_attachment(source_key, destination_key):
    secure_files.promote_clean_object(
        source_key,
        destination_key,
        _client=_object_store_client(),
    )


def delete_quarantined_attachment(storage_key):
    secure_files.delete_quarantined_object(
        storage_key,
        _client=_object_store_client(),
    )


def open_clean_attachment(attachment):
    if not attachment.is_downloadable:
        raise ValueError("Only malware-scanned attachments can be downloaded.")
    return secure_files.open_private_object(
        getattr(attachment, "effective_storage_key", attachment.storage_key),
        _client=_object_store_client(),
    )
