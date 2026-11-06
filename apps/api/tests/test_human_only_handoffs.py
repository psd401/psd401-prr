import uuid
from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class HumanOnlyRuntimeBoundaryTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.token = Token.objects.create(user=self.officer)
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records request.",
        )

    @property
    def auth(self) -> dict[str, Any]:
        # Any: these are WSGI environ extras, not the client's typed keywords.
        return {"HTTP_AUTHORIZATION": f"Token {self.token.key}"}

    def assert_handoff(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertIn("handoff", response.json())

    def test_legacy_direct_estimate_revision_is_not_routed(self):
        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/revise-estimate/",
            {
                "new_date": "2026-08-17",
                "reason": "An additional archive requires legal review.",
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.prr.refresh_from_db()
        self.assertIsNone(self.prr.estimated_completion)

    def test_token_cannot_transition_request_to_denied(self):
        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {"to_status": RequestStatus.DENIED, "note": "Draft only."},
            content_type="application/json",
            **self.auth,
        )
        self.assert_handoff(response)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_token_cannot_exclude_camera(self):
        preservation = FootagePreservation.objects.create(
            request=self.prr,
            window_start=timezone.now(),
            window_end=timezone.now(),
        )
        camera = CameraHold.objects.create(
            preservation=preservation,
            camera_id="camera-1",
            camera_name="Front entry",
        )
        response = self.client.post(
            f"/api/staff/preservations/{preservation.id}/exclude-camera/",
            {
                "camera_hold_id": camera.id,
                "reason": "The camera does not cover the requested location.",
            },
            content_type="application/json",
            **self.auth,
        )
        self.assert_handoff(response)

    def test_token_cannot_release_a_preservation(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
        )
        preservation = FootagePreservation.objects.create(
            request=self.prr,
            window_start=timezone.now(),
            window_end=timezone.now(),
            state=FootagePreservation.State.HELD,
        )
        response = self.client.post(
            f"/api/staff/preservations/{preservation.id}/release/",
            {
                "reason": "Request fulfilled and the requester confirmed receipt.",
                "attest_no_litigation_hold": True,
            },
            content_type="application/json",
            **self.auth,
        )
        self.assert_handoff(response)
        self.assertEqual(
            response.json()["handoff"]["capability_id"], "preservations.release",
        )
        preservation.refresh_from_db()
        self.assertEqual(preservation.state, FootagePreservation.State.HELD)

    def test_token_must_handoff_before_clarification_receipt_lookup(self):
        missing_receipt_id = uuid.uuid4()
        response = self.client.post(
            (
                "/api/staff/inbound-correspondence-receipts/"
                f"{missing_receipt_id}/associate-clarification/"
            ),
            {
                "clarification_cycle_id": str(uuid.uuid4()),
                "reason": (
                    "The officer reviewed this retained reply against the "
                    "open clarification."
                ),
                "expected_version": 1,
            },
            content_type="application/json",
            **self.auth,
        )

        self.assert_handoff(response)
        self.assertEqual(
            response.json()["handoff"]["capability_id"],
            "inbound_correspondence.associate_clarification_response",
        )
