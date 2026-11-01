# ABOUTME: Staff intake API contract: least-privilege roles, canonical intake,
# ABOUTME: historical receipt time, attachment safety, and idempotent retries.
import datetime as dt
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    Channel,
    PublicRecordsRequest,
    StaffIntakeSubmission,
)

STAFF_INTAKE_URL = "/api/staff/requests/intake/"


class StaffIntakeApiTests(TestCase):
    def setUp(self):
        self.received_at = timezone.now() - dt.timedelta(hours=2)
        self.payload = {
            "idempotency_key": str(uuid.uuid4()),
            "channel": Channel.PHONE,
            "received_at": self.received_at.isoformat(),
            "name": "Phone Requester",
            "email": "",
            "phone": "253-555-0188",
            "street_address": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "company": "",
            "description": "Bus camera footage from yesterday afternoon.",
        }

    def make_staff(self, username, role):
        user = get_user_model().objects.create_user(
            username,
            f"{username}@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(user, [role])
        return user

    def post_as(self, user, payload=None):
        self.client.force_login(user)
        return self.client.post(
            STAFF_INTAKE_URL,
            payload or self.payload,
            content_type="application/json",
        )

    def test_anonymous_cannot_submit_staff_intake(self):
        response = self.client.post(
            STAFF_INTAKE_URL,
            self.payload,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PublicRecordsRequest.objects.exists())

    def test_only_intake_clerk_and_records_officer_roles_can_submit(self):
        allowed = (
            StaffRole.INTAKE_CLERK,
            StaffRole.RECORDS_OFFICER,
        )
        denied = (
            StaffRole.LIAISON,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        )

        for index, role in enumerate(allowed):
            with self.subTest(role=role):
                user = self.make_staff(f"allowed-{index}", role)
                payload = {**self.payload, "idempotency_key": str(uuid.uuid4())}
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.post_as(user, payload)
                self.assertEqual(response.status_code, 201)

        for index, role in enumerate(denied):
            with self.subTest(role=role):
                user = self.make_staff(f"denied-{index}", role)
                payload = {**self.payload, "idempotency_key": str(uuid.uuid4())}
                response = self.post_as(user, payload)
                self.assertEqual(response.status_code, 403)

    @patch("apps.records.intake.persist_intake_attachments")
    @patch("apps.records.intake.route_to_systems")
    @patch("apps.records.intake.triage_footage")
    @patch("apps.records.intake.compute_statutory_deadline.delay")
    def test_submission_uses_canonical_pipeline_and_attributes_audit(
        self,
        deadline,
        footage,
        routing,
        attachments,
    ):
        clerk = self.make_staff("intake-clerk", StaffRole.INTAKE_CLERK)

        response = self.post_as(clerk)

        self.assertEqual(response.status_code, 201)
        prr = PublicRecordsRequest.objects.get()
        self.assertEqual(response.json(), {"number": prr.number})
        self.assertEqual(prr.channel, Channel.PHONE)
        self.assertEqual(prr.received_at, self.received_at)
        self.assertEqual(prr.requester.email, "")
        self.assertEqual(prr.requester.phone, "253-555-0188")
        deadline.assert_called_once_with(str(prr.pk))
        footage.assert_called_once_with(prr)
        routing.assert_called_once_with(prr)
        attachments.assert_called_once_with(prr, [])
        event = AuditLog.objects.get(action="request.created")
        self.assertEqual(event.actor, clerk)
        self.assertEqual(event.target, prr.number)
        self.assertEqual(event.detail["integration_source"], "staff_intake")
        self.assertEqual(event.detail["idempotency_key"], self.payload["idempotency_key"])

    def test_same_actor_and_key_replays_same_request(self):
        officer = self.make_staff("officer", StaffRole.RECORDS_OFFICER)

        first = self.post_as(officer)
        second = self.post_as(officer)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        receipt = StaffIntakeSubmission.objects.get()
        self.assertEqual(receipt.submitted_by, officer)
        self.assertEqual(str(receipt.idempotency_key), self.payload["idempotency_key"])
        self.assertEqual(receipt.request.number, first.json()["number"])

    def test_same_actor_and_key_rejects_changed_payload(self):
        officer = self.make_staff("conflict-officer", StaffRole.RECORDS_OFFICER)
        first = self.post_as(officer)

        changed = self.post_as(
            officer,
            {**self.payload, "description": "A different legal request."},
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)

    def test_idempotency_keys_are_scoped_to_the_accountable_actor(self):
        first_clerk = self.make_staff("first-clerk", StaffRole.INTAKE_CLERK)
        second_clerk = self.make_staff("second-clerk", StaffRole.INTAKE_CLERK)

        first = self.post_as(first_clerk)
        second = self.post_as(second_clerk)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["number"], second.json()["number"])
        self.assertEqual(PublicRecordsRequest.objects.count(), 2)

    def test_phone_intake_accepts_phone_without_email(self):
        clerk = self.make_staff("phone-clerk", StaffRole.INTAKE_CLERK)

        response = self.post_as(clerk)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(PublicRecordsRequest.objects.get().requester.email, "")

    def test_intake_requires_at_least_one_response_method(self):
        clerk = self.make_staff("contact-clerk", StaffRole.INTAKE_CLERK)

        response = self.post_as(
            clerk,
            {
                **self.payload,
                "email": "",
                "phone": "",
                "street_address": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("non_field_errors", response.json())
        self.assertFalse(PublicRecordsRequest.objects.exists())

    def test_channel_is_limited_to_non_portal_staff_channels(self):
        clerk = self.make_staff("channel-clerk", StaffRole.INTAKE_CLERK)

        for channel in (Channel.PORTAL, Channel.EMAIL):
            with self.subTest(channel=channel):
                response = self.post_as(
                    clerk,
                    {
                        **self.payload,
                        "idempotency_key": str(uuid.uuid4()),
                        "channel": channel,
                    },
                )
                self.assertEqual(response.status_code, 400)

    def test_future_receipt_time_is_rejected(self):
        clerk = self.make_staff("time-clerk", StaffRole.INTAKE_CLERK)

        response = self.post_as(
            clerk,
            {
                **self.payload,
                "received_at": (timezone.now() + dt.timedelta(minutes=6)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("received_at", response.json())
        self.assertFalse(PublicRecordsRequest.objects.exists())

    @patch("apps.records.intake.persist_intake_attachments")
    def test_multipart_attachments_enter_the_canonical_quarantine_pipeline(
        self,
        persist,
    ):
        clerk = self.make_staff("paper-clerk", StaffRole.INTAKE_CLERK)
        self.client.force_login(clerk)
        upload = SimpleUploadedFile(
            "paper-request.pdf",
            b"%PDF-1.7\nscanned paper request",
            content_type="application/pdf",
        )
        payload = {
            **self.payload,
            "channel": Channel.MAIL,
            "attachments": [upload],
        }

        response = self.client.post(STAFF_INTAKE_URL, payload)

        self.assertEqual(response.status_code, 201)
        prr = PublicRecordsRequest.objects.get()
        persist.assert_called_once()
        persisted_request, persisted_uploads = persist.call_args.args
        self.assertEqual(persisted_request, prr)
        self.assertEqual(len(persisted_uploads), 1)
        self.assertEqual(
            persisted_uploads[0].original_filename,
            "paper-request.pdf",
        )

    def test_intake_clerk_cannot_browse_request_pii_after_submission(self):
        clerk = self.make_staff("write-only-clerk", StaffRole.INTAKE_CLERK)
        created = self.post_as(clerk)

        response = self.client.get("/api/staff/requests/")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(response.status_code, 403)

    @patch(
        "apps.records.intake.triage_footage",
        side_effect=RuntimeError("preservation provider unavailable"),
    )
    def test_failed_side_effect_retains_one_audited_request_for_reconciliation(
        self,
        _footage,
    ):
        clerk = self.make_staff("reconcile-clerk", StaffRole.INTAKE_CLERK)

        first = self.post_as(clerk)
        replay = self.post_as(clerk)

        self.assertEqual(first.status_code, 503)
        self.assertEqual(replay.status_code, 503)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        prr = PublicRecordsRequest.objects.get()
        self.assertEqual(first.json()["number"], prr.number)
        receipt = StaffIntakeSubmission.objects.get()
        self.assertEqual(receipt.state, StaffIntakeSubmission.State.FAILED)
        self.assertEqual(receipt.request, prr)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.created",
                target=prr.number,
                actor=clerk,
            ).exists()
        )


class StaffIntakeCapabilityTests(TestCase):
    def test_staff_intake_is_registered_as_attributed_high_risk_write(self):
        from apps.api.capabilities import CAPABILITIES

        capability = next(
            row for row in CAPABILITIES if row["id"] == "requests.submit_staff"
        )
        self.assertEqual(capability["method"], "POST")
        self.assertEqual(capability["path"], STAFF_INTAKE_URL)
        self.assertEqual(capability["execution"], "direct")
        self.assertEqual(capability["risk"], "high")
        self.assertIn("accountable_human_actor", capability["constraints"])
        self.assertIn("idempotent_submission", capability["constraints"])
        self.assertIn("pii_minimization", capability["constraints"])
        self.assertIn("malware_scanned", capability["constraints"])
