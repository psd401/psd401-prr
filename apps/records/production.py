"""Human-authorized, integrity-checked installment package production."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import PurePath
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .exemption_logs import current_exemption_log_manifest_sha256
from .fees import assert_deposit_gate_clear
from .models import FeeDepositGate, Installment, RecordItem
from .services import CLOSED_STATUSES, _require_current_records_officer
from .storage import (
    READ_CHUNK_SIZE,
    generated_production_storage_key,
    load_verified_object,
    store_immutable_object,
)

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ ()-]+")


def _release_filename(item: RecordItem, index: int) -> str:
    name = PurePath(str(item.original_filename or "")).name
    name = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    if not name:
        name = f"record-{item.pk}.bin"
    if len(name) > 180:
        suffix = PurePath(name).suffix[:12]
        stem_limit = 180 - len(suffix)
        name = f"{PurePath(name).stem[:stem_limit]}{suffix}"
    return f"records/{index:04d}-{name}"


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _artifact_for_item(item: RecordItem, index: int):
    if item.reviewed_by_id is None or item.reviewed_at is None:
        raise ValidationError(
            f"Record item {item.pk} is not releasable after human review."
        )
    if item.second_review_required and item.second_reviewed_at is None:
        raise ValidationError(
            f"Record item {item.pk} requires an approved second review."
        )
    if item.review_state == RecordItem.ReviewState.WITHHELD:
        return {
            "record_item_id": str(item.pk),
            "review_state": item.review_state,
            "included": False,
            "filename": None,
            "sha256": None,
        }, None

    if (
        item.review_state != RecordItem.ReviewState.RESPONSIVE
    ):
        raise ValidationError(
            f"Record item {item.pk} is not releasable after human review."
        )

    has_redactions = any(
        redaction.voided_at is None
        for redaction in item.redactions.all()
    )
    if has_redactions:
        if not (
            item.redacted_storage_key
            and item.redacted_sha256
            and item.redacted_content_length is not None
            and item.redacted_content_type
            and item.redacted_manifest_sha256
        ):
            raise ValidationError(
                f"Record item {item.pk} requires an approved redacted artifact."
            )
        artifact = {
            "storage_key": item.redacted_storage_key,
            "sha256": item.redacted_sha256,
            "size": item.redacted_content_length,
            "content_type": item.redacted_content_type,
            "kind": "redacted",
        }
    else:
        artifact = {
            "storage_key": item.storage_key,
            "sha256": item.sha256,
            "size": item.content_length,
            "content_type": item.content_type,
            "kind": "original",
        }
    filename = _release_filename(item, index)
    return {
        "record_item_id": str(item.pk),
        "review_state": item.review_state,
        "included": True,
        "filename": filename,
        "sha256": artifact["sha256"],
        "content_length": artifact["size"],
        "artifact": artifact["kind"],
    }, artifact


def _production_plan(installment: Installment):
    items = list(
        RecordItem.objects.select_for_update()
        .filter(installments=installment)
        .prefetch_related(
            "redactions__exemption",
            "withholding_exemptions",
        )
        .order_by("created_at", "pk")
    )
    if not items:
        raise ValidationError("An installment must contain at least one record item.")

    records = []
    artifacts = []
    has_exemptions = False
    for index, item in enumerate(items, start=1):
        if item.request_id != installment.request_id:
            raise ValidationError(
                "Every installment item must belong to the same request."
            )
        if item.review_state in {
            RecordItem.ReviewState.UNREVIEWED,
            RecordItem.ReviewState.NON_RESPONSIVE,
            RecordItem.ReviewState.DUPLICATE,
            RecordItem.ReviewState.SUPERSEDED,
        }:
            raise ValidationError(
                f"Record item {item.pk} is not releasable in an installment."
            )
        manifest_record, artifact = _artifact_for_item(item, index)
        records.append(manifest_record)
        if artifact is not None:
            artifacts.append((manifest_record["filename"], artifact))
        if (
            item.review_state == RecordItem.ReviewState.WITHHELD
            or any(
                redaction.voided_at is None
                for redaction in item.redactions.all()
            )
            or item.withholding_exemption_id is not None
            or bool(item.withholding_exemptions.all())
        ):
            has_exemptions = True

    exemption_log = None
    if has_exemptions:
        if not (
            installment.exemption_log_storage_key
            and installment.exemption_log_sha256
            and installment.exemption_log_size is not None
            and installment.exemption_log_manifest_sha256
            and installment.exemption_log_prepared_at is not None
        ):
            raise ValidationError(
                "An exemption log is required before this installment can be prepared."
            )
        if (
            current_exemption_log_manifest_sha256(installment)
            != installment.exemption_log_manifest_sha256
        ):
            raise ValidationError(
                "The installment exemption evidence changed after generation; "
                "create a new installment before production."
            )
        exemption_log = {
            "storage_key": installment.exemption_log_storage_key,
            "sha256": installment.exemption_log_sha256,
            "size": installment.exemption_log_size,
        }

    manifest = {
        "format_version": 1,
        "request_number": installment.request.number,
        "installment_sequence": installment.sequence,
        "records": records,
        "includes_exemption_log": exemption_log is not None,
    }
    manifest_bytes = _canonical_json(manifest)
    return (
        manifest,
        manifest_bytes,
        hashlib.sha256(manifest_bytes).hexdigest(),
        artifacts,
        exemption_log,
    )


def _copy_verified_to_archive(archive, name, artifact, *, client):
    verified = load_verified_object(
        artifact["storage_key"],
        expected_size=artifact["size"],
        expected_sha256=artifact["sha256"],
        _client=client,
    )
    try:
        with archive.open(name, "w") as destination:
            shutil.copyfileobj(verified, destination, READ_CHUNK_SIZE)
    finally:
        verified.close()


@transaction.atomic
def prepare_installment(installment_id, *, actor, _client=None) -> Installment:
    """Prepare one immutable ZIP after a current Records Officer confirms it."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to prepare an installment.",
    )
    installment = (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )
    assert_deposit_gate_clear(
        installment.request,
        scope=FeeDepositGate.Scope.PRODUCTION,
    )
    # Lock the selected membership snapshot before creating an external object.
    list(
        Installment.items.through.objects.select_for_update()
        .filter(installment_id=installment.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    (
        _manifest,
        manifest_bytes,
        manifest_sha256,
        artifacts,
        exemption_log,
    ) = _production_plan(installment)

    if installment.package_storage_key:
        if installment.package_manifest_sha256 != manifest_sha256:
            raise ValidationError(
                "The installment changed after preparation; create a new production package."
            )
        return installment

    package = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115 - the spool is the returned package
    try:
        with zipfile.ZipFile(
            package,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for filename, artifact in artifacts:
                _copy_verified_to_archive(
                    archive,
                    filename,
                    artifact,
                    client=_client,
                )
            if exemption_log is not None:
                _copy_verified_to_archive(
                    archive,
                    "exemption-log.pdf",
                    exemption_log,
                    client=_client,
                )

        package.seek(0)
        package_digest = hashlib.sha256()
        package_size = 0
        while chunk := package.read(READ_CHUNK_SIZE):
            package_size += len(chunk)
            package_digest.update(chunk)
        package_sha256 = package_digest.hexdigest()
        package.seek(0)
        package_key = generated_production_storage_key(
            installment.request_id,
            installment.pk,
        )
        store_immutable_object(
            package_key,
            package,
            content_length=package_size,
            sha256=package_sha256,
            content_type="application/zip",
            _client=_client,
        )
    finally:
        package.close()

    installment.package_storage_key = package_key
    installment.package_sha256 = package_sha256
    installment.package_size = package_size
    installment.package_manifest_sha256 = manifest_sha256
    installment.package_prepared_at = timezone.now()
    installment.package_prepared_by = current_actor
    installment.save(
        update_fields=[
            "package_storage_key",
            "package_sha256",
            "package_size",
            "package_manifest_sha256",
            "package_prepared_at",
            "package_prepared_by",
        ]
    )
    audit(
        "installment.prepared",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        package_sha256=package_sha256,
        package_size=package_size,
        manifest_sha256=manifest_sha256,
        included_item_count=len(artifacts),
    )
    return installment


def installment_download_url(installment: Installment) -> str:
    configured = urlsplit(str(runtime.get("PORTAL_BASE_URL")))
    if configured.scheme not in {"http", "https"} or not configured.netloc:
        raise ValidationError("The requester portal origin is not configured.")
    path = reverse(
        "installment-download",
        kwargs={
            "request_number": installment.request.number,
            "sequence": installment.sequence,
            "token": installment.download_token,
        },
    )
    return urlunsplit((configured.scheme, configured.netloc, path, "", ""))


def current_installment_manifest_sha256(installment: Installment) -> str:
    """Digest the current locked production membership and review evidence."""
    list(
        Installment.items.through.objects.select_for_update()
        .filter(installment_id=installment.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return _production_plan(installment)[2]


def validate_prepared_installment_snapshot(installment: Installment) -> None:
    """Fail closed if current legal evidence differs from the prepared ZIP."""
    if (
        current_installment_manifest_sha256(installment)
        != installment.package_manifest_sha256
    ):
        raise ValidationError(
            "The installment changed after preparation; create a new production package."
        )


@transaction.atomic
def release_installment(
    installment_id,
    *,
    actor,
    _renew_expired_with_notice=False,
) -> tuple[Installment, str]:
    """Activate one prepared package and rotate its scoped requester token."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to release an installment.",
    )
    installment = (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )
    if installment.revoked_at is not None:
        raise ValidationError(
            "A revoked installment cannot be released again; create a new "
            "installment for these records."
        )
    if installment.request.status in CLOSED_STATUSES:
        raise ValidationError(
            "This request is closed; reopen it before releasing records."
        )
    if not (
        installment.package_storage_key
        and installment.package_sha256
        and installment.package_size is not None
        and installment.package_manifest_sha256
        and installment.package_prepared_at
    ):
        raise ValidationError(
            "A verified prepared package is required before release."
        )
    # Preparation and release are distinct human actions. Re-lock and rebuild
    # the legal manifest so changes between them cannot release stale bytes.
    validate_prepared_installment_snapshot(installment)
    if installment.fee_amount and installment.fee_paid_at is None:
        raise ValidationError(
            "Any approved installment fee must be paid in full before release."
        )
    if installment.released_at is None:
        # Replaying or renewing a released installment re-delivers records
        # already produced; only a first release is production the deposit
        # holds.
        assert_deposit_gate_clear(
            installment.request,
            scope=FeeDepositGate.Scope.PRODUCTION,
        )
    now = timezone.now()
    if installment.released_at is not None:
        if (
            installment.download_expires_at is None
            or installment.download_expires_at <= now
        ):
            if not _renew_expired_with_notice:
                raise ValidationError(
                    "The prior download expired; renew it through the requester "
                    "notice release workflow."
                )
            prior_expiration = installment.download_expires_at
            installment.download_token = uuid.uuid4()
            installment.download_expires_at = now + timedelta(
                days=runtime.get("PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS"),
            )
            installment.save(
                update_fields=[
                    "download_token",
                    "download_expires_at",
                ]
            )
            audit(
                "installment.download_renewed",
                target=installment.request.number,
                actor=current_actor,
                installment_id=installment.pk,
                sequence=installment.sequence,
                package_sha256=installment.package_sha256,
                package_size=installment.package_size,
                prior_download_expired_at=(
                    prior_expiration.isoformat()
                    if prior_expiration is not None
                    else None
                ),
                download_expires_at=installment.download_expires_at.isoformat(),
            )
            return installment, installment_download_url(installment)
        return installment, installment_download_url(installment)

    installment.download_token = uuid.uuid4()
    installment.released_at = now
    installment.download_expires_at = now + timedelta(
        days=runtime.get("PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS"),
    )
    installment.save(
        update_fields=[
            "download_token",
            "released_at",
            "download_expires_at",
        ]
    )
    audit(
        "installment.released",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        package_sha256=installment.package_sha256,
        package_size=installment.package_size,
        download_expires_at=installment.download_expires_at.isoformat(),
    )
    return installment, installment_download_url(installment)


@transaction.atomic
def release_installment_with_notice(
    installment_id,
    *,
    actor,
    is_final,
):
    """Release one package and atomically create its human-approval notice."""
    if not isinstance(is_final, bool):
        raise ValidationError("The final-installment flag must be a boolean.")
    installment, download_url = release_installment(
        installment_id,
        actor=actor,
        _renew_expired_with_notice=True,
    )
    from apps.correspondence import services as correspondence_services
    from apps.correspondence.models import Correspondence

    candidates = (
        Correspondence.objects.filter(
            request=installment.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.INSTALLMENT_NOTICE,
            template_identifier="installment_notice",
            template_version="3",
            draft_metadata__installment_id=installment.pk,
        )
        .exclude(status=Correspondence.Status.CANCELLED)
        .order_by("created_at", "pk")
    )
    existing = None
    for candidate in candidates:
        candidate_metadata = (
            candidate.draft_metadata
            if candidate.status == Correspondence.Status.DRAFT
            else candidate.approved_metadata
        )
        if candidate_metadata.get("download_url") == download_url:
            existing = candidate
            break
    if existing is not None:
        metadata = (
            existing.draft_metadata
            if existing.status == Correspondence.Status.DRAFT
            else existing.approved_metadata
        )
        if (
            metadata.get("download_url") != download_url
            or metadata.get("is_final") is not is_final
        ):
            raise ValidationError(
                "An installment notice already exists with a different release snapshot."
            )
        return installment, download_url, existing

    notice = correspondence_services.create_installment_notice_draft(
        installment.request,
        installment=installment,
        is_final=is_final,
        actor=actor,
    )
    audit(
        "installment.notice_drafted",
        target=installment.request.number,
        actor=actor,
        installment_id=installment.pk,
        correspondence_id=str(notice.pk),
        is_final=is_final,
    )
    return installment, download_url, notice
