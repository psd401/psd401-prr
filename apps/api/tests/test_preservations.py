# ABOUTME: Staff API tests for the footage-preservation resource — read-only
# ABOUTME: list/retrieve plus the gated exclude-camera action.
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records import triage
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
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
        self.assertIn("coverage_fraction", hold)
        self.assertEqual(hold["coverage_note"], "")
        self.assertEqual(row["site_codes"], [])
        self.assertEqual(row["site_resolution"], "")
        self.assertFalse(row["request_closed"])
        self.assertIsNone(row["released_at"])
        self.assertIsNone(row["released_by"])
        self.assertEqual(row["release_reason"], "")
        self.assertTrue(
            row["confirmation_text"].startswith(
                "2 cameras across the district held from "
            ),
            row["confirmation_text"],
        )

    def test_confirmation_text_is_null_when_nothing_is_held(self):
        FootagePreservation.objects.filter(pk=self.fp.pk).update(
            state=FootagePreservation.State.MANUAL,
        )
        body = self.client.get(f"/api/staff/preservations/{self.fp.id}/").json()
        self.assertIsNone(body["confirmation_text"])

    def test_request_closed_reflects_the_request_status(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
        )
        body = self.client.get(f"/api/staff/preservations/{self.fp.id}/").json()
        self.assertTrue(body["request_closed"])

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

    @mock.patch("apps.integrations.avigilon.client.delete_bookmark")
    def test_exclude_camera_happy_path(self, delete_bookmark):
        with self.captureOnCommitCallbacks(execute=True):
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

    # -- automatic release ------------------------------------------------

    def _close_request(self, status=RequestStatus.FULFILLED, closed_at=None):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=status, closed_at=closed_at or timezone.now(),
        )

    @mock.patch("apps.integrations.avigilon.client.delete_bookmark")
    def test_there_is_no_release_action(self, delete_bookmark):
        # Release is automatic (rule 7): no staff click, token, or agent path.
        self._close_request()
        response = self.client.post(
            f"/api/staff/preservations/{self.fp.id}/release/",
            {
                "reason": "Request fulfilled; the requester confirmed receipt.",
                "attest_no_litigation_hold": True,
            },
            content_type="application/json",
        )
        self.assertIn(response.status_code, (404, 405))
        delete_bookmark.assert_not_called()
        self.fp.refresh_from_db()
        self.assertEqual(self.fp.state, FootagePreservation.State.HELD)

    def test_release_eligible_at_is_computed_by_the_server(self):
        url = f"/api/staff/preservations/{self.fp.id}/"
        body = self.client.get(url).json()
        self.assertNotIn("releasable", body)
        self.assertIsNone(body["release_eligible_at"])   # open

        closed = timezone.now().replace(microsecond=0) - dt.timedelta(days=1)
        self._close_request(closed_at=closed)
        self.assertEqual(
            dt.datetime.fromisoformat(
                self.client.get(url).json()["release_eligible_at"],
            ),
            closed,
        )

        self._close_request(RequestStatus.DENIED, closed_at=closed)
        self.assertEqual(
            dt.datetime.fromisoformat(
                self.client.get(url).json()["release_eligible_at"],
            ),
            closed + triage.PRA_SUIT_WINDOW,
        )
        self._close_request(closed_at=closed)

        other = PublicRecordsRequest.objects.create(
            requester=self.prr.requester, description="case of record")
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(merged_into=other)
        self.assertIsNone(self.client.get(url).json()["release_eligible_at"])
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(merged_into=None)

        FootagePreservation.objects.filter(pk=self.fp.pk).update(
            state=FootagePreservation.State.RELEASED,
        )
        CameraHold.objects.filter(preservation=self.fp).update(
            state=CameraHold.State.RELEASED,
        )
        self.assertIsNone(self.client.get(url).json()["release_eligible_at"])
        CameraHold.objects.filter(pk=self.held.pk).update(
            state=CameraHold.State.RELEASE_FAILED,
        )
        self.assertIsNotNone(
            self.client.get(url).json()["release_eligible_at"],
        )   # retry

    @mock.patch("apps.integrations.avigilon.client.delete_bookmark")
    def test_non_text_exclusion_reasons_are_400_not_500(self, delete_bookmark):
        self._close_request()
        for reason in (["a list is not a reason"], {"a": "b"}, 1234567890123):
            with self.subTest(reason=reason):
                response = self.client.post(
                    f"/api/staff/preservations/{self.fp.id}/exclude-camera/",
                    {"camera_hold_id": self.other.id, "reason": reason},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        delete_bookmark.assert_not_called()

    # -- widen action --------------------------------------------------------

    WIDEN_REASON = "Request also names the bus loop beside the gym."

    def _narrow_to_phs(self):
        FootagePreservation.objects.filter(pk=self.fp.pk).update(site_codes=["PHS"])

    def _widen(self, reason: object = WIDEN_REASON):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/staff/preservations/{self.fp.id}/widen/",
                {"reason": reason},
                content_type="application/json",
            )

    @mock.patch("apps.records.tasks.execute_footage_hold.delay")
    def test_widen_happy_path_returns_the_updated_preservation(self, delay):
        self._narrow_to_phs()

        response = self._widen()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["id"], self.fp.id)
        self.assertEqual(body["site_codes"], [])
        self.assertFalse(body["widenable"])
        delay.assert_called_once_with(self.fp.id, only_new=True, widened_from=["PHS"])

    @mock.patch("apps.records.tasks.execute_footage_hold.delay")
    def test_widen_rejects_a_thin_or_non_string_reason(self, delay):
        self._narrow_to_phs()
        for reason in ("short", ["a list is not a reason"], {"a": "b"}, 1234567890123):
            with self.subTest(reason=reason):
                response = self._widen(reason=reason)
                self.assertEqual(response.status_code, 400)
        delay.assert_not_called()
        self.fp.refresh_from_db()
        self.assertEqual(self.fp.site_codes, ["PHS"])

    @mock.patch("apps.records.tasks.execute_footage_hold.delay")
    def test_widen_refuses_an_already_district_wide_hold(self, delay):
        response = self._widen()
        self.assertEqual(response.status_code, 400)
        delay.assert_not_called()

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    @mock.patch("apps.records.tasks.execute_footage_hold.delay")
    def test_widen_is_records_officer_only(self, delay):
        self._narrow_to_phs()
        apply_staff_roles(self.officer, {StaffRole.ADMIN})

        response = self._widen()

        self.assertEqual(response.status_code, 403)
        delay.assert_not_called()

    def test_widenable_reflects_scope_and_release(self):
        url = f"/api/staff/preservations/{self.fp.id}/"
        self.assertFalse(self.client.get(url).json()["widenable"])
        self._narrow_to_phs()
        self.assertTrue(self.client.get(url).json()["widenable"])
        FootagePreservation.objects.filter(pk=self.fp.pk).update(
            state=FootagePreservation.State.RELEASED,
        )
        self.assertFalse(self.client.get(url).json()["widenable"])

    @mock.patch("apps.records.tasks.execute_footage_hold.delay")
    def test_widen_is_refused_while_the_hold_is_still_running(self, delay):
        self._narrow_to_phs()
        FootagePreservation.objects.filter(pk=self.fp.pk).update(
            state=FootagePreservation.State.PENDING,
        )
        url = f"/api/staff/preservations/{self.fp.id}/"
        self.assertFalse(self.client.get(url).json()["widenable"])

        response = self._widen()

        self.assertEqual(response.status_code, 400)
        delay.assert_not_called()

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

    # -- error text --------------------------------------------------------

    INTERNAL_TEXT = "internal detail at /srv/app/apps/records/triage.py"

    def _post_each_action(self):
        self._close_request()
        self._narrow_to_phs()
        base = f"/api/staff/preservations/{self.fp.id}"
        bodies = {
            "exclude_camera": ("exclude-camera", {
                "camera_hold_id": self.other.id,
                "reason": "Camera faces staff parking; request concerns the gym entrance only.",
            }),
            "widen_preservation": ("widen", {"reason": self.WIDEN_REASON}),
        }
        for function, (path, body) in bodies.items():
            yield function, lambda path=path, body=body: self.client.post(
                f"{base}/{path}/", body, content_type="application/json"
            )

    def test_a_refusal_shows_staff_its_message(self):
        for function, post in self._post_each_action():
            with self.subTest(action=function), mock.patch(
                f"apps.records.triage.{function}",
                side_effect=triage.PreservationRefused("Choose a closed request."),
            ):
                response = post()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "Choose a closed request."})

    def test_an_unexpected_error_is_never_echoed_to_staff(self):
        self.client.raise_request_exception = False
        for function, post in self._post_each_action():
            with self.subTest(action=function), mock.patch(
                f"apps.records.triage.{function}",
                side_effect=ValueError(self.INTERNAL_TEXT),
            ):
                response = post()
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("srv/app", response.content.decode())
