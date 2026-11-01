"""Records that are not PDFs become readable without replacing the record."""

import hashlib
import io
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.derivatives import DerivativeError, build_derivative
from apps.records.models import PublicRecordsRequest, RecordItem, Requester
from apps.records.preview import is_previewable


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        source = kwargs["Body"]
        position = source.tell()
        payload = source.read()
        source.seek(position)
        if kwargs["Key"] in self.objects:
            raise AssertionError(f"immutable object {kwargs['Key']} overwritten")
        self.objects[kwargs["Key"]] = payload

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}


def _png_bytes():
    import pymupdf as fitz

    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30))
    pixmap.set_rect(pixmap.irect, (200, 40, 40))
    return pixmap.tobytes("png")


class DerivativeTests(TestCase):
    def setUp(self):
        self.client_stub = _ObjectClient()
        requester = Requester.objects.create(name="R", email="r@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester, description="d", received_at=timezone.now()
        )

    def _item(self, payload, filename, content_type):
        digest = hashlib.sha256(payload).hexdigest()
        key = f"records/{self.request.id}/{filename}"
        self.client_stub.objects[key] = payload
        return RecordItem.objects.create(
            request=self.request,
            source_system="nextrequest_legacy",
            original_filename=filename,
            storage_key=key,
            sha256=digest,
            content_length=len(payload),
            content_type=content_type,
        )

    def _build(self, item):
        return build_derivative(item.pk, _client=self.client_stub)

    def test_image_becomes_a_readable_pdf(self):
        item = self._item(_png_bytes(), "photo.png", "image/png")
        self.assertFalse(is_previewable(item))

        built = self._build(item)

        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.READY)
        self.assertTrue(built.derivative_storage_key.startswith("derivatives/"))
        self.assertTrue(is_previewable(built))
        stored = self.client_stub.objects[built.derivative_storage_key]
        self.assertTrue(stored.startswith(b"%PDF"))
        self.assertEqual(
            hashlib.sha256(stored).hexdigest(), built.derivative_sha256
        )

    def test_the_original_record_is_never_replaced(self):
        payload = _png_bytes()
        item = self._item(payload, "photo.png", "image/png")
        built = self._build(item)
        # The original key, digest, and length are the chain of custody.
        self.assertEqual(built.storage_key, item.storage_key)
        self.assertEqual(built.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(built.content_length, len(payload))
        self.assertEqual(self.client_stub.objects[built.storage_key], payload)
        self.assertNotEqual(built.derivative_storage_key, built.storage_key)

    def test_plain_text_becomes_a_readable_pdf(self):
        item = self._item(b"line one\nline two\n", "notes.txt", "text/plain")
        built = self._build(item)
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.READY)
        self.assertTrue(
            self.client_stub.objects[built.derivative_storage_key].startswith(b"%PDF")
        )

    def test_html_is_rendered_as_text_not_markup(self):
        item = self._item(
            b"<html><body><p>Board minutes</p></body></html>",
            "page.html",
            "text/html",
        )
        built = self._build(item)
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.READY)

    def test_a_pdf_record_needs_no_derivative(self):
        item = self._item(b"%PDF-1.4 x", "already.pdf", "application/pdf")
        built = self._build(item)
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.NONE)
        self.assertEqual(built.derivative_storage_key, "")
        self.assertTrue(is_previewable(built))

    def test_video_is_unsupported_rather_than_rendered(self):
        item = self._item(b"\x00\x00\x00 ftypmp42", "bus.mp4", "video/mp4")
        built = self._build(item)
        self.assertEqual(
            built.derivative_state, RecordItem.DerivativeState.UNSUPPORTED
        )
        self.assertIn("player", built.derivative_detail)
        self.assertFalse(is_previewable(built))

    def test_dead_format_is_unsupported_with_a_reason(self):
        item = self._item(b"CWS\x08", "portable.swf", "application/octet-stream")
        built = self._build(item)
        self.assertEqual(
            built.derivative_state, RecordItem.DerivativeState.UNSUPPORTED
        )
        self.assertIn("No available renderer", built.derivative_detail)

    def test_outlook_msg_says_what_to_do_instead_of_faking_a_render(self):
        item = self._item(b"\xd0\xcf\x11\xe0", "mail.msg", "application/vnd.ms-outlook")
        built = self._build(item)
        self.assertEqual(
            built.derivative_state, RecordItem.DerivativeState.UNSUPPORTED
        )
        self.assertIn(".eml", built.derivative_detail)

    def test_a_renderer_fault_is_recorded_not_swallowed(self):
        # An empty or broken render must never look like a clean, blank
        # document -- a reviewer would read that as "nothing to withhold".
        item = self._item(b"not really a png", "broken.png", "image/png")
        built = self._build(item)
        self.assertIn(
            built.derivative_state,
            {RecordItem.DerivativeState.FAILED,
             RecordItem.DerivativeState.UNSUPPORTED},
        )
        self.assertTrue(built.derivative_detail)
        self.assertFalse(is_previewable(built))

    def test_an_empty_render_is_a_failure(self):
        item = self._item(_png_bytes(), "photo.png", "image/png")
        with patch("apps.records.derivatives._image_to_pdf", return_value=b""):
            built = self._build(item)
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.FAILED)
        self.assertIn("empty", built.derivative_detail)

    @override_settings(PRR_DERIVATIVE_MAX_SOURCE_BYTES=1024)
    def test_oversized_records_are_refused_before_conversion(self):
        # 1024 is the registry floor for this limit. An arbitrarily tiny
        # value is no longer configurable -- the store refuses it -- so the
        # record is what grows past the limit instead.
        item = self._item(_png_bytes(), "photo.png", "image/png")
        item.content_length = 1025
        item.save(update_fields=["content_length"])
        built = self._build(item)
        self.assertEqual(
            built.derivative_state, RecordItem.DerivativeState.UNSUPPORTED
        )
        self.assertIn("larger than the conversion limit", built.derivative_detail)

    def test_building_twice_does_not_create_a_second_rendition(self):
        item = self._item(_png_bytes(), "photo.png", "image/png")
        first = self._build(item)
        key = first.derivative_storage_key
        # The stub refuses overwrites, so a second write would raise.
        second = build_derivative(item.pk, _client=self.client_stub)
        self.assertEqual(second.derivative_storage_key, key)

    def test_a_ready_derivative_is_audited(self):
        item = self._item(_png_bytes(), "photo.png", "image/png")
        self._build(item)
        entry = AuditLog.objects.filter(action="record.derivative.built").get()
        self.assertEqual(entry.detail["source_content_type"], "image/png")

    def test_office_conversion_runs_the_converter_and_stores_its_output(self):
        item = self._item(b"PK\x03\x04 docx bytes", "memo.docx",
                          "application/vnd.openxmlformats-officedocument."
                          "wordprocessingml.document")
        with patch(
            "apps.records.derivatives._office_to_pdf",
            return_value=b"%PDF-1.4 converted",
        ) as converter:
            built = self._build(item)
        converter.assert_called_once()
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.READY)
        self.assertEqual(
            self.client_stub.objects[built.derivative_storage_key],
            b"%PDF-1.4 converted",
        )

    def test_a_missing_office_converter_is_reported_as_retryable(self):
        item = self._item(b"PK\x03\x04", "memo.docx", "application/msword")
        with patch(
            "apps.records.derivatives._office_to_pdf",
            side_effect=DerivativeError("No converter installed.", permanent=False),
        ):
            built = self._build(item)
        # Retryable, because installing the converter later should fix it --
        # unlike a format nothing will ever render.
        self.assertEqual(built.derivative_state, RecordItem.DerivativeState.FAILED)
