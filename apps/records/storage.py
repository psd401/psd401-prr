"""Immutable, integrity-checked storage for responsive records and productions."""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from pathlib import PurePath
from typing import BinaryIO

from django.conf import settings

from apps.configuration import runtime
from apps.secure_files import _object_store_client

READ_CHUNK_SIZE = 1024 * 1024
_ALLOWED_PREFIXES = (
    "records/",
    "productions/",
    "exemption-logs/",
    "vault-exports/",
    "vault-messages/",
    "derivatives/",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


class ObjectIntegrityError(RuntimeError):
    """Stored or supplied bytes do not match their chain-of-custody facts."""


def generated_record_storage_key(request_id, *, original_filename: str = "") -> str:
    """Create an opaque record key without placing display names in storage."""
    request_uuid = uuid.UUID(str(request_id))
    suffix = PurePath(str(original_filename or "")).suffix.lower()
    if not _SUFFIX_RE.fullmatch(suffix):
        suffix = ".bin"
    return f"records/{request_uuid}/{uuid.uuid4()}{suffix}"


def generated_derivative_storage_key(request_id, record_item_id) -> str:
    """Key for the reviewable rendition of a record that is not a PDF.

    Kept under its own prefix, never overwriting the original: the original is
    the record and the chain of custody, and the derivative is only how a
    human reads it.
    """
    return (
        f"derivatives/{uuid.UUID(str(request_id))}/"
        f"{uuid.UUID(str(record_item_id))}/{uuid.uuid4()}.pdf"
    )


def generated_production_storage_key(request_id, installment_id) -> str:
    request_uuid = uuid.UUID(str(request_id))
    return (
        f"productions/{request_uuid}/{int(installment_id)}/"
        f"{uuid.uuid4()}.zip"
    )


def generated_exemption_log_storage_key(request_id, installment_id) -> str:
    request_uuid = uuid.UUID(str(request_id))
    return (
        f"exemption-logs/{request_uuid}/{int(installment_id)}/"
        f"{uuid.uuid4()}.pdf"
    )


def generated_vault_export_storage_key(
    request_id,
    collection_task_id,
    *,
    object_name: str = "",
) -> str:
    request_uuid = uuid.UUID(str(request_id))
    suffix = PurePath(str(object_name or "")).suffix.lower()
    if not _SUFFIX_RE.fullmatch(suffix):
        suffix = ".bin"
    return (
        f"vault-exports/{request_uuid}/{int(collection_task_id)}/"
        f"{uuid.uuid4()}{suffix}"
    )


def generated_vault_message_storage_key(request_id, collection_task_id) -> str:
    request_uuid = uuid.UUID(str(request_id))
    return (
        f"vault-messages/{request_uuid}/{int(collection_task_id)}/"
        f"{uuid.uuid4()}.eml"
    )


def _validate_generated_key(storage_key: str) -> None:
    if (
        not isinstance(storage_key, str)
        or not storage_key.startswith(_ALLOWED_PREFIXES)
        or storage_key.startswith("/")
        or ".." in storage_key.split("/")
        or "//" in storage_key
        or any(ord(character) < 32 for character in storage_key)
    ):
        raise ValueError("A generated records key is required.")


def _validate_integrity_facts(*, content_length: int, sha256: str) -> None:
    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length < 0
        or not isinstance(sha256, str)
        or not _SHA256_RE.fullmatch(sha256)
    ):
        raise ValueError("Object integrity facts are invalid.")


def _digest_source(source: BinaryIO) -> tuple[int, str]:
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(READ_CHUNK_SIZE):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def store_immutable_object(
    storage_key: str,
    source: BinaryIO,
    *,
    content_length: int,
    sha256: str,
    content_type: str,
    _client=None,
) -> None:
    """Verify and create one retained object without an overwrite path."""
    _validate_generated_key(storage_key)
    _validate_integrity_facts(content_length=content_length, sha256=sha256)
    normalized_content_type = str(content_type or "application/octet-stream")
    if "\r" in normalized_content_type or "\n" in normalized_content_type:
        raise ValueError("Object content type is invalid.")

    original_position = source.tell()
    try:
        actual_size, actual_sha256 = _digest_source(source)
        if actual_size != content_length or actual_sha256 != sha256:
            raise ObjectIntegrityError(
                "The source bytes do not match their integrity facts."
            )
        source.seek(0)
        client = _object_store_client() if _client is None else _client
        client.put_object(
            Bucket=settings.OBJECT_STORE["bucket"],
            Key=storage_key,
            Body=source,
            ContentLength=content_length,
            ContentType=normalized_content_type,
            Metadata={
                "sha256": sha256,
                "immutable": "true",
            },
            IfNoneMatch="*",
        )
    finally:
        source.seek(original_position)


def load_verified_object(
    storage_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    _client=None,
):
    """Read to a spool and verify all bytes before they reach a consumer."""
    _validate_generated_key(storage_key)
    _validate_integrity_facts(
        content_length=expected_size,
        sha256=expected_sha256,
    )
    client = _object_store_client() if _client is None else _client
    response = client.get_object(
        Bucket=settings.OBJECT_STORE["bucket"],
        Key=storage_key,
    )
    body = response["Body"]
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - the spool is handed to the caller
        max_size=runtime.get("PRR_VERIFIED_OBJECT_MEMORY_BYTES"),
        mode="w+b",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        chunks = (
            body.iter_chunks(chunk_size=READ_CHUNK_SIZE)
            if hasattr(body, "iter_chunks")
            else iter(lambda: body.read(READ_CHUNK_SIZE), b"")
        )
        for chunk in chunks:
            if not chunk:
                continue
            size += len(chunk)
            if size > expected_size:
                raise ObjectIntegrityError(
                    "The stored object failed integrity verification."
                )
            digest.update(chunk)
            spool.write(chunk)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise ObjectIntegrityError(
                "The stored object failed integrity verification."
            )
        spool.seek(0)
        return spool
    except Exception:
        spool.close()
        raise
    finally:
        body.close()
