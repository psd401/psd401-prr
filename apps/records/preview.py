"""Verified, raster-only staff previews for candidate record PDFs."""

from dataclasses import dataclass

import pymupdf as fitz
from django.core.exceptions import ValidationError

from apps.audit.models import audit
from apps.configuration import runtime

from .models import RecordItem
from .storage import load_verified_object


@dataclass(frozen=True)
class PdfPreviewPage:
    content: bytes
    page_number: int
    page_count: int
    width: int
    height: int


#: The only media type the preview renderer, and therefore the redaction
#: burn-in that shares it, can open today. Anything else can be stored and
#: given a review verdict but cannot be shown to a reviewer or redacted, so
#: callers need to be able to ask *before* a reviewer clicks into a dead end.
PREVIEWABLE_CONTENT_TYPES = frozenset({"application/pdf"})


def normalized_content_type(item) -> str:
    return item.content_type.lower().split(";", 1)[0].strip()


def has_ready_derivative(item) -> bool:
    return bool(
        item.derivative_storage_key
        and item.derivative_state == item.DerivativeState.READY
    )


def is_previewable(item) -> bool:
    """Can a reviewer actually open this record, and redact inside it?

    Kept next to the renderer rather than in the API layer so the list badge
    and the renderer can never disagree about what is reviewable. A record
    that is not a PDF becomes reviewable the moment its rendition is ready.
    """
    if has_ready_derivative(item):
        return True
    return (
        normalized_content_type(item) in PREVIEWABLE_CONTENT_TYPES
        and item.content_length <= runtime.get("PRR_RECORD_PREVIEW_MAX_BYTES")
    )


def _previewed_object(item):
    """Which stored object the reviewer actually reads.

    The rendition when one exists, the original when the record is already a
    PDF. Returned together with its own digest and length so the integrity
    check verifies the bytes being opened rather than a different object's.
    """
    if has_ready_derivative(item):
        return (
            item.derivative_storage_key,
            item.derivative_content_length or 0,
            item.derivative_sha256,
        )
    return item.storage_key, item.content_length, item.sha256


def _open_verified_pdf(item, *, _client=None):
    storage_key, content_length, sha256 = _previewed_object(item)
    maximum = runtime.get("PRR_RECORD_PREVIEW_MAX_BYTES")
    if content_length > maximum:
        raise ValidationError("This record exceeds the configured preview limit.")
    if (
        not has_ready_derivative(item)
        and normalized_content_type(item) not in PREVIEWABLE_CONTENT_TYPES
    ):
        raise ValidationError(
            "This record has no reviewable rendition yet, so it cannot be "
            "previewed or redacted."
        )
    source = load_verified_object(
        storage_key,
        expected_size=content_length,
        expected_sha256=sha256,
        _client=_client,
    )
    try:
        payload = source.read()
    finally:
        source.close()
    try:
        document = fitz.open(stream=payload, filetype="pdf")
    except (fitz.FileDataError, RuntimeError) as exc:
        raise ValidationError("The retained record is not a valid PDF.") from exc
    if document.page_count < 1:
        document.close()
        raise ValidationError("The retained PDF contains no pages.")
    if document.page_count > runtime.get("PRR_RECORD_PREVIEW_MAX_PAGES"):
        document.close()
        raise ValidationError("The retained PDF exceeds the configured page limit.")
    return document


def preview_pdf_metadata(record_item_id, *, actor=None, _client=None):
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    document = _open_verified_pdf(item, _client=_client)
    try:
        result = {"page_count": document.page_count}
    finally:
        document.close()
    if actor is not None:
        audit(
            "record.preview.opened",
            target=str(item.pk),
            actor=actor,
            request_number=item.request.number,
        )
    return result


def render_pdf_preview_page(
    record_item_id,
    *,
    page_number,
    actor=None,
    _client=None,
):
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    document = _open_verified_pdf(item, _client=_client)
    try:
        if isinstance(page_number, bool):
            raise ValidationError("A valid PDF page number is required.")
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise ValidationError("A valid PDF page number is required.") from exc
        if page_number < 1 or page_number > document.page_count:
            raise ValidationError("The requested PDF page does not exist.")
        page = document[page_number - 1]
        pixmap = page.get_pixmap(
            dpi=runtime.get("PRR_RECORD_PREVIEW_DPI"),
            alpha=False,
        )
        result = PdfPreviewPage(
            content=pixmap.tobytes("png"),
            page_number=page_number,
            page_count=document.page_count,
            width=pixmap.width,
            height=pixmap.height,
        )
    finally:
        document.close()
    if actor is not None:
        audit(
            "record.preview.page_rendered",
            target=str(item.pk),
            actor=actor,
            request_number=item.request.number,
            page_number=page_number,
        )
    return result
