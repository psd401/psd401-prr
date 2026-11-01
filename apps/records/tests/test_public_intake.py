# ABOUTME: Acceptance and security tests for the anonymous, write-only public
# ABOUTME: request intake endpoint and its shared compliance pipeline.
import json
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import FileUploadHandler
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.correspondence import tasks as correspondence_tasks
from apps.correspondence.models import Correspondence, CorrespondenceDelivery
from apps.integrations.gmail.client import SentMessage
from apps.records.deadlines import statutory_response_due
from apps.records.models import (
    Channel,
    IntakeAttachment,
    PublicIntakeSubmission,
    PublicRecordsRequest,
    Requester,
)
from apps.records.public_tokens import (
    PUBLIC_FORM_COOKIE_NAME,
    issue_public_form_token,
)

PUBLIC_INTAKE_URL = "/api/public/requests/"
PUBLIC_FORM_PATH = "/request"


class RecordingUploadHandler(FileUploadHandler):
    bytes_seen = 0

    def receive_data_chunk(self, raw_data, start):
        type(self).bytes_seen += len(raw_data)
        return raw_data

    def file_complete(self, file_size):
        return None


@override_settings(
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
    PRR_PUBLIC_INTAKE_MIN_FILL_SECONDS=0,
    PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS=86_400,
    PRR_PUBLIC_INTAKE_PER_IP_RATE="1000/hour",
    PRR_PUBLIC_INTAKE_GLOBAL_RATE="10000/day",
)
class PublicIntakeEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(enforce_csrf_checks=True)
        self.payload = {
            "email": "jamie@example.org",
            "description": "Emails concerning the 2026 transportation plan.",
        }

    def open_form(self, *, client=None):
        client = client or self.client
        response = client.get(PUBLIC_FORM_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", client.cookies)
        self.assertIn("prr_public_intake", client.cookies)
        return client.cookies["csrftoken"].value

    def post_json(
        self,
        payload=None,
        *,
        client=None,
        include_csrf=True,
        remote_addr="203.0.113.10",
        forwarded_for=None,
    ):
        client = client or self.client
        csrf_token = self.open_form(client=client)
        headers = {"REMOTE_ADDR": remote_addr}
        if include_csrf:
            headers["HTTP_X_CSRFTOKEN"] = csrf_token
        if forwarded_for is not None:
            headers["HTTP_X_FORWARDED_FOR"] = forwarded_for
        return client.post(
            PUBLIC_INTAKE_URL,
            json.dumps(payload or self.payload),
            content_type="application/json",
            **headers,
        )

    def post_multipart(self, data, *, client=None, remote_addr="203.0.113.10"):
        client = client or self.client
        csrf_token = self.open_form(client=client)
        return client.post(
            PUBLIC_INTAKE_URL,
            data,
            HTTP_X_CSRFTOKEN=csrf_token,
            REMOTE_ADDR=remote_addr,
        )

    def test_request_page_issues_csrf_and_http_only_signed_form_cookies(self):
        response = self.client.get(PUBLIC_FORM_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("csrftoken", self.client.cookies)
        form_cookie = self.client.cookies["prr_public_intake"]
        self.assertTrue(form_cookie["httponly"])
        self.assertEqual(form_cookie["samesite"], "Strict")
        self.assertEqual(form_cookie["path"], PUBLIC_INTAKE_URL)

    def test_public_submission_requires_csrf_header(self):
        response = self.post_json(include_csrf=False)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PublicRecordsRequest.objects.exists())
        self.assertFalse(Requester.objects.exists())

    def test_public_endpoint_exposes_no_read_method(self):
        response = self.client.get(PUBLIC_INTAKE_URL)

        self.assertEqual(response.status_code, 405)
        self.assertFalse(PublicRecordsRequest.objects.exists())
        self.assertFalse(Requester.objects.exists())

    @mock.patch("apps.records.intake.route_to_systems")
    @mock.patch("apps.records.intake.triage_footage")
    def test_minimal_submission_uses_the_canonical_compliance_pipeline(
        self,
        triage_footage,
        route_to_systems,
    ):
        response = self.post_json()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {"number"})
        prr = PublicRecordsRequest.objects.get(number=response.json()["number"])
        self.assertEqual(prr.channel, Channel.PORTAL)
        self.assertEqual(prr.requester.email, self.payload["email"])
        self.assertEqual(prr.requester.name, "")
        self.assertEqual(prr.description, self.payload["description"])
        self.assertEqual(
            prr.statutory_response_due,
            statutory_response_due(prr.received_at),
        )
        triage_footage.assert_called_once_with(prr)
        route_to_systems.assert_called_once_with(prr)

        audit = AuditLog.objects.get(action="request.created", target=prr.number)
        self.assertIsNone(audit.actor)
        self.assertEqual(audit.ip_address, "203.0.113.10")
        self.assertEqual(audit.detail["integration_source"], "public_form")

        receipt_model = prr._meta.apps.get_model("records", "PublicIntakeSubmission")
        receipt = receipt_model.objects.get(request=prr)
        self.assertEqual(receipt.state, receipt_model.State.COMPLETED)
        self.assertEqual(len(receipt.token_sha256), 64)

        submission_receipt = Correspondence.objects.get(
            request=prr,
            kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        self.assertTrue(submission_receipt.automated)
        self.assertEqual(
            submission_receipt.status,
            Correspondence.Status.QUEUED,
        )
        self.assertTrue(
            CorrespondenceDelivery.objects.filter(
                correspondence=submission_receipt,
            ).exists()
        )

    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    @mock.patch("apps.records.intake.route_to_systems")
    @mock.patch("apps.records.intake.triage_footage")
    def test_disabled_outbound_transport_does_not_fail_legal_intake(
        self,
        _triage_footage,
        _route_to_systems,
    ):
        response = self.post_json()

        self.assertEqual(response.status_code, 201)
        prr = PublicRecordsRequest.objects.get(number=response.json()["number"])
        receipt = PublicIntakeSubmission.objects.get(request=prr)
        self.assertEqual(receipt.state, PublicIntakeSubmission.State.COMPLETED)
        self.assertFalse(
            Correspondence.objects.filter(
                request=prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).exists()
        )

    @override_settings(PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False)
    @mock.patch("apps.records.intake.route_to_systems")
    @mock.patch("apps.records.intake.triage_footage")
    def test_manual_only_mode_accepts_intake_without_an_automatic_receipt(
        self,
        _triage_footage,
        _route_to_systems,
    ):
        response = self.post_json()

        self.assertEqual(response.status_code, 201)
        prr = PublicRecordsRequest.objects.get(number=response.json()["number"])
        receipt = PublicIntakeSubmission.objects.get(request=prr)
        self.assertEqual(receipt.state, PublicIntakeSubmission.State.COMPLETED)
        self.assertFalse(
            Correspondence.objects.filter(
                request=prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).exists()
        )

    @override_settings(
        PRR_GMAIL_ENABLED=True,
        PRR_DEPLOYMENT_MODE="production",
        PRR_ENVIRONMENT="production",
    )
    @mock.patch("apps.records.intake.route_to_systems")
    @mock.patch("apps.records.intake.triage_footage")
    def test_public_submission_receipt_delivers_exactly_once(
        self,
        _triage_footage,
        _route_to_systems,
    ):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post_json()

        self.assertEqual(response.status_code, 201)
        delivery = CorrespondenceDelivery.objects.get(
            correspondence__request__number=response.json()["number"],
            correspondence__kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        with mock.patch(
            "apps.correspondence.tasks.gmail_client.send_raw",
            return_value=SentMessage(
                message_id="public-receipt-message",
                thread_id="public-receipt-thread",
            ),
        ) as send_raw:
            first = correspondence_tasks.send_queued_delivery.run(
                str(delivery.pk),
                attempt_count=0,
            )
            duplicate = correspondence_tasks.send_queued_delivery.run(
                str(delivery.pk),
                attempt_count=0,
            )

        self.assertEqual(first["status"], CorrespondenceDelivery.Status.SENT)
        self.assertEqual(duplicate["status"], "stale")
        send_raw.assert_called_once()

    def test_serializer_requires_only_email_and_description(self):
        from apps.records.public_serializers import PublicIntakeSerializer

        serializer = PublicIntakeSerializer(data=self.payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        fields = serializer.fields
        self.assertEqual(
            {name for name, field in fields.items() if field.required},
            {"email", "description"},
        )
        self.assertNotIn("purpose", fields)
        self.assertNotIn("reason", fields)
        self.assertNotIn("identity", fields)
        self.assertNotIn("id_upload", fields)

    def _abuse_response(
        self, *, payload=None, max_age=None, min_fill=None, age_the_token=False
    ):
        client = Client(enforce_csrf_checks=True)
        csrf_token = self.open_form(client=client)
        if age_the_token:
            # The token lifetime has a 60-second floor in the settings
            # registry, so a negative lifetime is no longer a configurable
            # way to express "already expired". Age the token instead, which
            # is what actually happens to a requester who leaves the form
            # open: re-sign it with a timestamp older than the lifetime.
            lifetime = runtime.get("PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS")
            with mock.patch(
                "apps.records.public_tokens.time.time",
                return_value=time.time() - lifetime - 1,
            ):
                client.cookies[PUBLIC_FORM_COOKIE_NAME] = (
                    issue_public_form_token()
                )
        settings = {}
        if max_age is not None:
            settings["PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS"] = max_age
        if min_fill is not None:
            settings["PRR_PUBLIC_INTAKE_MIN_FILL_SECONDS"] = min_fill
        with self.settings(**settings):
            with self.assertLogs("apps.records.public_views", level="WARNING") as logs:
                response = client.post(
                    PUBLIC_INTAKE_URL,
                    json.dumps(payload or self.payload),
                    content_type="application/json",
                    HTTP_X_CSRFTOKEN=csrf_token,
                    REMOTE_ADDR="203.0.113.20",
                )
        return response, "\n".join(logs.output)

    def test_honeypot_fast_and_expired_rejections_are_indistinguishable_but_logged(self):
        honeypot, honeypot_log = self._abuse_response(
            payload={**self.payload, "website": "https://spam.invalid"},
        )
        too_fast, too_fast_log = self._abuse_response(min_fill=60)
        expired, expired_log = self._abuse_response(age_the_token=True)

        for response in (honeypot, too_fast, expired):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.content, honeypot.content)
        self.assertIn("honeypot", honeypot_log)
        self.assertIn("too_fast", too_fast_log)
        self.assertIn("expired", expired_log)
        self.assertFalse(PublicRecordsRequest.objects.exists())

    @override_settings(
        PRR_PUBLIC_INTAKE_PER_IP_RATE="1/hour",
        PRR_PUBLIC_INTAKE_GLOBAL_RATE="100/day",
    )
    def test_per_ip_throttle_enforces_its_configured_limit(self):
        first = self.post_json(remote_addr="203.0.113.30")
        second = self.post_json(remote_addr="203.0.113.30")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)

    @override_settings(
        PRR_PUBLIC_INTAKE_PER_IP_RATE="100/hour",
        PRR_PUBLIC_INTAKE_GLOBAL_RATE="1/day",
    )
    def test_global_throttle_enforces_one_ceiling_across_source_ips(self):
        first = self.post_json(
            client=Client(enforce_csrf_checks=True),
            remote_addr="203.0.113.31",
        )
        second = self.post_json(
            client=Client(enforce_csrf_checks=True),
            remote_addr="203.0.113.32",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)

    def test_proxy_aware_per_ip_key_uses_the_rightmost_forwarded_address(self):
        from apps.records.throttles import PublicIntakePerIpThrottle

        request = RequestFactory().get(
            "/",
            REMOTE_ADDR="172.18.0.5",
            HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.40",
        )

        self.assertEqual(
            PublicIntakePerIpThrottle().get_ident(request),
            "203.0.113.40",
        )

    def test_replaying_the_same_form_creates_exactly_one_legal_request(self):
        csrf_token = self.open_form()
        first = self.client.post(
            PUBLIC_INTAKE_URL,
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
            REMOTE_ADDR="203.0.113.50",
        )
        second = self.client.post(
            PUBLIC_INTAKE_URL,
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
            REMOTE_ADDR="203.0.113.50",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        self.assertEqual(Requester.objects.count(), 1)

    @mock.patch("apps.records.attachments.scan_intake_attachment.delay")
    @mock.patch("apps.records.attachments.store_quarantined_attachment")
    def test_attachment_is_quarantined_scanned_and_not_downloadable_while_pending(
        self,
        store_attachment,
        queue_scan,
    ):
        upload = SimpleUploadedFile(
            "context.pdf",
            b"%PDF-1.7\nrequest context",
            content_type="application/pdf",
        )

        response = self.post_multipart({**self.payload, "attachments": [upload]})

        self.assertEqual(response.status_code, 201)
        attachment = IntakeAttachment.objects.get()
        self.assertEqual(attachment.scan_status, IntakeAttachment.ScanStatus.PENDING)
        self.assertTrue(attachment.storage_key.startswith("quarantine/intake/"))
        self.assertNotIn("context.pdf", attachment.storage_key)
        store_attachment.assert_called_once()
        queue_scan.assert_called_once_with(str(attachment.id))

        download = self.client.post(
            f"/api/staff/intake-attachments/{attachment.id}/download/",
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )
        self.assertIn(download.status_code, (401, 403))

    @override_settings(
        PRR_INTAKE_ATTACHMENT_MAX_BYTES=64,
        PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=64,
        FILE_UPLOAD_HANDLERS=(
            "apps.records.tests.test_public_intake.RecordingUploadHandler",
            "django.core.files.uploadhandler.MemoryFileUploadHandler",
            "django.core.files.uploadhandler.TemporaryFileUploadHandler",
        ),
    )
    def test_oversized_multipart_is_stopped_before_downstream_buffering(self):
        RecordingUploadHandler.bytes_seen = 0
        upload = SimpleUploadedFile(
            "large.pdf",
            b"%PDF-1.7\n" + (b"x" * 4096),
            content_type="application/pdf",
        )

        response = self.post_multipart({**self.payload, "attachments": [upload]})

        self.assertEqual(response.status_code, 413)
        self.assertEqual(RecordingUploadHandler.bytes_seen, 0)
        self.assertFalse(PublicRecordsRequest.objects.exists())

    @override_settings(PRR_INTAKE_ATTACHMENT_MAX_COUNT=1)
    def test_too_many_multipart_files_are_rejected_before_legal_intake(self):
        uploads = [
            SimpleUploadedFile(
                f"context-{index}.pdf",
                b"%PDF-1.7\nrequest context",
                content_type="application/pdf",
            )
            for index in range(2)
        ]

        response = self.post_multipart(
            {**self.payload, "attachments": uploads},
        )

        self.assertEqual(response.status_code, 413)
        self.assertFalse(PublicRecordsRequest.objects.exists())


class PublicIntakeCapabilityTests(TestCase):
    def test_public_submission_is_a_registered_anonymous_write_capability(self):
        from apps.api.capabilities import CAPABILITIES

        capability = next(
            row for row in CAPABILITIES if row["id"] == "requests.submit_public"
        )
        self.assertEqual(capability["method"], "POST")
        self.assertEqual(capability["path"], PUBLIC_INTAKE_URL)
        self.assertEqual(capability["execution"], "anonymous")
        self.assertEqual(capability["risk"], "high")
        self.assertIn("anonymous_write_only", capability["constraints"])
        self.assertIn("pii_minimization", capability["constraints"])
        self.assertIn("malware_scanned", capability["constraints"])


@override_settings(PRR_PUBLIC_INTAKE_PROCESSING_STALE_MINUTES=15)
class RecoverStalePublicIntakeSubmissionTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_user(
            "public-recovery-officer",
            is_staff=True,
        )

    def create_receipt(self, *, linked=True, age_minutes=30):
        prr = None
        if linked:
            requester = Requester.objects.create(email="public-recovery@example.org")
            prr = PublicRecordsRequest.objects.create(
                requester=requester,
                description="Records needed for public recovery testing.",
            )
        receipt = PublicIntakeSubmission.objects.create(
            token_sha256="b" * 64,
            payload_sha256="c" * 64,
            request=prr,
        )
        PublicIntakeSubmission.objects.filter(pk=receipt.pk).update(
            created_at=timezone.now() - timedelta(minutes=age_minutes),
        )
        receipt.refresh_from_db()
        return receipt

    def test_operator_can_mark_a_stale_linked_public_receipt_failed(self):
        receipt = self.create_receipt()
        output = StringIO()

        call_command(
            "recover_stale_intake",
            str(receipt.pk),
            confirm=True,
            operator=self.operator.username,
            reason="Host was terminated after public request creation.",
            stdout=output,
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.state, PublicIntakeSubmission.State.FAILED)
        self.assertIn("Host was terminated", receipt.last_error)
        self.assertIn(receipt.request.number, output.getvalue())
        self.assertIn("same signed form", output.getvalue())
        event = AuditLog.objects.get(action="public.intake_recovered")
        self.assertEqual(event.target, receipt.request.number)
        self.assertEqual(event.actor, self.operator)
        self.assertEqual(event.detail["receipt_id"], receipt.pk)
        self.assertEqual(event.detail["source"], "public_form")

    def test_public_recovery_refuses_a_receipt_that_is_not_stale(self):
        receipt = self.create_receipt(age_minutes=1)

        with self.assertRaisesMessage(CommandError, "not stale"):
            call_command(
                "recover_stale_intake",
                str(receipt.pk),
                confirm=True,
                operator=self.operator.username,
                reason="This must be refused while public intake may still be active.",
            )

        receipt.refresh_from_db()
        self.assertEqual(receipt.state, PublicIntakeSubmission.State.PROCESSING)

    def test_unlinked_public_recovery_requires_reconciliation_confirmation(self):
        receipt = self.create_receipt(linked=False)

        with self.assertRaisesMessage(CommandError, "--confirm-unlinked"):
            call_command(
                "recover_stale_intake",
                str(receipt.pk),
                confirm=True,
                operator=self.operator.username,
                reason="The public intake process ended before request linkage.",
            )

        call_command(
            "recover_stale_intake",
            str(receipt.pk),
            confirm=True,
            confirm_unlinked=True,
            operator=self.operator.username,
            reason="Public form data was reconciled against the request queue.",
            stdout=StringIO(),
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, PublicIntakeSubmission.State.FAILED)
        event = AuditLog.objects.get(action="public.intake_recovered")
        self.assertEqual(event.target, f"public-intake-receipt:{receipt.pk}")
