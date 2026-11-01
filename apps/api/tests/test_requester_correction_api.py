# ABOUTME: Staff API tests for the officer-only requester-identity correction
# ABOUTME: endpoint — who may call it, what it refuses, and what it reports back.
import datetime as dt

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import Correspondence, CorrespondenceDelivery
from apps.records.models import PublicRecordsRequest, Requester

FORWARDED_REASON = (
    "Submitted by the district info mailbox on behalf of the actual "
    "requester, who is named in the forwarded email."
)


class RequesterCorrectionApiTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, [StaffRole.LIAISON])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="District Info Mailbox",
                email="info@psd401.net",
            ),
            description="All emails about the stadium project",
            estimated_completion=timezone.localdate() + dt.timedelta(days=30),
        )
        self.url = (
            f"/api/staff/requests/{self.prr.number}/correct-requester/"
        )
        self.client.force_login(self.officer)

    def _post(self, **overrides):
        payload = {
            "name": "Jane Rivera",
            "email": "jane@example.org",
            "reason": FORWARDED_REASON,
        }
        payload.update(overrides)
        return self.client.post(
            self.url, payload, content_type="application/json"
        )

    def test_an_officer_corrects_the_identity(self):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["requester"]["email"], "jane@example.org")
        self.assertEqual(body["requester"]["name"], "Jane Rivera")
        self.assertEqual(
            body["previous_requester"]["email"], "info@psd401.net"
        )
        self.assertEqual(
            body["request"]["requester"]["email"], "jane@example.org"
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "jane@example.org")

    def test_a_liaison_is_refused(self):
        self.client.force_login(self.liaison)

        response = self._post()

        self.assertEqual(response.status_code, 403)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "info@psd401.net")

    def test_an_anonymous_caller_is_refused(self):
        self.client.logout()

        response = self._post()

        self.assertEqual(response.status_code, 403)

    def test_the_readonly_agent_identity_is_refused(self):
        self.client.logout()
        agent = get_user_model().objects.create_user("mcp", is_staff=False)
        agent.user_permissions.add(
            Permission.objects.get(codename="read_staff_api")
        )
        token, _ = Token.objects.get_or_create(user=agent)

        response = self.client.post(
            self.url,
            {
                "name": "Jane Rivera",
                "email": "jane@example.org",
                "reason": FORWARDED_REASON,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 403)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "info@psd401.net")

    def test_a_short_reason_is_refused(self):
        response = self._post(reason="typo")

        self.assertEqual(response.status_code, 400)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "info@psd401.net")

    def test_a_malformed_email_is_refused(self):
        response = self._post(email="not-an-address")

        self.assertEqual(response.status_code, 400)

    def test_a_blank_name_is_refused(self):
        response = self._post(name="   ")

        self.assertEqual(response.status_code, 400)

    def test_an_unchanged_identity_is_refused(self):
        response = self._post(
            name="District Info Mailbox", email="info@psd401.net"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["code"], "invalid_requester_correction"
        )

    def test_the_response_reports_the_refreshed_draft(self):
        draft = correspondence_services.create_acknowledgment_draft(
            self.prr, actor=self.officer
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        refreshed = response.json()["refreshed_correspondence"]
        self.assertEqual([item["id"] for item in refreshed], [str(draft.id)])
        self.assertEqual(refreshed[0]["kind"], "acknowledgment")
        draft.refresh_from_db()
        self.assertIn("Dear Jane Rivera,", draft.draft_body)

    def test_the_response_reports_a_cancelled_approved_message(self):
        draft = correspondence_services.create_acknowledgment_draft(
            self.prr, actor=self.officer
        )
        correspondence_services.approve_draft(
            draft, actor=self.officer, expected_version=draft.version
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        cancelled = response.json()["cancelled_correspondence"]
        self.assertEqual([item["id"] for item in cancelled], [str(draft.id)])
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.CANCELLED)

    def test_the_response_reports_a_draft_needing_a_human_read(self):
        draft = correspondence_services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="About your request",
            body="District Info Mailbox, we are still gathering those emails.",
            actor=self.officer,
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [item["id"] for item in body["correspondence_needing_review"]],
            [str(draft.id)],
        )
        self.assertEqual(body["refreshed_correspondence"], [])

    def test_the_response_reports_a_message_already_in_delivery(self):
        draft = correspondence_services.create_acknowledgment_draft(
            self.prr,
            actor=self.officer,
        )
        correspondence_services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        draft.refresh_from_db()
        delivery = correspondence_services.queue_approved(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            status=CorrespondenceDelivery.Status.CLAIMED,
            claim_owner="worker-1",
            claim_expires_at=timezone.now() + dt.timedelta(minutes=5),
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cancelled_correspondence"], [])
        [reported] = body["uncancelled_correspondence"]
        self.assertEqual(reported["id"], str(draft.id))
        self.assertIn("cannot be cancelled", reported["detail"])
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.QUEUED)

    def test_the_correction_is_audited(self):
        self._post()

        self.assertTrue(
            AuditLog.objects.filter(
                action="request.requester_corrected",
                target=self.prr.number,
                actor=self.officer,
                detail__corrected_email="jane@example.org",
            ).exists()
        )
