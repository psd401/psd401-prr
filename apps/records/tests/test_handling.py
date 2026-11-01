"""A record's media type decides which review path is even possible."""

from django.test import TestCase

from apps.records.handling import (
    CONVERT,
    HANDLING_DETAIL,
    HANDLING_LABELS,
    MEDIA,
    OPAQUE,
    PREVIEW,
    handling_for,
)


class HandlingTests(TestCase):
    def test_pdf_is_the_only_path_that_reviews_today(self):
        self.assertEqual(handling_for("application/pdf", "a.pdf"), PREVIEW)

    def test_office_and_images_are_convertible(self):
        for content_type, name in (
            (
                (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                "memo.docx",
            ),
            ("application/msword", "memo.doc"),
            ("application/vnd.ms-excel", "budget.xls"),
            ("image/png", "screenshot.png"),
            ("image/jpeg", "photo.jpg"),
            ("text/plain", "notes.txt"),
            ("message/rfc822", "thread.eml"),
        ):
            self.assertEqual(handling_for(content_type, name), CONVERT, name)

    def test_video_and_audio_are_time_based_not_convertible(self):
        # Camera footage is central to this system, not an edge case. Calling
        # it "convertible" would imply a page raster could represent it.
        for content_type, name in (
            ("video/mp4", "bus-camera.mp4"),
            ("video/quicktime", "hallway.mov"),
            ("audio/mpeg", "call.mp3"),
            ("audio/wav", "interview.wav"),
        ):
            self.assertEqual(handling_for(content_type, name), MEDIA, name)

    def test_dead_formats_are_opaque_rather_than_optimistic(self):
        for name in ("portable.swf", "deck.keg", "theme.thmx", "app.nib"):
            self.assertEqual(handling_for("application/octet-stream", name), OPAQUE, name)

    def test_extension_decides_when_the_media_type_says_nothing(self):
        # Files that arrived from another system routinely carry a generic
        # type; the name is then the only evidence available.
        self.assertEqual(handling_for("application/octet-stream", "memo.docx"), CONVERT)
        self.assertEqual(handling_for("", "clip.mp4"), MEDIA)
        self.assertEqual(handling_for("application/octet-stream", "report.pdf"), PREVIEW)

    def test_media_type_wins_over_a_misleading_extension(self):
        self.assertEqual(handling_for("video/mp4", "evidence.pdf"), MEDIA)

    def test_unknown_is_opaque_not_assumed_reviewable(self):
        # Claiming a file is reviewable when nothing can open it invites a
        # reviewer to certify a record they never saw.
        self.assertEqual(handling_for("application/octet-stream", "mystery"), OPAQUE)
        self.assertEqual(handling_for("", ""), OPAQUE)

    def test_parameterised_media_types_are_normalized(self):
        self.assertEqual(handling_for("application/pdf; charset=binary", "a.pdf"), PREVIEW)
        self.assertEqual(handling_for("  APPLICATION/PDF  ", "a.pdf"), PREVIEW)

    def test_every_path_has_a_label_and_an_actionable_detail(self):
        for path in (PREVIEW, CONVERT, MEDIA, OPAQUE):
            self.assertTrue(HANDLING_LABELS[path])
            self.assertTrue(HANDLING_DETAIL[path])
