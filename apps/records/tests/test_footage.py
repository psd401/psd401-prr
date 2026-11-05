"""Footage triage tests — auto-hold always, auto-deny never."""
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.integrations.avigilon import client as alta
from apps.records import tasks, triage
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
)

FAKE_CAMERAS = [
    {"id": "cam-1", "name": "Gym Entrance", "site": "PHS"},
    {"id": "cam-2", "name": "Staff Parking", "site": "PHS"},
    {"id": "cam-3", "name": "Main Office", "site": "KMS"},
]


def make_request(description: str):
    r = Requester.objects.create(email="jane@example.org")
    return PublicRecordsRequest.objects.create(requester=r, description=description)


class DetectionTests(TestCase):
    def test_footage_keywords(self):
        self.assertTrue(triage.is_footage_request("security camera footage from the gym"))
        self.assertTrue(triage.is_footage_request("any VIDEO recorded near the office"))
        self.assertFalse(triage.is_footage_request("all emails about the stadium project"))

    def test_date_extraction_formats(self):
        text = "footage from 2026-06-25 and also 6/26/2026 and June 27th, 2026"
        self.assertEqual(triage.extract_dates(text), [
            dt.date(2026, 6, 25), dt.date(2026, 6, 26), dt.date(2026, 6, 27),
        ])

    def test_non_footage_request_returns_none(self):
        prr = make_request("all emails about the stadium project")
        self.assertIsNone(triage.triage_footage(prr))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CAMERA_RETENTION_DAYS=10)
class HoldPathTests(TestCase):
    @mock.patch("apps.integrations.avigilon.client.create_clip_hold", return_value="clip-x")
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS)
    def test_hold_triage_retry_reuses_durable_preservation(self, m_list, m_clip):
        day = (timezone.localdate() - dt.timedelta(days=3)).isoformat()
        prr = make_request(f"camera footage near the office on {day}")

        first = triage.triage_footage(prr)
        second = triage.triage_footage(prr)

        self.assertEqual(first, second)
        self.assertEqual(FootagePreservation.objects.filter(request=prr).count(), 1)
        self.assertEqual(m_list.call_count, 1)
        self.assertEqual(m_clip.call_count, len(FAKE_CAMERAS))

    @mock.patch("apps.integrations.avigilon.client.create_clip_hold", return_value="clip-x")
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS)
    def test_recent_footage_holds_every_camera(self, m_list, m_clip):
        day = (timezone.localdate() - dt.timedelta(days=3)).isoformat()
        prr = make_request(f"camera footage of the gym entrance on {day}")
        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.camera_holds.count(), 3)          # broad by default
        self.assertEqual(m_clip.call_count, 3)
        self.assertEqual(fp.held_count, 3)
        # Officer sees a review prompt on the timeline
        self.assertTrue(prr.events.filter(note__icontains="exclude irrelevant").exists())

    @mock.patch("apps.integrations.avigilon.client.list_cameras")
    def test_no_parseable_date_holds_full_retention_window(self, m_list):
        m_list.return_value = FAKE_CAMERAS
        with mock.patch("apps.integrations.avigilon.client.create_clip_hold", return_value="c"):
            prr = make_request("I want the surveillance video of the incident at the gym")
            fp = triage.triage_footage(prr)
        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        window_days = (fp.window_end - fp.window_start).days
        self.assertGreaterEqual(window_days, 9)               # ~full retention held
        self.assertIn("No dates parseable", fp.detected_dates_note)

    def test_expired_window_queues_review_never_denies(self):
        old = (timezone.localdate() - dt.timedelta(days=30)).isoformat()
        prr = make_request(f"cctv footage of the parking lot on {old}")
        fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.EXPIRED)
        self.assertEqual(fp.camera_holds.count(), 0)          # no pointless holds
        # Request is NOT denied — status untouched, checklist queued for a human
        prr.refresh_from_db()
        self.assertEqual(prr.status, "received")
        checklist = prr.events.get()
        for phrase in ("HUMAN MUST COMPLETE", "incident-hold", "email & Drive"):
            self.assertIn(phrase, checklist.note)
        self.assertIn("HUMAN REVIEW REQUIRED", checklist.note)
        self.assertNotIn("response queued", checklist.note)

    @mock.patch("apps.integrations.avigilon.client.list_cameras",
                side_effect=__import__("apps.integrations.avigilon.client",
                                       fromlist=["AltaNotConfigured"]).AltaNotConfigured("no token"))
    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_api_unconfigured_degrades_to_urgent_manual_hold(
        self,
        notify_manual_hold,
        m_list,
    ):
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"video from the main office camera on {day}")
        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()
        self.assertEqual(notify_manual_hold.call_args.args[0].pk, fp.pk)

    @override_settings(
        ALTA_API_BASE="http://alta.psd401.internal",
        ALTA_API_TOKEN="configured-but-never-sent",
    )
    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_invalid_api_configuration_degrades_to_urgent_manual_hold(
        self,
        notify_manual_hold,
    ):
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"video from the main office camera on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()
        self.assertTrue(
            prr.events.filter(
                note__icontains="URGENT manual hold notification",
            ).exists()
        )

    @mock.patch("apps.integrations.avigilon.client.create_clip_hold", return_value="clip-x")
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS)
    def test_completed_hold_records_durable_timeline_evidence(self, _m_list, _m_clip):
        day = (timezone.localdate() - dt.timedelta(days=3)).isoformat()
        prr = make_request(f"PRIVATE gym camera narrative on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.held_count, 3)
        self.assertTrue(
            prr.events.filter(
                note__icontains="3 camera(s) held, 0 failed",
            ).exists()
        )

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_transport_error_degrades_to_urgent_manual_hold(self, notify_manual_hold):
        # A timeout or HTTP error from Aware must never leave the hold PENDING
        # with nobody told: the retention clock keeps running (rule 7).
        import requests

        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"video from the main office camera on {day}")
        with mock.patch(
            "apps.integrations.avigilon.client.list_cameras",
            side_effect=requests.ConnectionError("connector unreachable"),
        ):
            fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=[])
    def test_zero_cameras_is_never_reported_held(self, _m_list, notify_manual_hold):
        # Harbor Heights, 2026-09-23: nothing to hold must not read as "Held".
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"all camera footage at the school on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertNotEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.held_count, 0)
        notify_manual_hold.assert_called_once()

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    @mock.patch(
        "apps.integrations.avigilon.client.create_clip_hold",
        side_effect=RuntimeError("bookmark rejected"),
    )
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS)
    def test_every_camera_failing_degrades_to_urgent_manual_hold(
        self, _m_list, _m_clip, notify_manual_hold,
    ):
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"camera footage of the gym on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        self.assertEqual(fp.held_count, 0)
        notify_manual_hold.assert_called_once()

    @mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete")
    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=[])
    def test_zero_cameras_records_reason_and_sends_no_completion(
        self, _m_list, notify_manual_hold, notify_complete,
    ):
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"all camera footage at the school on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_complete.assert_not_called()
        self.assertIn("zero cameras", notify_manual_hold.call_args.kwargs["error"])
        self.assertTrue(
            prr.events.filter(
                note__icontains="zero cameras — URGENT manual hold notification",
            ).exists()
        )

    @mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete")
    @mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold")
    @mock.patch(
        "apps.integrations.avigilon.client.create_clip_hold",
        side_effect=["clip-1", RuntimeError("bookmark rejected"), "clip-3"],
    )
    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS)
    def test_some_cameras_failing_stays_partial(
        self, _m_list, _m_clip, notify_manual_hold, notify_complete,
    ):
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"camera footage of the gym on {day}")

        fp = triage.triage_footage(prr)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(fp.held_count, 2)
        notify_manual_hold.assert_not_called()
        notify_complete.assert_called_once_with(fp, held=2, failed=1)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CAMERA_RETENTION_DAYS=10,
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
)
class ExclusionGateTests(TestCase):
    def _records_officer(self):
        officer = get_user_model().objects.create_user(
            f"footage-officer-{get_user_model().objects.count()}",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        return officer

    def _held_camera(self):
        with mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=FAKE_CAMERAS), \
             mock.patch("apps.integrations.avigilon.client.create_clip_hold", return_value="clip-x"):
            day = (timezone.localdate() - dt.timedelta(days=1)).isoformat()
            prr = make_request(f"gym entrance footage {day}")
            fp = triage.triage_footage(prr)
        return fp.camera_holds.get(camera_id="cam-2")   # Staff Parking

    def test_exclusion_requires_substantive_reason(self):
        ch = self._held_camera()
        officer = self._records_officer()
        for bad in ("", "   ", "n/a", None):
            with self.assertRaises(ValueError):
                triage.exclude_camera(ch, bad, actor=officer)
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)

    @mock.patch("apps.records.triage.audit")
    @mock.patch("apps.integrations.avigilon.client.delete_clip")
    def test_exclusion_rejects_oversized_reason_before_clip_delete(
        self,
        delete_clip,
        audit,
    ):
        ch = self._held_camera()
        officer = self._records_officer()
        audit.reset_mock()

        with self.assertRaisesMessage(
            ValueError,
            "at most 500 characters",
        ):
            triage.exclude_camera(
                ch,
                "x" * 501,
                actor=officer,
            )

        delete_clip.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch("apps.integrations.avigilon.client.delete_clip")
    def test_exclusion_does_not_delete_clip_when_audit_fails(
        self,
        delete_clip,
        audit,
    ):
        ch = self._held_camera()
        officer = self._records_officer()
        audit.reset_mock()
        audit.side_effect = RuntimeError("audit persistence failed")

        with self.assertRaisesMessage(
            RuntimeError,
            "audit persistence failed",
        ):
            triage.exclude_camera(
                ch,
                (
                    "Camera faces staff parking; request concerns the "
                    "gym entrance only."
                ),
                actor=officer,
            )

        delete_clip.assert_not_called()
        audit.assert_called_once()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch("apps.integrations.avigilon.client.delete_clip")
    def test_exclusion_requires_a_current_records_officer(
        self,
        delete_clip,
        audit,
    ):
        ch = self._held_camera()
        administrator = get_user_model().objects.create_user(
            "footage-application-admin",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(administrator, {StaffRole.ADMIN})
        audit.reset_mock()

        for actor in (None, administrator):
            with (
                self.subTest(actor=getattr(actor, "username", None)),
                self.assertRaisesMessage(
                    PermissionDenied,
                    (
                        "Current Records Officer authority is required to "
                        "exclude a camera."
                    ),
                ),
            ):
                triage.exclude_camera(
                    ch,
                    (
                        "Camera faces staff parking; request concerns the "
                        "gym entrance only."
                    ),
                    actor=actor,
                )

        delete_clip.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch("apps.integrations.avigilon.client.delete_clip")
    def test_exclusion_rejects_a_revoked_records_officer(
        self,
        delete_clip,
        audit,
    ):
        ch = self._held_camera()
        officer = self._records_officer()
        apply_staff_roles(officer, set())
        audit.reset_mock()

        with self.assertRaisesMessage(
            PermissionDenied,
            "Current Records Officer authority is required to exclude a camera.",
        ):
            triage.exclude_camera(
                ch,
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=officer,
            )

        delete_clip.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.integrations.avigilon.client.delete_clip")
    def test_valid_exclusion_releases_clip_and_records_why(self, m_del):
        ch = self._held_camera()
        officer = self._records_officer()
        with self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(
                ch,
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=officer,
            )
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.RELEASED)
        self.assertIsNotNone(ch.excluded_at)
        self.assertEqual(ch.excluded_by, officer)
        self.assertIn("gym entrance", ch.exclusion_reason)
        m_del.assert_called_once_with("clip-x")
        self.assertEqual(ch.preservation.held_count, 2)   # two cameras remain held

    @mock.patch(
        "apps.integrations.avigilon.client.delete_clip",
        side_effect=alta.AltaError("Alta release unavailable"),
    )
    @mock.patch("apps.records.triage.audit")
    def test_release_failure_is_audited_without_undoing_exclusion(
        self,
        audit,
        delete_clip,
    ):
        ch = self._held_camera()
        officer = self._records_officer()
        audit.reset_mock()
        reason = (
            "Camera faces staff parking; request concerns the gym entrance only."
        )

        with self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(ch, reason, actor=officer)

        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.RELEASED)
        self.assertEqual(ch.excluded_by, officer)
        self.assertEqual(ch.exclusion_reason, reason)
        delete_clip.assert_called_once_with("clip-x")
        audit.assert_any_call(
            "footage.camera_release_failed",
            target=ch.preservation.request.number,
            actor=officer,
            camera=ch.camera_name or ch.camera_id,
            error="Alta release unavailable",
        )

    def test_no_automated_exclusion_path(self):
        """Guard rail: no background task may call exclude_camera."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(tasks))
        calls = [
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        ]
        self.assertNotIn("exclude_camera", calls)
