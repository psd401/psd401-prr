"""A migrated case that was never answered has to say so, not stay silent."""

import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    StatusEvent,
)


class LegacyResponseGapTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="ro", email="ro@psd401.net"
        )
        self.batch = LegacyImportBatch.objects.create(
            imported_by=self.user,
            source_sha256="0" * 64,
            mapping_sha256="1" * 64,
            plan_sha256="2" * 64,
            row_count=2,
            mapping_snapshot={},
        )

    def _case(self, number, *, responded_at=None, source_status="Open - awaiting response"):
        requester = Requester.objects.create(
            name=f"R{number}", email=f"r{number}@example.org"
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"records for {number}",
            received_at=timezone.now(),
        )
        LegacyRequestImport.objects.create(
            batch=self.batch,
            request=request,
            source_id=number,
            source_number=number,
            source_status=source_status,
            source_row_sha256="3" * 64,
            legacy_initial_response_at=responded_at,
        )
        return request

    def _run(self, **kwargs):
        out = io.StringIO()
        call_command(
            "annotate_legacy_response_gap",
            "--operator", "records-officer",
            stdout=out, **kwargs,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        request = self._case("26-45")
        output = self._run()
        self.assertIn("to annotate       : 1", output)
        self.assertIn("Dry run only", output)
        self.assertEqual(StatusEvent.objects.filter(request=request).count(), 0)

    def test_an_unanswered_case_gets_an_explicit_statement(self):
        request = self._case("26-45")
        self._run(commit=True)
        event = StatusEvent.objects.get(request=request)
        self.assertIn("recorded NO initial response for source ID 26-45", event.note)
        self.assertIn("Open - awaiting response", event.note)
        self.assertIn("five-business-day", event.note)
        self.assertFalse(event.visible_to_requester)

    def test_the_note_does_not_assert_nobody_replied(self):
        # A response sent by direct email and never logged would not appear in
        # an export. The note has to report what the source recorded, and tell
        # the officer to go looking, rather than declare a fact it cannot know.
        request = self._case("26-45")
        self._run(commit=True)
        note = StatusEvent.objects.get(request=request).note
        self.assertIn("If a response was sent outside that system", note)

    def test_an_answered_case_is_left_alone(self):
        request = self._case(
            "26-19",
            responded_at=timezone.now(),
            source_status="Open - responded",
        )
        output = self._run(commit=True)
        self.assertIn("to annotate       : 0", output)
        self.assertEqual(StatusEvent.objects.filter(request=request).count(), 0)

    def test_the_note_sits_with_the_other_import_notes(self):
        request = self._case("26-45")
        self._run(commit=True)
        event = StatusEvent.objects.get(request=request)
        # Dated to receipt, not to today, so the timeline reads in the order
        # things actually happened.
        self.assertEqual(event.occurred_at, request.received_at)

    def test_running_twice_does_not_duplicate_the_note(self):
        request = self._case("26-45")
        self._run(commit=True)
        self._run(commit=True)
        self.assertEqual(StatusEvent.objects.filter(request=request).count(), 1)

    def test_every_annotation_is_audited(self):
        self._case("26-45")
        self._run(commit=True)
        entry = AuditLog.objects.filter(
            action="request.legacy_response_gap_recorded"
        ).get()
        self.assertEqual(entry.detail["source_id"], "26-45")
        self.assertEqual(entry.detail["source_status"], "Open - awaiting response")

    def test_the_retaining_system_is_named(self):
        request = self._case("26-45")
        self._run(commit=True, retaining_system="NextRequest (psd.nextrequest.com)")
        self.assertIn(
            "NextRequest (psd.nextrequest.com) recorded NO initial response",
            StatusEvent.objects.get(request=request).note,
        )

    def test_an_empty_operator_is_refused(self):
        self._case("26-45")
        with self.assertRaises(CommandError):
            call_command(
                "annotate_legacy_response_gap",
                "--operator", "   ",
                stdout=io.StringIO(),
            )
