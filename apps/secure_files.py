"""Private object-storage and malware-scanning primitives.

This module is deliberately domain-neutral. Callers supply generated object
keys, expected integrity metadata, and their own byte limit; display filenames
never cross this boundary.
"""

from __future__ import annotations

import hashlib
import socket
import struct
from collections.abc import Callable, Iterable, Iterator
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.configuration import runtime

CLAMAV_CHUNK_SIZE = 1024 * 1024
_QUARANTINE_KEY_PREFIXES = (
    "quarantine/intake/",
    "quarantine/correspondence/raw/",
    "quarantine/correspondence/attachments/",
    "quarantine/vault-messages/",
    "quarantine/legacy-files/",
    "quarantine/record-uploads/",
)


class AttachmentScanUnavailable(RuntimeError):
    """The attachment cannot safely be classified as clean."""


def _object_store_client_kwargs(config, *, client_config: Config) -> dict:
    endpoint = config.get("endpoint", "")
    access_key = config.get("access_key", "")
    secret_key = config.get("secret_key", "")
    if not config.get("bucket"):
        raise ImproperlyConfigured(
            "An object storage bucket is required for attachments."
        )
    if bool(access_key) != bool(secret_key) or (endpoint and not access_key):
        raise ImproperlyConfigured(
            "Object storage credentials must be a complete key pair; custom "
            "endpoints require that pair."
        )

    kwargs = {
        "service_name": "s3",
        "config": client_config,
    }
    if access_key:
        kwargs.update(
            {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
            }
        )
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if config.get("region"):
        kwargs["region_name"] = config["region"]
    return kwargs


class _FilesystemObjectStore:
    """A local stand-in for the private bucket, for browser acceptance runs.

    Only ``config.e2e_settings`` sets ``PRR_E2E_FILESYSTEM_OBJECT_STORE``, and
    it is honoured only while the test-fixture switch is on as well, so no
    deployed environment can reach it. It implements exactly the two calls
    the application makes, with the same no-overwrite rule as the bucket.
    """

    def __init__(self, root: str):
        from pathlib import Path

        self.root = Path(root)

    def _path(self, bucket: str, key: str):
        path = (self.root / bucket / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("Object key escapes the local store.")
        return path

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, **_unused):
        path = self._path(Bucket, Key)
        if IfNoneMatch == "*" and path.exists():
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Object exists."}},
                "PutObject",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Body.read())
        return {}

    def _existing(self, bucket: str, key: str, operation: str):
        path = self._path(bucket, key)
        if not path.exists():
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "No such key."}},
                operation,
            )
        return path

    def get_object(self, *, Bucket, Key, **_unused):
        import io

        payload = self._existing(Bucket, Key, "GetObject").read_bytes()
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}

    def head_object(self, *, Bucket, Key, **_unused):
        return {"ContentLength": self._existing(Bucket, Key, "HeadObject").stat().st_size}

    def copy_object(self, *, Bucket, Key, CopySource, **_unused):
        payload = self._existing(CopySource["Bucket"], CopySource["Key"], "CopyObject").read_bytes()
        path = self._path(Bucket, Key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {}

    def delete_object(self, *, Bucket, Key, **_unused):
        self._path(Bucket, Key).unlink(missing_ok=True)
        return {}

    def get_bucket_versioning(self, *, Bucket):
        # A plain directory keeps no versions.
        return {}


def _object_store_client():
    local_root = getattr(settings, "PRR_E2E_FILESYSTEM_OBJECT_STORE", "")
    if local_root and getattr(settings, "PRR_TEST_FIXTURE_SEED_ENABLED", False):
        return _FilesystemObjectStore(local_root)
    config = settings.OBJECT_STORE
    kwargs = _object_store_client_kwargs(
        config,
        client_config=Config(
            connect_timeout=runtime.get("PRR_OBJECT_STORE_CONNECT_TIMEOUT_SECONDS"),
            read_timeout=runtime.get("PRR_OBJECT_STORE_READ_TIMEOUT_SECONDS"),
            retries={
                "mode": "standard",
                "total_max_attempts": runtime.get("PRR_OBJECT_STORE_MAX_ATTEMPTS"),
            },
        ),
    )
    return boto3.client(**kwargs)


def probe_object_storage(*, _client=None) -> bool:
    """Perform one bounded, read-only request against the private bucket."""
    if _client is None:
        config = settings.OBJECT_STORE
        kwargs = _object_store_client_kwargs(
            config,
            client_config=Config(
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 0},
            ),
        )
        client = boto3.client(**kwargs)
    else:
        client = _client
    client.get_bucket_versioning(Bucket=settings.OBJECT_STORE["bucket"])
    return True


def probe_clamav(*, _connection_factory=socket.create_connection) -> bool:
    """Perform a bounded ClamAV PING and require the exact PONG response."""
    if not runtime.get("CLAMAV_HOST"):
        raise AttachmentScanUnavailable("ClamAV is not configured.")
    timeout = min(runtime.get("CLAMAV_TIMEOUT_SECONDS"), 5)
    try:
        with _connection_factory(
            (runtime.get("CLAMAV_HOST"), runtime.get("CLAMAV_PORT")),
            timeout=timeout,
        ) as connection:
            connection.sendall(b"zPING\0")
            response = bytearray()
            while b"\0" not in response and len(response) < 16:
                part = connection.recv(16 - len(response))
                if not part:
                    break
                response.extend(part)
    except OSError as exc:
        raise AttachmentScanUnavailable(
            "ClamAV readiness check failed."
        ) from exc
    if bytes(response) != b"PONG\0":
        raise AttachmentScanUnavailable("ClamAV readiness check failed.")
    return True


def store_quarantined_object(
    storage_key: str,
    source: BinaryIO,
    *,
    content_length: int,
    sha256: str,
    _client=None,
) -> None:
    """Store untrusted bytes with one visible, bounded S3 PutObject."""
    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length < 0
    ):
        raise ValueError("Quarantine object content length is invalid.")
    client = _object_store_client() if _client is None else _client
    original_position = source.tell()
    try:
        source.seek(0, 2)
        if source.tell() != content_length:
            raise ValueError(
                "Quarantine object content length does not match its source."
            )
        source.seek(0)
        client.put_object(
            Bucket=settings.OBJECT_STORE["bucket"],
            Key=storage_key,
            Body=source,
            ContentLength=content_length,
            ContentType="application/octet-stream",
            Metadata={
                "sha256": sha256,
                "scan-status": "pending",
            },
        )
    finally:
        source.seek(original_position)


def _clamav_scan(chunks: Iterable[bytes]) -> tuple[bool, str]:
    if not runtime.get("CLAMAV_HOST"):
        raise AttachmentScanUnavailable("ClamAV is not configured.")
    try:
        with socket.create_connection(
            (runtime.get("CLAMAV_HOST"), runtime.get("CLAMAV_PORT")),
            timeout=runtime.get("CLAMAV_TIMEOUT_SECONDS"),
        ) as connection:
            connection.sendall(b"zINSTREAM\0")
            for chunk in chunks:
                if not chunk:
                    continue
                connection.sendall(struct.pack("!I", len(chunk)))
                connection.sendall(chunk)
            connection.sendall(struct.pack("!I", 0))
            response = bytearray()
            while b"\0" not in response and len(response) < 4096:
                part = connection.recv(4096)
                if not part:
                    break
                response.extend(part)
    except OSError as exc:
        raise AttachmentScanUnavailable("ClamAV could not be reached.") from exc

    result = bytes(response).split(b"\0", 1)[0].decode(
        "utf-8", errors="replace"
    )
    if result.endswith(" OK"):
        return True, "OK"
    if result.endswith(" FOUND"):
        detail = result.split(":", 1)[-1].strip()
        return False, detail[:500]
    raise AttachmentScanUnavailable("ClamAV returned an invalid scan response.")


def scan_object_for_malware(
    storage_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
    _client=None,
    _scanner: Callable[[Iterable[bytes]], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """Stream one private object through ClamAV and verify its identity.

    Integrity is checked even after a clean scanner verdict. This prevents an
    object that changed after intake from being promoted as the audited file.
    """
    if max_bytes < 1:
        raise AttachmentScanUnavailable("The attachment scan limit is invalid.")

    client = _object_store_client() if _client is None else _client
    scanner = _clamav_scan if _scanner is None else _scanner
    response = client.get_object(
        Bucket=settings.OBJECT_STORE["bucket"],
        Key=storage_key,
    )
    body = response["Body"]
    digest = hashlib.sha256()
    bytes_seen = 0

    def verified_chunks() -> Iterator[bytes]:
        nonlocal bytes_seen
        for chunk in body.iter_chunks(chunk_size=CLAMAV_CHUNK_SIZE):
            if not chunk:
                continue
            bytes_seen += len(chunk)
            if bytes_seen > max_bytes:
                raise AttachmentScanUnavailable(
                    "The quarantined object exceeds its limit."
                )
            digest.update(chunk)
            yield chunk

    try:
        result = scanner(verified_chunks())
        if (
            bytes_seen != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise AttachmentScanUnavailable(
                "The quarantined object failed its integrity check."
            )
        return result
    finally:
        body.close()


def copy_clean_object(
    source_key: str,
    destination_key: str,
    *,
    _client=None,
) -> None:
    """Copy a clean object with fresh metadata while retaining quarantine."""
    client = _object_store_client() if _client is None else _client
    bucket = settings.OBJECT_STORE["bucket"]
    client.copy_object(
        Bucket=bucket,
        Key=destination_key,
        CopySource={"Bucket": bucket, "Key": source_key},
        ContentType="application/octet-stream",
        Metadata={"scan-status": "clean"},
        MetadataDirective="REPLACE",
    )


def promote_clean_object(
    source_key: str,
    destination_key: str,
    *,
    _client=None,
) -> None:
    """Compatibility helper that copies clean bytes and removes quarantine."""
    client = _object_store_client() if _client is None else _client
    copy_clean_object(
        source_key,
        destination_key,
        _client=client,
    )
    delete_quarantined_object(source_key, _client=client)


def _validate_quarantine_key(storage_key: str) -> None:
    if not isinstance(storage_key, str) or not any(
        storage_key.startswith(prefix)
        for prefix in _QUARANTINE_KEY_PREFIXES
    ):
        raise ValueError(
            "Object deletion is restricted to generated quarantine keys."
        )


def _listed_object_versions(client, *, bucket: str, storage_key: str):
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=storage_key):
        for collection in ("Versions", "DeleteMarkers"):
            for item in page.get(collection, ()):
                if item.get("Key") == storage_key and item.get("VersionId"):
                    yield item["VersionId"]


def delete_quarantined_object(storage_key: str, *, _client=None) -> None:
    """Physically remove one generated quarantine object and verify absence."""
    _validate_quarantine_key(storage_key)
    client = _object_store_client() if _client is None else _client
    bucket = settings.OBJECT_STORE["bucket"]
    versioning = client.get_bucket_versioning(Bucket=bucket).get("Status")
    if versioning in {"Enabled", "Suspended"}:
        version_ids = list(
            _listed_object_versions(
                client,
                bucket=bucket,
                storage_key=storage_key,
            )
        )
        for version_id in version_ids:
            client.delete_object(
                Bucket=bucket,
                Key=storage_key,
                VersionId=version_id,
            )
        if list(
            _listed_object_versions(
                client,
                bucket=bucket,
                storage_key=storage_key,
            )
        ):
            raise AttachmentScanUnavailable(
                "The versioned quarantine object still exists."
            )
        return

    client.delete_object(Bucket=bucket, Key=storage_key)
    try:
        client.head_object(Bucket=bucket, Key=storage_key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return
        raise
    raise AttachmentScanUnavailable(
        "The quarantine object still exists after deletion."
    )


def open_private_object(storage_key: str, *, _client=None):
    client = _object_store_client() if _client is None else _client
    response = client.get_object(
        Bucket=settings.OBJECT_STORE["bucket"],
        Key=storage_key,
    )
    return response["Body"]
