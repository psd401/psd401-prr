# ABOUTME: Staff API tests for the footage-preservation resource — read-only
# ABOUTME: list/retrieve plus the gated exclude-camera action.
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
)

EXCLUDE_GATE_MESSAGE = (
    "Excluding a camera from a preservation hold requires a substantive "
    "reason (e.g. 'camera faces staff parking lot; request concerns the "
    "gym entrance'). The exclusion record is part of the adequate-search "
    "documentation."
)


class PreservationApiTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")

        requester = Requester.objects.create(email="jane@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester, description="camera footage of the gym entrance")
        self.fp = FootagePreservation.objects.create(
            request=self.prr,
            window_start=timezone.now() - dt.timedelta(days=2),
            window_end=timezone.now(),
            state=FootagePreservation.State.HELD,
            detected_dates_note="",
        )
        self.held = CameraHold.objects.create(
            preservation=self.fp, camera_id="cam-1", camera_name="Gym Entrance",
            site="PHS", clip_id="clip-1", state=CameraHold.State.HELD,
        )
        self.other = CameraHold.objects.create(
            preservation=self.fp, camera_id="cam-2", camera_name="Staff Parking",
            site="PHS", clip_id="clip-2", state=CameraHold.State.HELD,
        )

    def _rows(self, response):
        payload = response.json()
        return payload["results"] if isinstance(payload, dict) else payload

    # -- staff gate -----------------------------------------------------

    def test_staff_endpoint_refuses_anonymous(self):
        self.client.logout()
        r = self.client.get("/api/staff/preservations/")
        self.assertEqual(r.status_code, 403)

    def test_staff_endpoint_refuses_non_staff_session(self):
        self.client.logout()
        get_user_model().objects.create_user(
            "civilian", "civilian@example.com", "pw", is_staff=False)
        self.client.login(username="civilian", password="pw")
        r = self.client.get("/api/staff/preservations/")
        self.assertEqual(r.status_code, 403)

    # -- list / retrieve shape -------------------------------------------

    def test_list_shape(self):
        r = self.client.get("/api/staff/preservations/")
        self.assertEqual(r.status_code, 200)
        row = next(x for x in self._rows(r) if x["id"] == self.fp.id)
        self.assertEqual(row["request_number"], self.prr.number)
        self.assertEqual(row["state"], FootagePreservation.State.HELD)
        self.assertEqual(row["held_count"], 2)
        self.assertEqual(len(row["camera_holds"]), 2)
        hold = next(c for c in row["camera_holds"] if c["camera_id"] == "cam-1")
        self.assertEqual(hold["camera_name"], "Gym Entrance")
        self.assertEqual(hold["site"], "PHS")
        self.assertEqual(hold["clip_id"], "clip-1")
        self.assertEqual(hold["state"], CameraHold.State.HELD)
        self.assertIsNone(hold["excluded_by"])
        self.assertEqual(hold["exclusion_reason"], "")

    def test_retrieve_shape(self):
        r = self.client.get(f"/api/staff/preservations/{self.fp.id}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["id"], self.fp.id)
        self.assertEqual(body["request_number"], self.prr.number)
        self.assertEqual(body["detected_dates_note"], "")
        self.assertEqual(len(body["camera_holds"]), 2)

    def test_state_filter(self):
        expired_prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="old@example.org"),
            description="cctv footage of the parking lot months ago")
        expired = FootagePreservation.objects.create(
            request=expired_prr,
            window_start=timezone.now() - dt.timedelta(days=40),
            window_end=timezone.now() - dt.timedelta(days=30),
            state=FootagePreservation.State.EXPIRED,
        )
        r = self.client.get("/api/staff/preservations/?state=expired")
        ids = {row["id"] for row in self._rows(r)}
        self.assertIn(expired.id, ids)
        self.assertNotIn(self.fp.id, ids)

    def test_request_filter_returns_only_the_exact_case(self):
        other_prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="camera footage from another case",
        )
        other = FootagePreservation.objects.create(
            request=other_prr,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

        response = self.client.get(
            f"/api/staff/preservations/?request={self.prr.number}"
        )

        self.assertEqual(response.status_code, 200, response.content)
        ids = {row["id"] for row in self._rows(response)}
        self.assertEqual(ids, {self.fp.id})
        self.assertNotIn(other.id, ids)

    # -- exclude-camera action --------------------------------------------

    def test_exclude_camera_happy_path(self):
        r = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
            {"camera_hold_id": self.other.id,
             "reason": "Camera faces staff parking; request concerns the "
                       "gym entrance only."},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.other.refresh_from_db()
        self.assertEqual(self.other.state, CameraHold.State.RELEASED)
        self.assertEqual(self.other.excluded_by, self.officer)
        self.assertIn("gym entrance", self.other.exclusion_reason)
        self.assertIsNotNone(self.other.excluded_at)
        # The untouched camera is still held.
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, CameraHold.State.HELD)

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    @mock.patch("apps.integrations.avigilon.client.delete_bookmark")
    def test_exclude_camera_rechecks_current_role_after_dispatch(self, delete_bookmark):
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

        def revoke_role_after_permission_check(*_args, **_kwargs):
            apply_staff_roles(self.officer, set())

        with mock.patch(
            "apps.api.resources.preservations.human_only_handoff",
            side_effect=revoke_role_after_permission_check,
        ):
            response = self.client.post(
                f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
                {
                    "camera_hold_id": self.other.id,
                    "reason": (
                        "Camera faces staff parking; request concerns the "
                        "gym entrance only."
                    ),
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)
        delete_bookmark.assert_not_called()
        self.other.refresh_from_db()
        self.assertEqual(self.other.state, CameraHold.State.HELD)
        self.assertIsNone(self.other.excluded_at)
        self.assertIsNone(self.other.excluded_by_id)
        self.assertEqual(self.other.exclusion_reason, "")

    def test_exclude_camera_empty_reason_rejected(self):
        r = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
            {"camera_hold_id": self.held.id, "reason": ""},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], EXCLUDE_GATE_MESSAGE)
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, CameraHold.State.HELD)

    def test_exclude_camera_thin_reason_rejected(self):
        r = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
            {"camera_hold_id": self.held.id, "reason": "n/a"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], EXCLUDE_GATE_MESSAGE)
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, CameraHold.State.HELD)

    @mock.patch("apps.integrations.avigilon.client.delete_bookmark")
    def test_exclude_camera_oversized_reason_rejected_before_bookmark_delete(
        self,
        delete_bookmark,
    ):
        response = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
            {
                "camera_hold_id": self.held.id,
                "reason": "x" * 501,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "A camera-exclusion reason must contain at most 500 characters.",
        )
        delete_bookmark.assert_not_called()
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, CameraHold.State.HELD)
        self.assertIsNone(self.held.excluded_at)
        self.assertIsNone(self.held.excluded_by_id)
        self.assertEqual(self.held.exclusion_reason, "")

    def test_exclude_camera_unknown_hold_404(self):
        r = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
            {"camera_hold_id": 999999,
             "reason": "Valid, substantive reason for exclusion here."},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)

    # -- read-only enforcement --------------------------------------------

    def test_create_not_allowed(self):
        r = self.client.post("/api/staff/preservations/", {}, content_type="application/json")
        self.assertEqual(r.status_code, 405)

    def test_write_methods_rejected(self):
        url = f"/api/staff/preservations/{self.fp.id}/"
        self.assertEqual(
            self.client.put(url, {"state": "released"}, content_type="application/json").status_code, 405)
        self.assertEqual(
            self.client.patch(url, {"state": "released"}, content_type="application/json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
