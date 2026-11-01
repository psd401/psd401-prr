"""Immutable RCW 42.56.210(3) exemption/withholding log generation."""

from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
from pathlib import PurePath

import pymupdf as fitz
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit

from .models import Installment, RecordItem
from .services import _require_current_records_officer
from .storage import (
    generated_exemption_log_storage_key,
    store_immutable_object,
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _safe_filename(value):
    name = PurePath(str(value or "")).name
    name = _CONTROL.sub(" ", name).strip()
    return name[:240] or "Unnamed retained record"


def _citation_snapshot(exemption):
    return {
        "id": exemption.pk,
        "citation": exemption.citation,
        "short_name": exemption.short_name,
    }


def _build_manifest(installment):
    items = list(
        RecordItem.objects.select_for_update()
        .filter(installments=installment)
        .prefetch_related(
            "withholding_exemption",
            "withholding_exemptions",
            "redactions__exemption",
        )
        .order_by("created_at", "pk")
    )
    entries = []
    for item in items:
        if item.request_id != installment.request_id:
            raise ValidationError(
                "Every installment item must belong to the same request."
            )
        if item.reviewed_by_id is None or item.reviewed_at is None:
            raise ValidationError(
                f"Record item {item.pk} requires completed human review."
            )
        if item.second_review_required and item.second_reviewed_at is None:
            raise ValidationError(
                f"Record item {item.pk} requires approved second review."
            )

        if item.review_state == RecordItem.ReviewState.WITHHELD:
            exemptions = []
            seen = set()
            candidates = [item.withholding_exemption]
            candidates.extend(item.withholding_exemptions.all())
            for exemption in candidates:
                if exemption is not None and exemption.pk not in seen:
                    seen.add(exemption.pk)
                    exemptions.append(_citation_snapshot(exemption))
            if not exemptions:
                raise ValidationError(
                    f"Withheld record item {item.pk} has no exemption citation."
                )
            entries.append({
                "record_item_id": str(item.pk),
                "record_name": _safe_filename(item.original_filename),
                "disposition": "withheld_in_full",
                "citations": exemptions,
            })

        active_redactions = [
            redaction
            for redaction in item.redactions.all()
            if redaction.voided_at is None
        ]
        for redaction in active_redactions:
            entries.append({  # noqa: PERF401 - reads clearer as a loop
                "record_item_id": str(item.pk),
                "record_name": _safe_filename(item.original_filename),
                "disposition": "redacted_in_part",
                "page_number": redaction.page_number,
                "coordinates": [
                    redaction.x0,
                    redaction.y0,
                    redaction.x1,
                    redaction.y1,
                ],
                "citation": _citation_snapshot(redaction.exemption),
                "explanation": _CONTROL.sub(
                    " ",
                    str(redaction.explanation or "").strip(),
                )[:500],
            })

    if not entries:
        raise ValidationError(
            "This installment does not contain withheld or redacted records."
        )
    return {
        "format_version": 1,
        "request_number": installment.request.number,
        "installment_sequence": installment.sequence,
        "entries": entries,
    }


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def current_exemption_log_manifest_sha256(installment):
    """Digest the current locked exemption evidence for package revalidation."""
    manifest = _build_manifest(installment)
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()


def _render_pdf(manifest, *, prepared_at):
    document = fitz.open()
    page = None
    y = 54

    def line(value, *, size=10, bold=False, gap=4):
        nonlocal page, y
        wrapped = textwrap.wrap(str(value), width=92) or [""]
        for text_line in wrapped:
            if page is None or y > 745:
                page = document.new_page(width=612, height=792)
                y = 54
            safe = text_line.encode("latin-1", errors="replace").decode("latin-1")
            page.insert_text(
                (54, y),
                safe,
                fontsize=size,
                fontname="hebo" if bold else "helv",
            )
            y += size + 4
        y += gap

    line("EXEMPTION AND WITHHOLDING LOG", size=15, bold=True, gap=8)
    line(f"Public Records Request: {manifest['request_number']}", bold=True)
    line(f"Installment: {manifest['installment_sequence']}")
    line(f"Prepared: {prepared_at.date().isoformat()}", gap=12)
    line(
        "This log identifies records withheld in full or redacted in part "
        "and the legal basis applied under RCW 42.56.210(3).",
        gap=12,
    )
    for index, entry in enumerate(manifest["entries"], start=1):
        line(f"{index}. {entry['record_name']}", bold=True, gap=2)
        line(f"Record item: {entry['record_item_id']}", size=9, gap=2)
        if entry["disposition"] == "withheld_in_full":
            line("Disposition: Withheld in full", gap=2)
            for citation in entry["citations"]:
                line(
                    f"Legal basis: {citation['citation']} — {citation['short_name']}",
                    gap=2,
                )
        else:
            line(f"Disposition: Redacted in part — Page {entry['page_number']}", gap=2)
            citation = entry["citation"]
            line(
                f"Legal basis: {citation['citation']} — {citation['short_name']}",
                gap=2,
            )
            if entry["explanation"]:
                line(f"Explanation: {entry['explanation']}", gap=2)
        y += 8
    try:
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


@transaction.atomic
def generate_exemption_log(installment_id, *, actor, _client=None):
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to generate an exemption log.",
    )
    installment = (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )
    list(
        Installment.items.through.objects.select_for_update()
        .filter(installment_id=installment.pk)
        .values_list("pk", flat=True)
    )
    manifest = _build_manifest(installment)
    manifest_sha256 = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    if installment.exemption_log_storage_key:
        if installment.exemption_log_manifest_sha256 != manifest_sha256:
            raise ValidationError(
                "The installment exemption evidence changed after generation; "
                "create a new installment before production."
            )
        return installment

    prepared_at = timezone.now()
    payload = _render_pdf(manifest, prepared_at=prepared_at)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    storage_key = generated_exemption_log_storage_key(
        installment.request_id,
        installment.pk,
    )
    store_immutable_object(
        storage_key,
        io.BytesIO(payload),
        content_length=len(payload),
        sha256=payload_sha256,
        content_type="application/pdf",
        _client=_client,
    )
    installment.exemption_log_storage_key = storage_key
    installment.exemption_log_sha256 = payload_sha256
    installment.exemption_log_size = len(payload)
    installment.exemption_log_manifest_sha256 = manifest_sha256
    installment.exemption_log_prepared_at = prepared_at
    installment.exemption_log_prepared_by = current_actor
    installment.save(update_fields=[
        "exemption_log_storage_key",
        "exemption_log_sha256",
        "exemption_log_size",
        "exemption_log_manifest_sha256",
        "exemption_log_prepared_at",
        "exemption_log_prepared_by",
    ])
    audit(
        "installment.exemption_log.generated",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        manifest_sha256=manifest_sha256,
        output_sha256=payload_sha256,
        output_size=len(payload),
        entry_count=len(manifest["entries"]),
    )
    return installment
