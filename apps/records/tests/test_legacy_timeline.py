"""A migrated case carries its conversation, not just its request row."""

import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.legacy_timeline import MARKER, parse_stamp
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    StatusEvent,
)

ACK = (
    "Requester + Staff\nMessage to requester\n\n"
    "Peninsula School District has received your Public Records Request.\n\n"
    "Pursuant to RCW 42.56.520, the District needs additional time.\n\n"
    "May 20, 2026, 9:09am"
)
FROM_REQ = (
    "Requester + Staff\nMessage from requester\n\n"
    "Hello, We prefer the electronic files. Please provide an invoice.\n\n"
    "June 18, 2026, 2:50pm by Sam Example, Paralegal Example Law via email"
)
NOTE = (
    "Staff Only\nNote\n\n"
    "Additional Requests for 26-19 are as follows: produce the sample file.\n\n"
    "May 26, 2026, 10:44am by Robin Clerk (Staff)"
)
DOC_NOISE = "Staff Only\nDocument(s) added\n\nfoo.pdf\n\nMay 1, 2026, 9:00am"


class ParseStampTests(TestCase):
    def test_reads_date_and_author(self):
        when, who = parse_stamp(FROM_REQ)
        self.assertEqual((when.year, when.month, when.day), (2026, 6, 18))
        self.assertEqual((when.hour, when.minute), (14, 50))
        self.assertIn("Sam Example", who)

    def test_reads_a_date_with_no_author(self):
        when, who = parse_stamp(ACK)
        self.assertEqual((when.month, when.day, when.hour), (5, 20, 9))
        self.assertEqual(who, "")

    def test_midnight_and_noon_do_not_collide(self):
        noon, _ = parse_stamp("x\n\nMay 1, 2026, 12:00pm")
        midnight, _ = parse_stamp("x\n\nMay 1, 2026, 12:00am")
        self.assertEqual(noon.hour, 12)
        self.assertEqual(midnight.hour, 0)

    def test_unparseable_tail_is_not_fatal(self):
        when, who = parse_stamp("just some text with no stamp")
        self.assertIsNone(when)
        self.assertEqual(who, "")


class ImportLegacyTimelineTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="ro", email="ro@psd401.net"
        )
        batch = LegacyImportBatch.objects.create(
            imported_by=user, source_sha256="0" * 64, mapping_sha256="1" * 64,
            plan_sha256="2" * 64, row_count=1, mapping_snapshot={},
        )
        requester = Requester.objects.create(name="R", email="r@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester, description="d", received_at=timezone.now()
        )
        LegacyRequestImport.objects.create(
            batch=batch, request=self.request, source_id="26-19",
            source_number="26-19", source_status="Open - responded",
            source_row_sha256="3" * 64,
        )

    def _run(self, payload, **kwargs):
        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        path = Path(directory) / "tl.json"
        path.write_text(json.dumps(payload))
        out = io.StringIO()
        call_command(
            "import_legacy_timeline", str(path),
            "--operator", "records-officer", stdout=out, **kwargs,
        )
        return out.getvalue()

    PAYLOAD = {"26-19": {"cards": [
        {"v": "Requester + Staff", "t": "Message to requester", "x": ACK},
        {"v": "Requester + Staff", "t": "Message from requester", "x": FROM_REQ},
        {"v": "Staff Only", "t": "Note", "x": NOTE},
        {"v": "Staff Only", "t": "Document(s) added", "x": DOC_NOISE},
    ]}}

    def test_dry_run_writes_nothing(self):
        out = self._run(self.PAYLOAD)
        self.assertIn("Entries to import    : 3", out)
        self.assertIn("Dry run only", out)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_document_noise_is_not_imported(self):
        self._run(self.PAYLOAD, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 3)
        self.assertFalse(
            StatusEvent.objects.filter(note__contains="Document(s) added").exists()
        )

    def test_message_bodies_arrive_in_full(self):
        self._run(self.PAYLOAD, commit=True)
        ack = StatusEvent.objects.get(note__contains="received your Public Records")
        self.assertIn("Pursuant to RCW 42.56.520", ack.note)
        self.assertIn(MARKER, ack.note)

    def test_requester_visibility_is_preserved(self):
        self._run(self.PAYLOAD, commit=True)
        self.assertTrue(
            StatusEvent.objects.get(note__contains="received your Public Records")
            .visible_to_requester
        )
        # An internal note must not become requester-visible in the move.
        self.assertFalse(
            StatusEvent.objects.get(note__contains="Additional Requests")
            .visible_to_requester
        )

    def test_entries_are_dated_when_they_happened(self):
        self._run(self.PAYLOAD, commit=True)
        ack = StatusEvent.objects.get(note__contains="received your Public Records")
        self.assertEqual((ack.occurred_at.year, ack.occurred_at.month), (2026, 5))
        self.assertNotEqual(ack.occurred_at, self.request.received_at)

    def test_the_author_is_recorded(self):
        self._run(self.PAYLOAD, commit=True)
        msg = StatusEvent.objects.get(note__contains="prefer the electronic files")
        self.assertIn("Sam Example", msg.note)

    def test_import_does_not_change_request_status(self):
        before = self.request.status
        self._run(self.PAYLOAD, commit=True)
        self.request.refresh_from_db()
        # Whether a migrated acknowledgment satisfies RCW 42.56.520 is an
        # officer's determination through the lifecycle service, not something
        # an importer may infer from parsed text.
        self.assertEqual(self.request.status, before)

    def test_running_twice_imports_nothing_new(self):
        self._run(self.PAYLOAD, commit=True)
        out = self._run(self.PAYLOAD, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 3)
        self.assertIn("already present", out)

    def test_an_unknown_source_request_blocks_the_run(self):
        with self.assertRaisesMessage(CommandError, "blocking issue"):
            self._run({"99-99": {"cards": [
                {"v": "Staff Only", "t": "Note", "x": NOTE}]}}, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_import_is_audited_per_case(self):
        self._run(self.PAYLOAD, commit=True)
        entry = AuditLog.objects.filter(
            action="request.legacy_timeline_imported"
        ).get()
        self.assertEqual(entry.detail["entries"], 3)

    def test_a_malformed_file_is_refused(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        path = Path(directory) / "bad.json"
        path.write_text("{nope")
        with self.assertRaisesMessage(CommandError, "not valid JSON"):
            call_command(
                "import_legacy_timeline", str(path),
                "--operator", "ro", stdout=io.StringIO(),
            )


class SupersedeNoResponseNoteTests(ImportLegacyTimelineTests):
    """The earlier no-response note was wrong; the timeline proves it."""

    GAP = (
        "NextRequest recorded NO initial response for source ID 26-19; its "
        "workflow status at export was 'Open - awaiting response'."
    )

    def _add_gap_note(self):
        StatusEvent.objects.create(
            request=self.request,
            occurred_at=self.request.received_at,
            note=self.GAP,
            visible_to_requester=False,
        )

    def test_a_case_with_a_message_gets_its_wrong_note_superseded(self):
        self._add_gap_note()
        out = self._run(self.PAYLOAD, commit=True)
        self.assertIn("Superseded 1", out)
        correction = StatusEvent.objects.get(note__contains="CORRECTION")
        self.assertIn("May 20, 2026", correction.note)
        self.assertIn("was wrong", correction.note)

    def test_the_wrong_note_is_left_in_place_not_removed(self):
        self._add_gap_note()
        self._run(self.PAYLOAD, commit=True)
        # Append-only: the record of the mistake stays, superseded.
        self.assertTrue(StatusEvent.objects.filter(note=self.GAP).exists())

    def test_a_case_without_the_wrong_note_gets_no_correction(self):
        self._run(self.PAYLOAD, commit=True)
        self.assertFalse(StatusEvent.objects.filter(note__contains="CORRECTION").exists())

    def test_correction_is_not_written_twice(self):
        self._add_gap_note()
        self._run(self.PAYLOAD, commit=True)
        self._run(self.PAYLOAD, commit=True)
        self.assertEqual(
            StatusEvent.objects.filter(note__contains="CORRECTION").count(), 1
        )

    def test_correction_is_audited(self):
        self._add_gap_note()
        self._run(self.PAYLOAD, commit=True)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.legacy_response_note_corrected"
            ).exists()
        )
