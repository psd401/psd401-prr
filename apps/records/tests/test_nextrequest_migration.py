"""Fail-closed planning, import, activation, and cutover checks for NextRequest."""

from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestActivation,
    LegacyRequestImport,
    PublicRecordsRequest,
    RequestStatus,
)
from apps.records.nextrequest_migration import (
    activate_legacy_request,
    assess_nextrequest_cutover,
    commit_nextrequest_plan,
    plan_nextrequest_import,
)

MAPPING = {
    "version": 1,
    "source_timezone": "America/Los_Angeles",
    "columns": {
        "source_id": "Request ID",
        "source_number": "Request Number",
        "description": "Request Text",
        "received_at": "Received",
        "status": "Status",
        "status_changed_at": "Status Changed",
        "closed_at": "Closed",
        "closure_reason": "Closure Reason",
        "initial_response_at": "Initial Response",
        "estimated_completion": "Estimated Completion",
        "attachment_count": "Attachments",
        "channel": "Channel",
        "name": "Requester Name",
        "email": "Requester Email",
        "phone": "Requester Phone",
        "mailing_address": "Mailing Address",
        "company": "Company",
    },
    "status_map": {
        "Open": "gathering",
        "Review": "review",
        "Fulfilled": "fulfilled",
        "Acknowledged": "acknowledged",
    },
    "channel_map": {"Web": "portal", "Email": "email"},
}


def csv_bytes(*rows):
    header = (
        "Request ID,Request Number,Request Text,Received,Status,Status Changed,"
        "Closed,Closure Reason,Initial Response,Estimated Completion,Attachments,"
        "Channel,Requester Name,Requester Email,Requester Phone,Mailing Address,Company"
    )
    return ("\n".join((header, *rows)) + "\n").encode()


OPEN_ROW = (
    '41,NR-41,"Board meeting minutes",2025-02-03 09:15,Open,'
    "2025-02-05 10:00,,,2025-02-04 14:00,2025-02-28,0,Web,"
    "Taylor Reed,taylor@example.org,253-555-0100,,"
)


class NextRequestMigrationTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "migration-officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            "migration-reviewer",
            "reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})

    def test_plan_is_deterministic_normalized_and_has_no_database_effect(self):
        first = plan_nextrequest_import(csv_bytes(OPEN_ROW), MAPPING)
        second = plan_nextrequest_import(csv_bytes(OPEN_ROW), MAPPING)

        self.assertTrue(first.ready)
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.rows[0].target_status, RequestStatus.GATHERING)
        self.assertEqual(
            first.rows[0].received_at,
            datetime(2025, 2, 3, 9, 15, tzinfo=timezone.get_current_timezone()),
        )
        self.assertEqual(PublicRecordsRequest.objects.count(), 0)
        self.assertEqual(LegacyImportBatch.objects.count(), 0)

    def test_a_z_suffixed_timestamp_is_read_as_utc(self):
        """NextRequest exports write `2025-02-03T17:15:00Z`, and the planner
        reads it with `datetime.fromisoformat`, which only learned the `Z`
        suffix in Python 3.11. `pyproject.toml` pins 3.12, so this passes --
        and it is here so that a change to that pin fails a test about the
        legal record's received date rather than failing in production."""
        row = (
            '44,NR-44,"Budget records",2025-02-03T17:15:00Z,Open,'
            "2025-02-05 10:00,,,2025-02-04 14:00,2025-02-28,0,Web,"
            "Taylor Reed,taylor@example.org,253-555-0100,,"
        )

        plan = plan_nextrequest_import(csv_bytes(row), MAPPING)

        self.assertTrue(plan.ready, plan.issues)
        self.assertEqual(
            plan.rows[0].received_at,
            datetime(2025, 2, 3, 17, 15, tzinfo=UTC),
        )

    def test_plan_fails_closed_for_delivery_states_attachments_and_bad_rows(self):
        rows = (
            (
                '42,NR-42,"Email records",2025-02-03 09:15,Acknowledged,'
                "2025-02-04 10:00,,,2025-02-04 10:00,,0,Email,Pat,pat@example.org,,,"
            ),
            (
                '43,NR-43,"Incident records",2025-02-03 09:15,Open,'
                "2025-02-05 10:00,,,2025-02-04 10:00,,2,Web,Lee,lee@example.org,,,"
            ),
            (
                '43,NR-44,"Duplicate source id",not-a-date,Open,'
                "2025-02-05 10:00,,,2025-02-04 10:00,,0,Web,Sam,sam@example.org,,,"
            ),
        )
        plan = plan_nextrequest_import(csv_bytes(*rows), MAPPING)

        self.assertFalse(plan.ready)
        joined = " ".join(issue.message for issue in plan.issues)
        self.assertIn("delivery-backed status", joined)
        self.assertIn("attachments", joined)
        self.assertIn("duplicate source ID", joined)
        self.assertIn("Received", joined)

        with self.assertRaisesMessage(ValidationError, "blocked rows"):
            commit_nextrequest_plan(
                plan,
                actor=self.officer,
                expected_plan_sha256=plan.plan_sha256,
            )

    @patch("apps.records.nextrequest_migration.route_to_systems")
    @patch("apps.records.nextrequest_migration.triage_footage")
    def test_commit_is_exactly_gated_attributed_idempotent_and_side_effect_free(
        self,
        triage_footage,
        route_to_systems,
    ):
        plan = plan_nextrequest_import(csv_bytes(OPEN_ROW), MAPPING)

        with self.assertRaises(PermissionDenied):
            commit_nextrequest_plan(
                plan,
                actor=self.reviewer,
                expected_plan_sha256=plan.plan_sha256,
            )
        with self.assertRaisesMessage(ValidationError, "plan digest"):
            commit_nextrequest_plan(
                plan,
                actor=self.officer,
                expected_plan_sha256="0" * 64,
            )

        batch = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )
        repeated = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )

        self.assertEqual(batch, repeated)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        imported = LegacyRequestImport.objects.get(batch=batch)
        request = imported.request
        self.assertEqual(request.description, "Board meeting minutes")
        self.assertEqual(request.status, RequestStatus.GATHERING)
        self.assertEqual(request.channel, "portal")
        self.assertEqual(request.requester.email, "taylor@example.org")
        self.assertIsNone(request.estimated_completion)
        self.assertEqual(imported.source_number, "NR-41")
        self.assertEqual(
            imported.legacy_estimated_completion.isoformat(),
            "2025-02-28",
        )
        self.assertTrue(request.initial_response_delivered)
        self.assertTrue(request.events.filter(
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.GATHERING,
            occurred_at=imported.status_changed_at,
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action="legacy.nextrequest_request_imported",
            target=request.number,
            actor=self.officer,
            detail__source_id="41",
        ).exists())
        triage_footage.assert_not_called()
        route_to_systems.assert_not_called()

    def test_terminal_import_requires_historic_closure_evidence(self):
        bad = (
            '51,NR-51,"Closed case",2024-01-03 09:00,Fulfilled,'
            "2024-01-10 12:00,2024-01-10 12:00,,2024-01-04 09:00,,0,Web,"
            "Jordan,jordan@example.org,,,"
        )
        blocked = plan_nextrequest_import(csv_bytes(bad), MAPPING)
        self.assertFalse(blocked.ready)
        self.assertIn("closure reason", " ".join(i.message for i in blocked.issues))

        good = bad.replace(",,2024-01-04", ",Records provided in full,2024-01-04")
        plan = plan_nextrequest_import(csv_bytes(good), MAPPING)
        batch = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )
        request = batch.request_imports.get().request
        self.assertEqual(request.status, RequestStatus.FULFILLED)
        self.assertEqual(
            request.closed_at,
            datetime(2024, 1, 10, 12, 0, tzinfo=timezone.get_current_timezone()),
        )

    @patch("apps.records.nextrequest_migration.route_to_systems", return_value=[object()])
    @patch("apps.records.nextrequest_migration.triage_footage", return_value=None)
    def test_open_request_activation_is_explicit_and_idempotent(
        self,
        triage_footage,
        route_to_systems,
    ):
        plan = plan_nextrequest_import(csv_bytes(OPEN_ROW), MAPPING)
        batch = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )
        imported = batch.request_imports.get()

        activation = activate_legacy_request(imported.pk, actor=self.officer)
        repeated = activate_legacy_request(imported.pk, actor=self.officer)

        self.assertEqual(activation, repeated)
        self.assertEqual(LegacyRequestActivation.objects.count(), 1)
        self.assertEqual(activation.collection_tasks_created, 1)
        triage_footage.assert_called_once_with(imported.request)
        route_to_systems.assert_called_once_with(imported.request)

    def test_cutover_report_requires_exact_import_and_activation(self):
        plan = plan_nextrequest_import(csv_bytes(OPEN_ROW), MAPPING)
        before = assess_nextrequest_cutover(plan)
        self.assertFalse(before.ready)
        self.assertIn("not been committed", " ".join(before.blockers))

        batch = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )
        after_import = assess_nextrequest_cutover(plan)
        self.assertFalse(after_import.ready)
        self.assertIn("not activated", " ".join(after_import.blockers))

        with patch("apps.records.nextrequest_migration.triage_footage", return_value=None), patch(
            "apps.records.nextrequest_migration.route_to_systems", return_value=[]
        ):
            activate_legacy_request(batch.request_imports.get().pk, actor=self.officer)
        ready = assess_nextrequest_cutover(plan)
        self.assertTrue(ready.ready)
        self.assertEqual(ready.blockers, ())


ACKNOWLEDGEMENT = {
    "acknowledged": True,
    "source_system": "NextRequest (peninsulaschooldistrictwa.nextrequest.com)",
    "reason": (
        "Source files stay in the read-only vendor system until they are moved "
        "through the scanned attachment manifest."
    ),
}

RETAINED_MAPPING = {**MAPPING, "attachments_retained_in_source": ACKNOWLEDGEMENT}

ATTACHMENT_ROW = (
    '51,NR-51,"Investigation records",2025-02-03 09:15,Open,'
    "2025-02-05 10:00,,,2025-02-04 14:00,,7,Web,"
    "Jamie Fox,jamie@example.org,,,"
)


class RetainedAttachmentAcknowledgementTests(TestCase):
    """Attachments may stay in the source only through an explicit statement."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "retain-officer",
            "retain-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def test_attachments_still_block_without_the_acknowledgement(self):
        """The default must stay fail-closed; this is the whole safety rail."""
        plan = plan_nextrequest_import(csv_bytes(ATTACHMENT_ROW), MAPPING)

        self.assertFalse(plan.ready)
        self.assertIn(
            "attachments",
            " ".join(issue.message for issue in plan.issues),
        )

    def test_acknowledgement_must_be_complete_and_substantive(self):
        for label, block in (
            ("not an object", "yes"),
            ("not acknowledged", {**ACKNOWLEDGEMENT, "acknowledged": False}),
            ("no source system", {**ACKNOWLEDGEMENT, "source_system": "  "}),
            ("thin reason", {**ACKNOWLEDGEMENT, "reason": "files stay put"}),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValidationError):
                    plan_nextrequest_import(
                        csv_bytes(ATTACHMENT_ROW),
                        {**MAPPING, "attachments_retained_in_source": block},
                    )

    def test_acknowledged_rows_import_with_the_true_count_recorded(self):
        plan = plan_nextrequest_import(csv_bytes(ATTACHMENT_ROW), RETAINED_MAPPING)
        self.assertTrue(plan.ready)
        # The true count is inside the row digest, so an approved plan cannot
        # be swapped for one that understates the files left behind.
        self.assertEqual(plan.rows[0].attachment_count, 7)

        batch = commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )

        imported = LegacyRequestImport.objects.get(batch=batch)
        self.assertEqual(imported.source_attachment_count, 7)
        # No file was created anywhere: this records absence, not migration.
        self.assertEqual(imported.request.intake_attachments.count(), 0)

    def test_the_case_timeline_tells_the_officer_the_files_are_elsewhere(self):
        """An audit row nobody opens is not how a case gets worked."""
        plan = plan_nextrequest_import(csv_bytes(ATTACHMENT_ROW), RETAINED_MAPPING)
        commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )

        request = PublicRecordsRequest.objects.get()
        notes = " ".join(event.note for event in request.events.all())
        self.assertIn("7 source file(s) were NOT migrated", notes)
        self.assertIn("peninsulaschooldistrictwa.nextrequest.com", notes)

    def test_the_batch_audit_records_what_was_left_behind(self):
        plan = plan_nextrequest_import(csv_bytes(ATTACHMENT_ROW), RETAINED_MAPPING)
        commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )

        entry = AuditLog.objects.get(action="legacy.nextrequest_batch_imported")
        self.assertEqual(entry.detail["rows_with_retained_attachments"], 1)
        self.assertEqual(entry.detail["retained_attachment_files"], 7)

    def test_cutover_names_every_request_whose_files_did_not_move(self):
        plan = plan_nextrequest_import(csv_bytes(ATTACHMENT_ROW), RETAINED_MAPPING)
        commit_nextrequest_plan(
            plan,
            actor=self.officer,
            expected_plan_sha256=plan.plan_sha256,
        )
        activate_legacy_request(
            LegacyRequestImport.objects.get().pk,
            actor=self.officer,
        )

        report = assess_nextrequest_cutover(plan)

        self.assertTrue(report.ready)
        self.assertTrue(
            any("7 file(s) in the source system" in warning for warning in report.warnings),
            report.warnings,
        )
