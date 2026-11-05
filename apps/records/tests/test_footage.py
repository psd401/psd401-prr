"""Footage triage tests — auto-hold always, auto-deny never."""
import contextlib
import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.integrations.avigilon import client as alta
from apps.records import tasks, triage
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
)

CLIENT = "apps.integrations.avigilon.client"
# Older than every window these tests request, so the camera covers it.
LONG_AGO = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def camera(camera_id, name, site, *, pruned_to=LONG_AGO, status="CONNECTED",
           sources=("0",)):
    return alta.Camera(
        id=camera_id, name=name, site=site, device_group_id=f"group-{site}",
        server_group_id="server-1", status=status, pruned_to=pruned_to,
        sources=sources,
    )


FAKE_CAMERAS = [
    camera("cam-1", "Gym Entrance", "PHS"),
    camera("cam-2", "Staff Parking", "PHS"),
    camera("cam-3", "Main Office", "KMS"),
]
FAKE_GROUPS = [
    alta.DeviceGroup(id=f"group-{code}", name=code)
    for code in ("PHS", "KMS", "HHE")
]


def full_hold(cam, start, end, *, name, description):
    return alta.BookmarkHold(
        id=f"bm-{cam.id}",
        coverage=tuple(alta.HeadCoverage(source, 1.0) for source in cam.sources),
    )


@contextlib.contextmanager
def aware(cameras=FAKE_CAMERAS, *, groups=FAKE_GROUPS, hold=full_hold):
    with mock.patch(f"{CLIENT}.list_device_cameras", return_value=list(cameras)) as m_list, \
         mock.patch(f"{CLIENT}.list_device_groups", return_value=list(groups)) as m_groups, \
         mock.patch(f"{CLIENT}.create_bookmark_hold", side_effect=hold) as m_hold:
        yield SimpleNamespace(list=m_list, groups=m_groups, hold=m_hold)


def days_ago(days: int) -> str:
    return (timezone.localdate() - dt.timedelta(days=days)).isoformat()


def make_request(description: str):
    r = Requester.objects.create(email="jane@example.org")
    return PublicRecordsRequest.objects.create(requester=r, description=description)


class PreservationModelTests(TestCase):
    def test_states_name_what_actually_happened(self):
        State = FootagePreservation.State
        self.assertEqual(State.NO_CAMERAS.label, "No cameras in Aware at the requested site")
        self.assertEqual(State.MANUAL.label, "Manual hold required")
        self.assertIn("gap", CameraHold.State.GAP.label.lower())
        self.assertEqual(CameraHold.State.NOT_RETAINED, "not_retained")
        self.assertEqual(
            CameraHold._meta.get_field("clip_id").verbose_name, "Aware bookmark id"
        )

    def test_held_count_counts_every_bookmarked_camera_not_excluded(self):
        prr = make_request("camera footage")
        now = timezone.now()
        fp = FootagePreservation.objects.create(
            request=prr, window_start=now - dt.timedelta(days=1), window_end=now,
            site_codes=["PHS"], site_resolution="Named: Peninsula High School",
        )
        for camera_id, state in (
            ("a", CameraHold.State.HELD),
            ("b", CameraHold.State.GAP),
            ("c", CameraHold.State.NOT_RETAINED),
            ("d", CameraHold.State.FAILED),
        ):
            CameraHold.objects.create(preservation=fp, camera_id=camera_id, state=state)
        self.assertEqual(fp.held_count, 2)
        fp.refresh_from_db()
        self.assertEqual(fp.site_codes, ["PHS"])
        self.assertIsNone(fp.released_at)


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


class CalendarDayWindowTests(SimpleTestCase):
    """A date D holds [D 00:00, D+1 00:00) Pacific, whatever its length."""

    def assertWindow(self, dates, start_utc, end_utc, hours):
        start, end = triage.calendar_day_window(dates)
        self.assertEqual(start.astimezone(dt.UTC), start_utc)
        self.assertEqual(end.astimezone(dt.UTC), end_utc)
        self.assertEqual(
            end.astimezone(dt.UTC) - start.astimezone(dt.UTC),
            dt.timedelta(hours=hours),
        )

    def test_ordinary_pdt_day_is_24_hours(self):
        self.assertWindow(
            [dt.date(2026, 9, 22)],
            dt.datetime(2026, 9, 22, 7, tzinfo=dt.UTC),
            dt.datetime(2026, 9, 23, 7, tzinfo=dt.UTC),
            24,
        )

    def test_spring_forward_day_is_23_hours(self):
        self.assertWindow(
            [dt.date(2026, 3, 8)],
            dt.datetime(2026, 3, 8, 8, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 9, 7, tzinfo=dt.UTC),
            23,
        )

    def test_fall_back_day_is_25_hours(self):
        self.assertWindow(
            [dt.date(2026, 11, 1)],
            dt.datetime(2026, 11, 1, 7, tzinfo=dt.UTC),
            dt.datetime(2026, 11, 2, 8, tzinfo=dt.UTC),
            25,
        )

    def test_several_dates_hold_first_midnight_to_midnight_after_last(self):
        self.assertWindow(
            [dt.date(2026, 9, 24), dt.date(2026, 9, 22)],
            dt.datetime(2026, 9, 22, 7, tzinfo=dt.UTC),
            dt.datetime(2026, 9, 25, 7, tzinfo=dt.UTC),
            72,
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CAMERA_RETENTION_DAYS=10)
class HoldPathTests(TestCase):
    def test_hold_triage_retry_reuses_durable_preservation(self):
        prr = make_request(f"camera footage near the office on {days_ago(3)}")
        with aware() as m:
            first = triage.triage_footage(prr)
            second = triage.triage_footage(prr)

        self.assertEqual(first, second)
        self.assertEqual(FootagePreservation.objects.filter(request=prr).count(), 1)
        self.assertEqual(m.list.call_count, 1)
        self.assertEqual(m.hold.call_count, len(FAKE_CAMERAS))

    def test_no_site_named_holds_every_camera_district_wide(self):
        prr = make_request(f"camera footage of the gym entrance on {days_ago(3)}")
        with aware() as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.site_codes, [])
        self.assertIn("district-wide", fp.site_resolution)
        self.assertEqual(fp.camera_holds.count(), 3)          # broad by default
        self.assertEqual(m.hold.call_count, 3)
        self.assertEqual(fp.held_count, 3)
        m.groups.assert_not_called()
        # Officer sees a review prompt on the timeline
        self.assertTrue(prr.events.filter(note__icontains="exclude irrelevant").exists())

    def test_named_site_holds_only_that_sites_cameras(self):
        prr = make_request(
            f"all camera footage at Peninsula High School on {days_ago(3)}"
        )
        with aware() as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.site_codes, ["PHS"])
        self.assertIn("Peninsula High School", fp.site_resolution)
        self.assertEqual(
            sorted(fp.camera_holds.values_list("camera_id", flat=True)),
            ["cam-1", "cam-2"],
        )
        self.assertEqual(
            [c.args[0].id for c in m.hold.call_args_list], ["cam-1", "cam-2"]
        )
        hold = fp.camera_holds.get(camera_id="cam-1")
        self.assertEqual(hold.clip_id, "bm-cam-1")
        self.assertEqual(hold.coverage_fraction, 1.0)
        self.assertEqual(hold.device_group_id, "group-PHS")

    def test_bookmark_holds_the_requested_window_with_pra_name(self):
        prr = make_request("video at Peninsula High School on 2026-09-22")
        with aware([camera("cam-1", "Gym Entrance", "PHS")]) as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.window_start, dt.datetime(2026, 9, 22, 7, tzinfo=dt.UTC))
        self.assertEqual(fp.window_end, dt.datetime(2026, 9, 23, 7, tzinfo=dt.UTC))
        args, kwargs = m.hold.call_args
        self.assertEqual(args[1], fp.window_start)
        self.assertEqual(args[2], fp.window_end)
        self.assertEqual(kwargs["name"], f"{prr.number} PRA hold — Gym Entrance")
        self.assertEqual(
            kwargs["description"],
            "Public Records Act preservation hold. Do not delete. "
            "Managed by the PRR system.",
        )

    def test_long_camera_name_is_truncated_in_bookmark_name(self):
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([camera("cam-1", "Gym " * 60, "PHS")]) as m:
            triage.triage_footage(prr)

        name = m.hold.call_args.kwargs["name"]
        self.assertLessEqual(len(name), triage.BOOKMARK_NAME_MAX_LENGTH)
        self.assertTrue(name.startswith(f"{prr.number} PRA hold — Gym"))
        self.assertTrue(name.endswith("…"))

    def test_no_parseable_date_holds_full_retention_window(self):
        prr = make_request("I want the surveillance video of the incident at the gym")
        with aware():
            fp = triage.triage_footage(prr)
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        window_days = (fp.window_end - fp.window_start).days
        self.assertGreaterEqual(window_days, 9)               # ~full retention held
        self.assertIn("No dates parseable", fp.detected_dates_note)

    def test_harbor_heights_with_no_cameras_is_never_held(self):
        # Harbor Heights, 2026-09-23: the HHE group exists with no devices.
        prr = make_request(
            f"All video from Harbor Heights Elementary on {days_ago(1)}; "
            "please preserve it and confirm the hold in writing."
        )
        with aware() as m, \
             mock.patch("apps.records.tasks.notifications.notify_adequate_search_review") as review, \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold") as manual, \
             mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete") as complete:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.NO_CAMERAS)
        self.assertEqual(fp.site_codes, ["HHE"])
        self.assertEqual(fp.held_count, 0)
        m.hold.assert_not_called()
        self.assertFalse(fp.camera_holds.exists())
        review.assert_called_once()
        self.assertEqual(review.call_args.kwargs["no_camera_sites"], ["HHE"])
        manual.assert_not_called()
        complete.assert_not_called()
        notes = " ".join(prr.events.values_list("note", flat=True)).lower()
        self.assertNotIn("held", notes)
        for phrase in ("human review required", "doorbell", "sro"):
            self.assertIn(phrase, notes)
        prr.refresh_from_db()
        self.assertEqual(prr.status, "received")
        self.assertFalse(prr.correspondence.exists())

    def test_named_site_missing_from_aware_is_urgent_manual(self):
        prr = make_request(f"video at Goodman Middle School on {days_ago(1)}")
        with aware() as m, \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        m.hold.assert_not_called()
        manual.assert_called_once()
        self.assertIn("GMS", manual.call_args.kwargs["error"])
        self.assertEqual(manual.call_args.kwargs["site_codes"], ["GMS"])

    def test_one_named_site_with_no_cameras_keeps_preservation_partial(self):
        prr = make_request(
            f"video at Peninsula High School and Harbor Heights on {days_ago(1)}"
        )
        with aware() as m, \
             mock.patch("apps.records.tasks.notifications.notify_adequate_search_review") as review:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(m.hold.call_count, 2)
        self.assertEqual(review.call_args.kwargs["no_camera_sites"], ["HHE"])

    def test_window_older_than_flat_retention_still_checks_each_camera(self):
        # Some cameras keep far more than the setting; a flat floor is wrong.
        prr = make_request(f"video at Peninsula High School on {days_ago(14)}")
        with aware([camera("cam-1", "Commons", "PHS")]) as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(m.hold.call_count, 1)
        expected_start, _ = triage.calendar_day_window(
            [timezone.localdate() - dt.timedelta(days=14)]
        )
        self.assertEqual(fp.window_start, expected_start)     # never clamped

    def test_footage_pruned_after_window_start_is_bookmarked_from_oldest_footage(self):
        start, end = triage.calendar_day_window(
            [timezone.localdate() - dt.timedelta(days=2)]
        )
        pruned_to = start + (end - start) / 2
        cam = camera("cam-1", "Gym Entrance", "PHS", pruned_to=pruned_to)
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam]) as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(m.hold.call_args.args[1], pruned_to)
        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.GAP)
        self.assertAlmostEqual(hold.coverage_fraction, 0.5, places=3)
        self.assertIn("oldest retained footage", hold.coverage_note.lower())
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)

    def test_footage_pruned_after_window_end_is_not_retained_and_not_bookmarked(self):
        _, end = triage.calendar_day_window(
            [timezone.localdate() - dt.timedelta(days=2)]
        )
        cameras = [
            camera("cam-1", "Gym Entrance", "PHS"),
            camera("cam-2", "Staff Parking", "PHS", pruned_to=end),
        ]
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware(cameras) as m:
            fp = triage.triage_footage(prr)

        self.assertEqual([c.args[0].id for c in m.hold.call_args_list], ["cam-1"])
        pruned = fp.camera_holds.get(camera_id="cam-2")
        self.assertEqual(pruned.state, CameraHold.State.NOT_RETAINED)
        self.assertEqual(pruned.clip_id, "")
        self.assertIn("after the window end", pruned.coverage_note)
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)

    def test_every_camera_not_retained_is_expired_with_one_checklist(self):
        _, end = triage.calendar_day_window(
            [timezone.localdate() - dt.timedelta(days=2)]
        )
        cameras = [camera("cam-1", "Gym", "PHS", pruned_to=end + dt.timedelta(hours=1))]
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware(cameras) as m, \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold") as manual, \
             mock.patch("apps.records.triage.notifications.notify_adequate_search_review") as review:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.EXPIRED)
        m.hold.assert_not_called()
        manual.assert_not_called()
        review.assert_called_once()
        checklist = prr.events.get()
        for phrase in ("HUMAN REVIEW REQUIRED", "HUMAN MUST COMPLETE",
                       "incident-hold", "email & Drive"):
            self.assertIn(phrase, checklist.note)
        self.assertTrue(
            AuditLog.objects.filter(action="footage.expired_window",
                                    target=prr.number).exists()
        )

    def test_unknown_oldest_footage_holds_from_window_start_and_says_so(self):
        cam = camera("cam-1", "Gym", "PHS", pruned_to=None)
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam]) as m:
            fp = triage.triage_footage(prr)

        self.assertEqual(m.hold.call_args.args[1], fp.window_start)
        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.HELD)
        self.assertIn("no oldest-footage time", hold.coverage_note)

    def test_low_coverage_on_one_head_is_a_gap_with_heads_recorded(self):
        cam = camera("cam-1", "Multisensor", "PHS", sources=("0", "1"))

        def gappy(cam, start, end, *, name, description):
            return alta.BookmarkHold(
                id="bm-1",
                coverage=(alta.HeadCoverage("0", 1.0),
                          alta.HeadCoverage("1", 0.6, "recording gap")),
            )

        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam], hold=gappy):
            fp = triage.triage_footage(prr)

        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.GAP)
        self.assertAlmostEqual(hold.coverage_fraction, 0.6, places=3)
        self.assertIn("head 1: 60%", hold.coverage_note)
        self.assertIn("recording gap", hold.coverage_note)
        self.assertEqual(hold.clip_id, "bm-1")

    def test_empty_coverage_report_is_never_a_clean_hold(self):
        def silent(cam, start, end, *, name, description):
            return alta.BookmarkHold(id="bm-1", coverage=())

        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([camera("cam-1", "Gym", "PHS")], hold=silent):
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.camera_holds.get().state, CameraHold.State.GAP)

    def test_disconnected_camera_still_gets_a_hold_with_status_noted(self):
        cam = camera("cam-1", "Gym", "PHS", status="COMMUNICATION_ERROR")
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam]) as m:
            fp = triage.triage_footage(prr)

        m.hold.assert_called_once()
        self.assertIn("COMMUNICATION_ERROR", fp.camera_holds.get().coverage_note)

    def test_rerun_skips_bookmarked_cameras_and_retries_failures(self):
        attempts = iter([None, RuntimeError("bookmark rejected"), None])

        def flaky(cam, start, end, *, name, description):
            error = next(attempts, None)
            if error:
                raise error
            return full_hold(cam, start, end, name=name, description=description)

        prr = make_request(f"camera footage of the gym on {days_ago(2)}")
        with aware(hold=flaky) as m:
            fp = triage.triage_footage(prr)
            self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
            tasks.execute_footage_hold(fp.id)

        self.assertEqual(
            [c.args[0].id for c in m.hold.call_args_list],
            ["cam-1", "cam-2", "cam-3", "cam-2"],
        )
        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.camera_holds.count(), 3)

    def test_released_preservation_is_never_re_held(self):
        prr = make_request("camera footage")
        now = timezone.now()
        fp = FootagePreservation.objects.create(
            request=prr, window_start=now - dt.timedelta(days=1), window_end=now,
            state=FootagePreservation.State.RELEASED,
        )
        with aware() as m:
            tasks.execute_footage_hold(fp.id)
        m.list.assert_not_called()
        m.hold.assert_not_called()

    def test_expired_window_queues_review_never_denies(self):
        prr = make_request(f"cctv footage of the parking lot on {days_ago(30)}")
        with mock.patch(f"{CLIENT}.list_device_cameras",
                        side_effect=alta.AltaError("Aware unreachable")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold") as manual:
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.EXPIRED)
        self.assertEqual(fp.camera_holds.count(), 0)          # no pointless holds
        manual.assert_not_called()
        # Request is NOT denied — status untouched, checklist queued for a human
        prr.refresh_from_db()
        self.assertEqual(prr.status, "received")
        checklist = prr.events.get()
        for phrase in ("HUMAN MUST COMPLETE", "incident-hold", "email & Drive"):
            self.assertIn(phrase, checklist.note)
        self.assertIn("HUMAN REVIEW REQUIRED", checklist.note)
        self.assertIn("per-camera retention could not be checked", checklist.note)
        self.assertNotIn("response queued", checklist.note)

    @mock.patch(f"{CLIENT}.list_device_cameras",
                side_effect=alta.AltaNotConfigured("no token"))
    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_api_unconfigured_degrades_to_urgent_manual_hold(
        self,
        notify_manual_hold,
        m_list,
    ):
        prr = make_request(f"video from the main office camera on {days_ago(2)}")
        fp = triage.triage_footage(prr)

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
        prr = make_request(f"video from the main office camera on {days_ago(2)}")

        fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()
        self.assertTrue(
            prr.events.filter(
                note__icontains="URGENT manual hold notification",
            ).exists()
        )

    def test_completed_hold_records_durable_timeline_evidence(self):
        prr = make_request(f"PRIVATE gym camera narrative on {days_ago(3)}")

        with aware():
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.held_count, 3)
        self.assertTrue(
            prr.events.filter(
                note__icontains="3 camera(s) held, 0 with footage gaps, 0 failed",
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

        prr = make_request(f"video from the main office camera on {days_ago(2)}")
        with mock.patch(
            f"{CLIENT}.list_device_cameras",
            side_effect=requests.ConnectionError("connector unreachable"),
        ):
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_zero_cameras_is_never_reported_held(self, notify_manual_hold):
        prr = make_request(f"all camera footage at the school on {days_ago(2)}")

        with aware([]):
            fp = triage.triage_footage(prr)

        self.assertNotEqual(fp.state, FootagePreservation.State.HELD)
        self.assertEqual(fp.held_count, 0)
        notify_manual_hold.assert_called_once()

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_every_camera_failing_degrades_to_urgent_manual_hold(
        self, notify_manual_hold,
    ):
        prr = make_request(f"camera footage of the gym on {days_ago(2)}")

        with aware(hold=RuntimeError("bookmark rejected")):
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        self.assertEqual(fp.held_count, 0)
        notify_manual_hold.assert_called_once()

    @mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete")
    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_zero_cameras_records_reason_and_sends_no_completion(
        self, notify_manual_hold, notify_complete,
    ):
        prr = make_request(f"all camera footage at the school on {days_ago(2)}")

        with aware([]):
            fp = triage.triage_footage(prr)

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
    def test_some_cameras_failing_stays_partial(
        self, notify_manual_hold, notify_complete,
    ):
        outcomes = iter([None, RuntimeError("bookmark rejected"), None])

        def flaky(cam, start, end, *, name, description):
            error = next(outcomes)
            if error:
                raise error
            return full_hold(cam, start, end, name=name, description=description)

        prr = make_request(f"camera footage of the gym on {days_ago(2)}")

        with aware(hold=flaky):
            fp = triage.triage_footage(prr)

        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(fp.held_count, 2)
        self.assertEqual(fp.camera_holds.get(camera_id="cam-2").error, "bookmark rejected")
        notify_manual_hold.assert_not_called()
        notify_complete.assert_called_once_with(
            fp, held=2, gap=0, failed=1, not_retained=0,
        )


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
        with aware():
            prr = make_request(f"gym entrance footage {days_ago(1)}")
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
    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_exclusion_rejects_oversized_reason_before_clip_delete(
        self,
        delete_bookmark,
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

        delete_bookmark.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_exclusion_does_not_delete_bookmark_when_audit_fails(
        self,
        delete_bookmark,
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

        delete_bookmark.assert_not_called()
        audit.assert_called_once()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_exclusion_requires_a_current_records_officer(
        self,
        delete_bookmark,
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

        delete_bookmark.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch("apps.records.triage.audit")
    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_exclusion_rejects_a_revoked_records_officer(
        self,
        delete_bookmark,
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

        delete_bookmark.assert_not_called()
        audit.assert_not_called()
        ch.refresh_from_db()
        self.assertEqual(ch.state, CameraHold.State.HELD)
        self.assertIsNone(ch.excluded_at)
        self.assertIsNone(ch.excluded_by_id)
        self.assertEqual(ch.exclusion_reason, "")

    @mock.patch(f"{CLIENT}.delete_bookmark")
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
        m_del.assert_called_once_with("bm-cam-2")
        self.assertEqual(ch.preservation.held_count, 2)   # two cameras remain held

    @mock.patch(
        f"{CLIENT}.delete_bookmark",
        side_effect=alta.AltaError("Alta release unavailable"),
    )
    @mock.patch("apps.records.triage.audit")
    def test_release_failure_is_audited_without_undoing_exclusion(
        self,
        audit,
        delete_bookmark,
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
        delete_bookmark.assert_called_once_with("bm-cam-2")
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
