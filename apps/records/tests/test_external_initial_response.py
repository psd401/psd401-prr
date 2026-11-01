# ABOUTME: A first response sent through NextRequest, district email, or mail is recorded
# ABOUTME: on a Records Officer's statement; the bulk command backfills the migrated set.
import csv
import datetime as dt
import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records import services
from apps.records.models import (
    InitialResponseAttestation,
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    awaiting_initial_response,
)

EVIDENCE = (
    "Acknowledged in NextRequest on receipt; copy in the officer's "
    "NextRequest notification mailbox."
)


def _received(year, month, day):
    return timezone.make_aware(dt.datetime(year, month, day, 9, 0))


class ExternalInitialResponseServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.clerk = User.objects.create_user(
            "clerk", "clerk@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.clerk, {StaffRole.INTAKE_CLERK})
        requester = Requester.objects.create(
            email="jane@example.org", name="Jane Q. Public"
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board minutes",
            received_at=_received(2026, 6, 1),
        )

    def test_statement_records_the_response_and_clears_the_backlog(self):
        self.assertFalse(self.prr.initial_response_delivered)
        self.assertTrue(
            awaiting_initial_response(
                PublicRecordsRequest.objects.filter(pk=self.prr.pk)
            ).exists()
        )

        attestation = services.record_external_initial_response(
            self.prr,
            actor=self.officer,
            sent_via="nextrequest",
            evidence=EVIDENCE,
            responded_on=dt.date(2026, 6, 3),
        )

        self.prr.refresh_from_db()
        self.assertTrue(self.prr.initial_response_delivered)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertFalse(
            awaiting_initial_response(
                PublicRecordsRequest.objects.filter(pk=self.prr.pk)
            ).exists()
        )
        self.assertEqual(attestation.attested_by, self.officer)
        self.assertEqual(attestation.responded_on, dt.date(2026, 6, 3))
        event = self.prr.events.get(
            note__contains="Initial response recorded as sent outside this system"
        )
        self.assertEqual(event.actor, self.officer)
        self.assertFalse(event.visible_to_requester)
        self.assertIn("2026-06-03", event.note)
        self.assertIn(EVIDENCE, event.note)
        entry = AuditLog.objects.get(
            action="request.initial_response_attested", target=self.prr.number
        )
        self.assertEqual(entry.actor, self.officer)
        self.assertEqual(entry.detail["sent_via"], "nextrequest")
        self.assertEqual(entry.detail["responded_on"], "2026-06-03")

    def test_the_date_may_be_unknown(self):
        services.record_external_initial_response(
            self.prr,
            actor=self.officer,
            sent_via="nextrequest",
            evidence=EVIDENCE,
        )

        self.prr.refresh_from_db()
        self.assertTrue(self.prr.initial_response_delivered)
        event = self.prr.events.get(note__contains="a date not on record")
        self.assertEqual(event.actor, self.officer)

    def test_requires_a_records_officer(self):
        with self.assertRaises(PermissionDenied):
            services.record_external_initial_response(
                self.prr,
                actor=self.clerk,
                sent_via="nextrequest",
                evidence=EVIDENCE,
            )
        self.assertFalse(InitialResponseAttestation.objects.exists())

    def test_requires_a_statement(self):
        with self.assertRaises(ValidationError):
            services.record_external_initial_response(
                self.prr,
                actor=self.officer,
                sent_via="nextrequest",
                evidence="done",
            )
        with self.assertRaises(ValidationError):
            services.record_external_initial_response(
                self.prr,
                actor=self.officer,
                sent_via="carrier_pigeon",
                evidence=EVIDENCE,
            )

    def test_refuses_a_second_statement(self):
        services.record_external_initial_response(
            self.prr, actor=self.officer, sent_via="mail", evidence=EVIDENCE
        )
        with self.assertRaises(ValidationError) as caught:
            services.record_external_initial_response(
                self.prr, actor=self.officer, sent_via="mail", evidence=EVIDENCE
            )
        self.assertIn("already has a first response", str(caught.exception))

    def test_refuses_dates_before_receipt_or_in_the_future(self):
        with self.assertRaises(ValidationError):
            services.record_external_initial_response(
                self.prr,
                actor=self.officer,
                sent_via="nextrequest",
                evidence=EVIDENCE,
                responded_on=dt.date(2026, 5, 31),
            )
        with self.assertRaises(ValidationError):
            services.record_external_initial_response(
                self.prr,
                actor=self.officer,
                sent_via="nextrequest",
                evidence=EVIDENCE,
                responded_on=timezone.localdate() + dt.timedelta(days=1),
            )
        self.assertFalse(InitialResponseAttestation.objects.exists())

    def test_the_statement_is_append_only(self):
        attestation = services.record_external_initial_response(
            self.prr, actor=self.officer, sent_via="mail", evidence=EVIDENCE
        )
        attestation.evidence = "Something else entirely, rewritten later."
        with self.assertRaises(ValidationError):
            attestation.save()
        with self.assertRaises(ValidationError):
            attestation.delete()


class AttestCommandTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.clerk = User.objects.create_user(
            "clerk", "clerk@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.clerk, {StaffRole.INTAKE_CLERK})
        self.batch = LegacyImportBatch.objects.create(
            imported_by=self.officer,
            source_sha256="0" * 64,
            mapping_sha256="1" * 64,
            plan_sha256="2" * 64,
            row_count=3,
            mapping_snapshot={},
        )
        self.unanswered_a = self._migrated("26-1", _received(2026, 6, 1))
        self.unanswered_b = self._migrated("26-2", _received(2026, 6, 2))
        self.answered = self._migrated(
            "26-3", _received(2026, 6, 3), legacy_response=_received(2026, 6, 4)
        )
        self.web_case = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email="web@example.org", name="Web Form"
            ),
            description="Not migrated at all",
            received_at=_received(2026, 9, 1),
        )

    def _migrated(self, source_number, received_at, *, legacy_response=None):
        requester = Requester.objects.create(
            email=f"{source_number}@example.org", name=source_number
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"records for {source_number}",
            received_at=received_at,
        )
        LegacyRequestImport.objects.create(
            batch=self.batch,
            request=request,
            source_id=source_number,
            source_number=source_number,
            source_status="Open",
            source_row_sha256="3" * 64,
            legacy_initial_response_at=legacy_response,
        )
        return request

    def _run(self, *args, attested_by=None):
        out = io.StringIO()
        call_command(
            "attest_external_initial_responses",
            "--attested-by",
            attested_by or self.officer.username,
            *args,
            stdout=out,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self._run("--remaining-migrated")

        self.assertIn("Dry run", output)
        self.assertIn(self.unanswered_a.number, output)
        self.assertNotIn(self.answered.number, output)
        self.assertFalse(InitialResponseAttestation.objects.exists())

    def test_remaining_migrated_records_every_unanswered_migrated_case(self):
        output = self._run("--remaining-migrated", "--commit")

        self.assertIn("Recorded 2; failed 0.", output)
        for request in (self.unanswered_a, self.unanswered_b):
            request.refresh_from_db()
            self.assertTrue(request.initial_response_delivered)
            attestation = request.initial_response_attestation
            self.assertIsNone(attestation.responded_on)
            self.assertEqual(attestation.sent_via, "nextrequest")
            self.assertIn("before the 2026-08-31 cutover", attestation.evidence)
            self.assertEqual(attestation.attested_by, self.officer)
        # Already answered in the source, and never migrated: both untouched.
        self.assertFalse(
            InitialResponseAttestation.objects.filter(
                request__in=[self.answered, self.web_case]
            ).exists()
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="request.initial_response_attested"
            ).count(),
            2,
        )

    def test_csv_rows_carry_their_own_dates_and_unknown_rows_are_skipped(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", newline="", delete=False
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["number", "responded_on", "sent_via", "evidence"]
            )
            writer.writeheader()
            writer.writerow(
                {
                    "number": "26-1",
                    "responded_on": "2026-06-02",
                    "sent_via": "",
                    "evidence": EVIDENCE,
                }
            )
            writer.writerow(
                {
                    "number": self.unanswered_b.number.lower(),
                    "responded_on": "",
                    "sent_via": "district_email",
                    "evidence": "Replied from the records mailbox; copy in Sent.",
                }
            )
            writer.writerow(
                {"number": "99-999", "responded_on": "", "sent_via": "", "evidence": ""}
            )
            path = handle.name
        try:
            output = self._run("--csv", path, "--commit")
        finally:
            Path(path).unlink()

        self.assertIn("99-999", output)
        self.assertIn("no such case", output)
        self.unanswered_a.refresh_from_db()
        self.unanswered_b.refresh_from_db()
        a = self.unanswered_a.initial_response_attestation
        b = self.unanswered_b.initial_response_attestation
        self.assertEqual(a.responded_on, dt.date(2026, 6, 2))
        self.assertEqual(a.sent_via, "nextrequest")
        self.assertIsNone(b.responded_on)
        self.assertEqual(b.sent_via, "district_email")

    def test_archive_uses_the_earliest_district_message(self):
        archive = {
            "closed_requests": {
                "26-1": {
                    "timeline": {
                        "timeline": [
                            {
                                "timeline_id": "3",
                                "timeline_name": "External Message",
                                "timeline_byline": (
                                    "June 30, 2026,  9:03am by Robin Clerk (Staff)"
                                ),
                                "timeline_state": "Requester + Staff",
                            },
                            {
                                "timeline_id": "2",
                                "timeline_name": "External Message",
                                "timeline_byline": "June 26, 2026, 10:25am by Casey Staff",
                                "timeline_state": "Requester + Staff",
                            },
                            {
                                "timeline_id": "1",
                                "timeline_name": "External Message",
                                "timeline_byline": "June 23, 2026, 12:02pm",
                                "timeline_state": "Requester + Staff",
                            },
                            {
                                "timeline_id": "0",
                                "timeline_name": "Request Opened",
                                "timeline_byline": "June 1, 2026, 9:00am",
                                "timeline_state": "Requester + Staff",
                            },
                        ]
                    }
                },
                "26-2": {"timeline": {"timeline": []}},
                "26-3": {"timeline": {"timeline": []}},
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(archive, handle)
            path = handle.name
        try:
            output = self._run("--archive", path, "--commit")
        finally:
            Path(path).unlink()

        # The staff-authored message wins over the tenant's automated one,
        # and the requester's own message never counts.
        self.unanswered_a.refresh_from_db()
        attestation = self.unanswered_a.initial_response_attestation
        self.assertEqual(attestation.responded_on, dt.date(2026, 6, 30))
        self.assertIn("staff message", attestation.evidence)
        self.assertIn("timeline id 3", attestation.evidence)
        self.assertIn("no district message in the archive", output)
        self.assertFalse(
            InitialResponseAttestation.objects.filter(
                request__in=[self.unanswered_b, self.answered]
            ).exists()
        )

    def test_refuses_a_user_who_is_not_a_records_officer(self):
        with self.assertRaises(CommandError):
            self._run("--remaining-migrated", "--commit", attested_by="clerk")
        self.assertFalse(InitialResponseAttestation.objects.exists())
