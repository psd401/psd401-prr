# ABOUTME: Renders a reviewable PDF from records that are not PDFs, so review
# ABOUTME: and redaction work on more than one format.
"""Build the rendition a human reviews, without disturbing the record.

The reviewer and the redaction burn-in both open PDFs. That is a reasonable
engine choice and a terrible collection policy: a district request pulls in
Word documents, spreadsheets, phone photos, and email, and none of those could
be read or redacted in this application.

This module renders those into a PDF *derivative*. Three rules keep that from
quietly becoming a lie about the record:

* **The original is never replaced.** It keeps its own key, digest, and length,
  and it is what gets produced when a record is released unredacted. The
  derivative is a reading copy.
* **A failed or impossible conversion is recorded, not swallowed.** A record
  whose derivative failed stays visibly unreviewable rather than appearing as
  an empty document, which a reviewer could mistake for "nothing to withhold".
* **Conversion is bounded.** Everything runs against a size ceiling and a wall
  clock, because these renderers take untrusted input from outside the
  district.

Redaction of a converted record burns into the derivative, which is the copy
the requester receives. That is the point: releasing the original .docx would
ship tracked changes, comments, and author metadata that no box on a page ever
covered.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
from pathlib import Path

from apps.audit.models import audit
from apps.configuration import runtime

from .handling import CONVERT, MEDIA, OPAQUE, PREVIEW, handling_for_item
from .models import RecordItem
from .storage import (
    generated_derivative_storage_key,
    load_verified_object,
    store_immutable_object,
)


class DerivativeError(RuntimeError):
    """A conversion that cannot succeed, with a reason fit to show a human."""

    def __init__(self, detail: str, *, permanent: bool = True):
        super().__init__(detail)
        self.detail = detail
        self.permanent = permanent


#: Formats LibreOffice is asked to open. Anything outside this set is refused
#: rather than handed to it hopefully - the converter is a large, complex
#: process running on untrusted input, so its input set is explicit.
_OFFICE_SUFFIXES = {
    ".doc", ".docx", ".rtf", ".odt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
}
_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
}
_MESSAGE_SUFFIXES = {".eml", ".msg"}
_TEXT_SUFFIXES = {".txt", ".log", ".md", ".json", ".xml", ".htm", ".html"}


def _suffix(item) -> str:
    return Path(item.original_filename or "").suffix.lower()


def _image_to_pdf(payload: bytes, item) -> bytes:
    """Wrap one image in a single-page PDF at its natural size."""
    import pymupdf as fitz

    try:
        picture = fitz.open(stream=payload, filetype=_suffix(item).lstrip("."))
    except Exception as exc:
        raise DerivativeError(f"The image could not be opened: {exc}") from exc
    with picture:
        try:
            pdf_bytes = picture.convert_to_pdf()
        except Exception as exc:
            raise DerivativeError(
                f"The image could not be rendered to PDF: {exc}"
            ) from exc
    return pdf_bytes


def _text_to_pdf(payload: bytes, item) -> bytes:
    from .vault_ingest import _render_text_pdf

    text = payload.decode("utf-8", errors="replace")
    if _suffix(item) in {".htm", ".html"}:
        from .vault_ingest import _HTML_TAG

        text = _HTML_TAG.sub(" ", text)
    return _render_text_pdf(text)


def _message_to_pdf(payload: bytes, item) -> bytes:
    """Render an email to the same searchable PDF Vault ingest produces."""
    from .vault_ingest import convert_message_to_searchable_pdf

    if _suffix(item) == ".msg":
        # Outlook's own container is not RFC822 and needs a dedicated parser
        # that is not installed. Saying so is better than rendering a header
        # dump and calling it the message.
        raise DerivativeError(
            "Outlook .msg files need a dedicated parser that is not installed. "
            "Export the message as .eml, or review it outside the application "
            "and record the reasoning."
        )
    converted = convert_message_to_searchable_pdf(payload)
    return converted.content


def _office_to_pdf(payload: bytes, item) -> bytes:
    """Convert through headless LibreOffice in a throwaway profile.

    Runs with its own HOME so concurrent conversions cannot collide in a
    shared user profile, and under a wall clock because a malformed document
    can otherwise hang the converter indefinitely.
    """
    binary = runtime.get("PRR_OFFICE_CONVERTER_BINARY")
    suffix = _suffix(item)
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        source = root / f"input{suffix}"
        source.write_bytes(payload)
        profile = root / "profile"
        profile.mkdir()
        try:
            completed = subprocess.run(
                [
                    binary,
                    "--headless",
                    "--norestore",
                    "--nolockcheck",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to", "pdf",
                    "--outdir", str(root),
                    str(source),
                ],
                capture_output=True,
                timeout=runtime.get("PRR_OFFICE_CONVERTER_TIMEOUT_SECONDS"),
                env={"HOME": str(profile), "PATH": "/usr/bin:/bin"},
                check=False,
            )
        except FileNotFoundError as exc:
            raise DerivativeError(
                "No office document converter is installed in this "
                "environment, so this record cannot be rendered for review.",
                permanent=False,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DerivativeError(
                "The office document converter timed out on this record.",
                permanent=False,
            ) from exc
        produced = root / "input.pdf"
        if completed.returncode != 0 or not produced.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise DerivativeError(
                f"The office document converter could not read this file. {detail}"[:400]
            )
        return produced.read_bytes()


def _converter_for(item):
    suffix = _suffix(item)
    if suffix in _IMAGE_SUFFIXES or (item.content_type or "").startswith("image/"):
        return _image_to_pdf
    if suffix in _OFFICE_SUFFIXES:
        return _office_to_pdf
    if suffix in _MESSAGE_SUFFIXES:
        return _message_to_pdf
    if suffix in _TEXT_SUFFIXES:
        return _text_to_pdf
    return None


def needs_derivative(item) -> bool:
    return handling_for_item(item) == CONVERT


def build_derivative(record_item_id, *, actor=None, _client=None):
    """Render one record into a reviewable PDF and retain it.

    Idempotent: a record that already has a ready derivative is left alone, so
    a retry cannot produce a second reading copy of the same record.
    """
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    if item.derivative_state == RecordItem.DerivativeState.READY:
        return item

    path = handling_for_item(item)
    if path == PREVIEW:
        return _record_outcome(item, RecordItem.DerivativeState.NONE, "")
    if path == MEDIA:
        return _record_outcome(
            item,
            RecordItem.DerivativeState.UNSUPPORTED,
            "Time-based media is reviewed with a player, not a page rendition.",
        )
    if path == OPAQUE:
        return _record_outcome(
            item,
            RecordItem.DerivativeState.UNSUPPORTED,
            "No available renderer can open this format.",
        )

    if item.content_length > runtime.get("PRR_DERIVATIVE_MAX_SOURCE_BYTES"):
        return _record_outcome(
            item,
            RecordItem.DerivativeState.UNSUPPORTED,
            "This record is larger than the conversion limit.",
        )
    convert = _converter_for(item)
    if convert is None:
        return _record_outcome(
            item,
            RecordItem.DerivativeState.UNSUPPORTED,
            "No renderer is registered for this format.",
        )

    source = load_verified_object(
        item.storage_key,
        expected_size=item.content_length,
        expected_sha256=item.sha256,
        _client=_client,
    )
    try:
        payload = source.read()
    finally:
        source.close()

    try:
        rendered = convert(payload, item)
    except DerivativeError as exc:
        return _record_outcome(
            item,
            RecordItem.DerivativeState.UNSUPPORTED
            if exc.permanent
            else RecordItem.DerivativeState.FAILED,
            exc.detail,
        )
    except Exception as exc:  # noqa: BLE001 - a renderer fault must not look like a clean file
        return _record_outcome(
            item,
            RecordItem.DerivativeState.FAILED,
            f"The renderer failed on this record: {exc}"[:500],
        )

    if not rendered:
        return _record_outcome(
            item,
            RecordItem.DerivativeState.FAILED,
            "The renderer produced an empty document.",
        )

    digest = hashlib.sha256(rendered).hexdigest()
    storage_key = generated_derivative_storage_key(item.request_id, item.id)
    store_immutable_object(
        storage_key,
        io.BytesIO(rendered),
        content_length=len(rendered),
        sha256=digest,
        content_type="application/pdf",
        _client=_client,
    )
    item.derivative_storage_key = storage_key
    item.derivative_sha256 = digest
    item.derivative_content_length = len(rendered)
    item.derivative_state = RecordItem.DerivativeState.READY
    item.derivative_detail = ""
    item.save(update_fields=[
        "derivative_storage_key", "derivative_sha256",
        "derivative_content_length", "derivative_state", "derivative_detail",
    ])
    audit(
        "record.derivative.built",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        source_content_type=item.content_type,
        derivative_sha256=digest,
        derivative_bytes=len(rendered),
    )
    return item


def _record_outcome(item, state, detail):
    if item.derivative_state == state and item.derivative_detail == detail:
        return item
    item.derivative_state = state
    item.derivative_detail = detail[:500]
    item.save(update_fields=["derivative_state", "derivative_detail"])
    return item
