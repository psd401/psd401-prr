#!/usr/bin/env python3
# ABOUTME: Creates and verifies app-backup integrity manifests without AWS access.
# ABOUTME: Also proves a restored database contains the exported audit boundary.
import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

MAX_PUT_OBJECT_BYTES = 5_000_000_000
ENVIRONMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILES = {
    "database.dump": "application/vnd.postgresql.custom",
    "audit.jsonl.gz": "application/gzip",
}
MANIFEST_FIELDS = {
    "audit_export",
    "backup_id",
    "bucket",
    "created_at",
    "environment",
    "files",
    "image_id",
    "s3_prefix",
    "schema_version",
}
AUDIT_EVIDENCE_FIELDS = {"max_id", "row_count"}
FILE_EVIDENCE_FIELDS = {"bytes", "content_type", "sha256"}


def _validate_image_id(image_id: object) -> str:
    if not isinstance(image_id, str) or IMAGE_ID_RE.fullmatch(image_id) is None:
        raise ValueError("application image ID must be a canonical SHA-256")
    return image_id


def _validate_audit_evidence(evidence: object) -> dict:
    if not isinstance(evidence, dict) or set(evidence) != AUDIT_EVIDENCE_FIELDS:
        raise ValueError("audit export boundary is invalid")
    row_count = evidence.get("row_count")
    max_id = evidence.get("max_id")
    if (
        type(row_count) is not int
        or type(max_id) is not int
        or row_count < 0
        or max_id < 0
        or (row_count == 0 and max_id != 0)
        or (row_count > 0 and max_id < row_count)
    ):
        raise ValueError("audit export boundary is invalid")
    return evidence


def _validate_file_evidence(files: object) -> dict:
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise ValueError("backup manifest has an unexpected artifact set")
    for name, content_type in FILES.items():
        evidence = files.get(name)
        if not isinstance(evidence, dict) or set(evidence) != FILE_EVIDENCE_FIELDS:
            raise ValueError(f"{name} evidence is invalid")
        size = evidence.get("bytes")
        checksum = evidence.get("sha256")
        if (
            type(size) is not int
            or size <= 0
            or size > MAX_PUT_OBJECT_BYTES
            or evidence.get("content_type") != content_type
            or not isinstance(checksum, str)
            or SHA256_RE.fullmatch(checksum) is None
        ):
            raise ValueError(f"{name} evidence is invalid")
    return files


def _validate_manifest_schema(
    manifest: object, *, allow_legacy_bare_image_id: bool = False
) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("backup manifest schema is invalid")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("unsupported backup manifest schema")
    image_id = manifest.get("image_id")
    if not (
        allow_legacy_bare_image_id
        and isinstance(image_id, str)
        and SHA256_RE.fullmatch(image_id) is not None
    ):
        _validate_image_id(image_id)
    _validate_audit_evidence(manifest.get("audit_export"))
    _validate_file_evidence(manifest.get("files"))
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_base64(path: Path) -> str:
    return base64.b64encode(bytes.fromhex(_sha256(path))).decode("ascii")


def _file_evidence(path: Path, content_type: str) -> dict:
    size = path.stat().st_size
    if size > MAX_PUT_OBJECT_BYTES:
        raise ValueError(f"{path.name} exceeds the single-object backup limit")
    return {
        "bytes": size,
        "content_type": content_type,
        "sha256": _sha256(path),
    }


def _audit_evidence(path: Path) -> dict:
    row_count = 0
    max_id = 0
    previous_id = 0
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                payload = json.loads(line)
                row_id = payload["id"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"invalid audit JSON on line {line_number}") from exc
            if type(row_id) is not int or row_id <= previous_id:
                raise ValueError("audit IDs must be positive and strictly increasing")
            previous_id = row_id
            max_id = row_id
            row_count += 1
    return {"max_id": max_id, "row_count": row_count}


def _validate_identity(
    *, backup_id: str, created_at: str, environment: str, bucket: str
):
    if not all(
        isinstance(value, str)
        for value in (backup_id, created_at, environment, bucket)
    ):
        raise ValueError("backup identity fields must be strings")
    if not ENVIRONMENT_RE.fullmatch(environment):
        raise ValueError("invalid backup environment")
    expected_bucket = f"psd401-prr-backups-{environment}"
    if bucket != expected_bucket:
        raise ValueError(f"backup bucket must be {expected_bucket}")
    parsed_uuid = uuid.UUID(backup_id)
    if str(parsed_uuid) != backup_id or parsed_uuid.version != 4:
        raise ValueError("backup ID must be a canonical UUIDv4")
    try:
        timestamp = dt.datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != dt.timedelta(0):
        raise ValueError("created_at must be UTC")
    normalized = timestamp.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if normalized != created_at:
        raise ValueError("created_at must use canonical UTC seconds")
    return timestamp


def _validate_manifest_identity(manifest: object) -> dict:
    # The installed pre-deployer backup helper stored Docker's bare digest.
    # Accept that one legacy representation only while verifying immutable
    # backup data; newly created manifests remain strict `sha256:<digest>`.
    validated = _validate_manifest_schema(
        manifest, allow_legacy_bare_image_id=True
    )
    timestamp = _validate_identity(
        backup_id=validated["backup_id"],
        created_at=validated["created_at"],
        environment=validated["environment"],
        bucket=validated["bucket"],
    )
    expected_prefix = (
        f"app-backups/{timestamp:%Y/%m/%d}/{validated['backup_id']}"
    )
    if validated["s3_prefix"] != expected_prefix:
        raise ValueError("backup S3 prefix does not match its identity")
    return validated


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_manifest(
    *,
    database_path: Path,
    audit_path: Path,
    output_path: Path,
    backup_id: str,
    created_at: str,
    environment: str,
    image_id: str,
    bucket: str,
) -> dict:
    timestamp = _validate_identity(
        backup_id=backup_id,
        created_at=created_at,
        environment=environment,
        bucket=bucket,
    )
    _validate_image_id(image_id)
    if database_path.name != "database.dump" or audit_path.name != "audit.jsonl.gz":
        raise ValueError("backup artifacts must use their canonical names")
    prefix = f"app-backups/{timestamp:%Y/%m/%d}/{backup_id}"
    manifest = {
        "audit_export": _audit_evidence(audit_path),
        "backup_id": backup_id,
        "bucket": bucket,
        "created_at": created_at,
        "environment": environment,
        "files": {
            database_path.name: _file_evidence(database_path, FILES[database_path.name]),
            audit_path.name: _file_evidence(audit_path, FILES[audit_path.name]),
        },
        "image_id": image_id,
        "s3_prefix": prefix,
        "schema_version": 1,
    }
    _validate_manifest_schema(manifest)
    _atomic_json_write(output_path, manifest)
    return manifest


def verify_manifest(manifest_path: Path, directory: Path) -> dict:
    manifest = _validate_manifest_identity(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    for name, content_type in FILES.items():
        evidence = manifest["files"][name]
        path = directory / name
        if evidence.get("content_type") != content_type:
            raise ValueError(f"{name} content type does not match")
        if evidence.get("bytes") != path.stat().st_size:
            raise ValueError(f"{name} size does not match")
        if evidence.get("sha256") != _sha256(path):
            raise ValueError(f"{name} SHA-256 does not match")
    if manifest.get("audit_export") != _audit_evidence(directory / "audit.jsonl.gz"):
        raise ValueError("audit export boundary does not match")
    return manifest


def verify_restored_audit(
    manifest: dict, *, restored_count: int, restored_max_id: int
) -> None:
    validated = _validate_manifest_identity(manifest)
    exported = validated["audit_export"]
    if (
        type(restored_count) is not int
        or type(restored_max_id) is not int
        or restored_count < 0
        or restored_max_id < 0
    ):
        raise ValueError("restore audit counts are invalid")
    if restored_count < exported["row_count"]:
        raise ValueError("restored database has fewer audit rows than the export")
    if restored_max_id < exported["max_id"]:
        raise ValueError("restored database maximum audit ID precedes the export")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify PRR app-backup evidence"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--audit", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--backup-id", required=True)
    create.add_argument("--created-at", required=True)
    create.add_argument("--environment", required=True)
    create.add_argument("--image-id", required=True)
    create.add_argument("--bucket", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--directory", type=Path, required=True)
    checksum = commands.add_parser("checksum-base64")
    checksum.add_argument("path", type=Path)
    restored = commands.add_parser("verify-restored")
    restored.add_argument("--manifest", type=Path, required=True)
    restored.add_argument("--restored-count", type=int, required=True)
    restored.add_argument("--restored-max-id", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create":
        create_manifest(
            database_path=args.database,
            audit_path=args.audit,
            output_path=args.output,
            backup_id=args.backup_id,
            created_at=args.created_at,
            environment=args.environment,
            image_id=args.image_id,
            bucket=args.bucket,
        )
    elif args.command == "verify":
        verify_manifest(args.manifest, args.directory)
    elif args.command == "checksum-base64":
        print(checksum_base64(args.path))
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        verify_restored_audit(
            manifest,
            restored_count=args.restored_count,
            restored_max_id=args.restored_max_id,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
