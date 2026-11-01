"""Time-based records and unrenderable records still reach a lawful decision."""

import hashlib
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.media_review import (
    _cut_segments,
    _filters_for,
    render_media_redactions,
)
from apps.records.models import (
    ExemptionCitation,
    MediaRedaction,
    OpaqueRecordDisposition,
    PublicRecordsRequest,
    RecordItem,
    Requester,
)
from apps.records.opaque_review import (
    MIN_OPAQUE_REASONING,
    record_opaque_disposition,
)


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
            raise AssertionError("immutable object overwritten")
        self.objects[kwargs["Key"]] = payload

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}


class _Base(TestCase):
    def setUp(self):
        self.stub = _ObjectClient()
        self.user = get_user_model().objects.create_user(
            username="reviewer", email="reviewer@psd401.net"
        )
        requester = Requester.objects.create(name="R", email="r@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester, description="d", received_at=timezone.now()
        )
        self.exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)", short_name="Student personal info"
        )

    def _item(self, filename, content_type, payload=b"\x00\x00\x00 ftypmp42",
              review_state=RecordItem.ReviewState.RESPONSIVE):
        key = f"records/{self.request.id}/{filename}"
        self.stub.objects[key] = payload
        return RecordItem.objects.create(
            request=self.request,
            source_system="avigilon",
            original_filename=filename,
            storage_key=key,
            sha256=hashlib.sha256(payload).hexdigest(),
            content_length=len(payload),
            content_type=content_type,
            review_state=review_state,
        )


class MediaRedactionModelTests(_Base):
    def test_an_interval_must_run_forwards(self):
        item = self._item("bus.mp4", "video/mp4")
        with self.assertRaises(IntegrityError):
            MediaRedaction.objects.create(
                record_item=item, starts_at=10.0, ends_at=4.0,
                treatment=MediaRedaction.Treatment.MUTE_AUDIO,
                exemption=self.exemption,
            )

    def test_obscuring_a_region_requires_an_actual_region(self):
        # A region treatment with no box would obscure nothing while reading
        # afterwards as a redaction that was applied.
        item = self._item("bus.mp4", "video/mp4")
        with self.assertRaises(IntegrityError):
            MediaRedaction.objects.create(
                record_item=item, starts_at=1.0, ends_at=2.0,
                treatment=MediaRedaction.Treatment.OBSCURE_REGION,
                exemption=self.exemption,
            )

    def test_muting_audio_needs_no_region(self):
        item = self._item("call.mp3", "audio/mpeg")
        entry = MediaRedaction.objects.create(
            record_item=item, starts_at=1.0, ends_at=2.0,
            treatment=MediaRedaction.Treatment.MUTE_AUDIO,
            exemption=self.exemption,
        )
        self.assertIsNone(entry.x0)

    def test_an_exemption_citation_is_required(self):
        item = self._item("bus.mp4", "video/mp4")
        with self.assertRaises(IntegrityError):
            MediaRedaction.objects.create(
                record_item=item, starts_at=1.0, ends_at=2.0,
                treatment=MediaRedaction.Treatment.MUTE_AUDIO,
                exemption=None,
            )


class MediaFilterTests(_Base):
    def _redaction(self, **kwargs):
        item = kwargs.pop("item")
        return MediaRedaction(record_item=item, exemption=self.exemption, **kwargs)

    def test_region_filter_uses_normalized_coordinates(self):
        item = self._item("bus.mp4", "video/mp4")
        entry = self._redaction(
            item=item, starts_at=2.0, ends_at=5.0,
            treatment=MediaRedaction.Treatment.OBSCURE_REGION,
            x0=0.25, y0=0.5, x1=0.75, y1=1.0,
        )
        video, audio = _filters_for([entry], {"has_video": True, "has_audio": True})
        self.assertIn("drawbox=x=iw*0.2500:y=ih*0.5000", video)
        self.assertIn("w=iw*0.5000:h=ih*0.5000", video)
        self.assertIn("between(t,2.000,5.000)", video)
        self.assertEqual(audio, "")

    def test_muting_touches_audio_only(self):
        item = self._item("call.mp3", "audio/mpeg")
        entry = self._redaction(
            item=item, starts_at=8.0, ends_at=9.5,
            treatment=MediaRedaction.Treatment.MUTE_AUDIO,
        )
        video, audio = _filters_for([entry], {"has_video": False, "has_audio": True})
        self.assertEqual(video, "")
        self.assertIn("volume=enable='between(t,8.000,9.500)':volume=0", audio)

    def test_cuts_keep_everything_outside_the_removed_span(self):
        item = self._item("bus.mp4", "video/mp4")
        entry = self._redaction(
            item=item, starts_at=10.0, ends_at=20.0,
            treatment=MediaRedaction.Treatment.CUT,
        )
        self.assertEqual(_cut_segments([entry], 60.0), [(0.0, 10.0), (20.0, 60.0)])

    def test_overlapping_cuts_merge_instead_of_double_counting(self):
        item = self._item("bus.mp4", "video/mp4")
        first = self._redaction(
            item=item, starts_at=10.0, ends_at=20.0,
            treatment=MediaRedaction.Treatment.CUT,
        )
        second = self._redaction(
            item=item, starts_at=15.0, ends_at=25.0,
            treatment=MediaRedaction.Treatment.CUT,
        )
        self.assertEqual(
            _cut_segments([first, second], 40.0), [(0.0, 10.0), (25.0, 40.0)]
        )

    def test_a_cut_to_the_end_leaves_no_trailing_segment(self):
        item = self._item("bus.mp4", "video/mp4")
        entry = self._redaction(
            item=item, starts_at=30.0, ends_at=60.0,
            treatment=MediaRedaction.Treatment.CUT,
        )
        self.assertEqual(_cut_segments([entry], 60.0), [(0.0, 30.0)])


class MediaRenderTests(_Base):
    def _redact(self, item, **kwargs):
        return MediaRedaction.objects.create(
            record_item=item, exemption=self.exemption, **kwargs
        )

    def test_render_refuses_a_record_that_is_not_media(self):
        item = self._item("memo.pdf", "application/pdf", payload=b"%PDF x")
        with self.assertRaisesMessage(ValidationError, "not audio or video"):
            render_media_redactions(item.pk, actor=self.user, _client=self.stub)

    def test_render_refuses_before_a_responsive_verdict(self):
        item = self._item(
            "bus.mp4", "video/mp4",
            review_state=RecordItem.ReviewState.UNREVIEWED,
        )
        self._redact(
            item, starts_at=1.0, ends_at=2.0,
            treatment=MediaRedaction.Treatment.MUTE_AUDIO,
        )
        with self.assertRaisesMessage(ValidationError, "marked responsive"):
            render_media_redactions(item.pk, actor=self.user, _client=self.stub)

    def test_render_refuses_with_nothing_marked(self):
        item = self._item("bus.mp4", "video/mp4")
        with self.assertRaisesMessage(ValidationError, "no active redactions"):
            render_media_redactions(item.pk, actor=self.user, _client=self.stub)

    def test_a_redaction_past_the_end_is_refused(self):
        item = self._item("bus.mp4", "video/mp4")
        self._redact(
            item, starts_at=10.0, ends_at=400.0,
            treatment=MediaRedaction.Treatment.MUTE_AUDIO,
        )
        with patch(
            "apps.records.media_review.probe_media",
            return_value={"duration_seconds": 60.0, "has_video": True,
                          "has_audio": True, "width": 640, "height": 480},
        ):
            with self.assertRaisesMessage(ValidationError, "past the end"):
                render_media_redactions(item.pk, actor=self.user, _client=self.stub)

    def test_a_rendered_copy_is_stored_without_touching_the_original(self):
        payload = b"\x00\x00\x00 ftypmp42 original"
        item = self._item("bus.mp4", "video/mp4", payload=payload)
        self._redact(
            item, starts_at=1.0, ends_at=2.0,
            treatment=MediaRedaction.Treatment.OBSCURE_REGION,
            x0=0.1, y0=0.1, x1=0.4, y1=0.4,
        )
        with patch(
            "apps.records.media_review.probe_media",
            return_value={"duration_seconds": 60.0, "has_video": True,
                          "has_audio": True, "width": 640, "height": 480},
        ), patch("apps.records.media_review._binary", return_value="/usr/bin/true"), \
             patch("apps.records.media_review.subprocess.run") as run, \
             patch("apps.records.media_review.Path.is_file", return_value=True), \
             patch("apps.records.media_review.Path.read_bytes",
                   return_value=b"redacted output"), \
             patch("apps.records.media_review.Path.write_bytes"):
            run.return_value = type("R", (), {"returncode": 0, "stderr": b""})()
            updated = render_media_redactions(
                item.pk, actor=self.user, _client=self.stub
            )

        self.assertTrue(updated.redacted_storage_key)
        self.assertNotEqual(updated.redacted_storage_key, updated.storage_key)
        # The original bytes are the record and must survive untouched.
        self.assertEqual(self.stub.objects[updated.storage_key], payload)
        self.assertEqual(
            updated.redacted_sha256, hashlib.sha256(b"redacted output").hexdigest()
        )
        entry = AuditLog.objects.filter(
            action="record.media_redactions_rendered"
        ).get()
        self.assertEqual(entry.detail["citations"], ["RCW 42.56.230(1)"])

    def test_the_render_re_encodes_rather_than_copying_the_stream(self):
        # A stream copy would change the container and leave every frame the
        # redaction was meant to remove.
        item = self._item("bus.mp4", "video/mp4")
        self._redact(
            item, starts_at=1.0, ends_at=2.0,
            treatment=MediaRedaction.Treatment.OBSCURE_FRAME,
        )
        with patch(
            "apps.records.media_review.probe_media",
            return_value={"duration_seconds": 60.0, "has_video": True,
                          "has_audio": True, "width": 640, "height": 480},
        ), patch("apps.records.media_review._binary", return_value="/usr/bin/true"), \
             patch("apps.records.media_review.subprocess.run") as run, \
             patch("apps.records.media_review.Path.is_file", return_value=True), \
             patch("apps.records.media_review.Path.read_bytes", return_value=b"out"), \
             patch("apps.records.media_review.Path.write_bytes"):
            run.return_value = type("R", (), {"returncode": 0, "stderr": b""})()
            render_media_redactions(item.pk, actor=self.user, _client=self.stub)
        command = run.call_args[0][0]
        self.assertIn("libx264", command)
        self.assertNotIn("copy", command)


class OpaqueDispositionTests(_Base):
    def _opaque(self):
        return self._item(
            "portable.swf", "application/octet-stream",
            payload=b"CWS\x08",
            review_state=RecordItem.ReviewState.UNREVIEWED,
        )

    def test_a_renderable_record_must_be_read_not_described(self):
        item = self._item(
            "memo.pdf", "application/pdf", payload=b"%PDF x",
            review_state=RecordItem.ReviewState.UNREVIEWED,
        )
        with self.assertRaisesMessage(ValidationError, "must be reviewed on its"):
            record_opaque_disposition(
                item.pk,
                basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
                reasoning="x" * 200,
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.user,
            )

    def test_thin_reasoning_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "at least"):
            record_opaque_disposition(
                self._opaque().pk,
                basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
                reasoning="cannot open",
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.user,
            )

    def test_withholding_still_requires_an_exemption(self):
        with self.assertRaisesMessage(ValidationError, "exemption citation"):
            record_opaque_disposition(
                self._opaque().pk,
                basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
                reasoning="y" * 200,
                review_state=RecordItem.ReviewState.WITHHELD,
                actor=self.user,
            )

    def test_a_disposition_records_who_decided_and_why(self):
        item = self._opaque()
        reasoning = (
            "Opened the archive on an isolated workstation with a standalone "
            "Flash projector. It contains 2017 compliance training slides "
            "with no student identifiers."
        )
        self.assertGreaterEqual(len(reasoning), MIN_OPAQUE_REASONING)
        disposition = record_opaque_disposition(
            item.pk,
            basis=OpaqueRecordDisposition.Basis.OPENED_ELSEWHERE,
            reasoning=reasoning,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.user,
        )
        item.refresh_from_db()
        self.assertEqual(item.review_state, RecordItem.ReviewState.RESPONSIVE)
        self.assertEqual(item.reviewed_by, self.user)
        self.assertEqual(disposition.decided_by, self.user)
        entry = AuditLog.objects.filter(
            action="record.opaque_disposition_recorded"
        ).get()
        self.assertEqual(entry.detail["basis"], "opened_elsewhere")

    def test_a_disposition_cannot_be_recorded_twice(self):
        item = self._opaque()
        record_opaque_disposition(
            item.pk,
            basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
            reasoning="z" * 200,
            review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            actor=self.user,
        )
        with self.assertRaisesMessage(ValidationError, "already has a recorded"):
            record_opaque_disposition(
                item.pk,
                basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
                reasoning="q" * 200,
                review_state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.user,
            )

    def test_an_anonymous_actor_cannot_decide(self):
        with self.assertRaisesMessage(ValidationError, "named reviewer"):
            record_opaque_disposition(
                self._opaque().pk,
                basis=OpaqueRecordDisposition.Basis.NOT_INSPECTABLE,
                reasoning="w" * 200,
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=None,
            )
