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
    RequestStatus,
)

CLIENT = "apps.integrations.avigilon.client"
# Older than every window these tests request, so the camera covers it.
LONG_AGO = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def camera(camera_id, name, site, *, pruned_to: dt.datetime | None = LONG_AGO,
           status="CONNECTED", sources=("0",)):
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


def preserve(prr) -> FootagePreservation:
    """triage_footage for a request the test knows is a footage request."""
    fp = triage.triage_footage(prr)
    assert fp is not None, "expected a footage request"
    return fp


def confirmation(fp) -> str:
    text = triage.preservation_confirmation_text(fp)
    assert text is not None, "expected a confirmation"
    return text


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
            first = preserve(prr)
            second = preserve(prr)

        self.assertEqual(first, second)
        self.assertEqual(FootagePreservation.objects.filter(request=prr).count(), 1)
        self.assertEqual(m.list.call_count, 1)
        self.assertEqual(m.hold.call_count, len(FAKE_CAMERAS))

    def test_no_site_named_holds_every_camera_district_wide(self):
        prr = make_request(f"camera footage of the gym entrance on {days_ago(3)}")
        with aware() as m:
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            preserve(prr)

        name = m.hold.call_args.kwargs["name"]
        self.assertLessEqual(len(name), triage.BOOKMARK_NAME_MAX_LENGTH)
        self.assertTrue(name.startswith(f"{prr.number} PRA hold — Gym"))
        self.assertTrue(name.endswith("…"))

    def test_no_parseable_date_holds_full_retention_window(self):
        prr = make_request("I want the surveillance video of the incident at the gym")
        with aware():
            fp = preserve(prr)
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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(m.hold.call_count, 2)
        self.assertEqual(review.call_args.kwargs["no_camera_sites"], ["HHE"])

    def test_window_older_than_flat_retention_still_checks_each_camera(self):
        # Some cameras keep far more than the setting; a flat floor is wrong.
        prr = make_request(f"video at Peninsula High School on {days_ago(14)}")
        with aware([camera("cam-1", "Commons", "PHS")]) as m:
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

        self.assertEqual(fp.camera_holds.get().state, CameraHold.State.GAP)

    def test_disconnected_camera_still_gets_a_hold_with_status_noted(self):
        cam = camera("cam-1", "Gym", "PHS", status="COMMUNICATION_ERROR")
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam]) as m:
            fp = preserve(prr)

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
            fp = preserve(prr)
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
            fp = preserve(prr)

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
        fp = preserve(prr)

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

        fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        notify_manual_hold.assert_called_once()

    @mock.patch(
        "apps.records.tasks.notifications.notify_footage_manual_hold",
        return_value=True,
    )
    def test_zero_cameras_is_never_reported_held(self, notify_manual_hold):
        prr = make_request(f"all camera footage at the school on {days_ago(2)}")

        with aware([]):
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)

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
            fp = preserve(prr)
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
        for forbidden in ("exclude_camera", "release_preservation", "delete_bookmark"):
            self.assertNotIn(forbidden, calls)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CAMERA_RETENTION_DAYS=10)
class ConfirmationTextTests(TestCase):
    """The sentence a Records Officer may copy into a letter. Computed from
    the hold rows, so it can only ever say what Aware confirmed."""

    WINDOW = "from 2026-09-22 00:00 to 2026-09-23 00:00 Pacific time"

    def _preserve(self, description, cameras=FAKE_CAMERAS, hold=full_hold):
        with aware(cameras, hold=hold):
            return preserve(make_request(description))

    def test_named_site_fully_held(self):
        fp = self._preserve("video at Peninsula High School on 2026-09-22")
        self.assertEqual(
            triage.preservation_confirmation_text(fp),
            f"2 cameras at Peninsula High School held {self.WINDOW} "
            "(Aware bookmarks, no expiry); 0 cameras have footage gaps; 0 failed.",
        )

    def test_single_camera_reads_singular(self):
        fp = self._preserve(
            "video at Peninsula High School on 2026-09-22",
            cameras=[camera("cam-1", "Gym Entrance", "PHS")],
        )
        self.assertTrue(
            confirmation(fp).startswith(
                "1 camera at Peninsula High School held"
            )
        )

    def test_district_wide_partial_counts_gaps_and_failures(self):
        def gap_and_failure(cam, start, end, *, name, description):
            if cam.id == "cam-2":
                raise alta.AltaError("Aware refused the bookmark")
            if cam.id == "cam-3":
                return alta.BookmarkHold("bm-cam-3", (alta.HeadCoverage("0", 0.5),))
            return full_hold(cam, start, end, name=name, description=description)

        fp = self._preserve("gym video on 2026-09-22", hold=gap_and_failure)
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(
            triage.preservation_confirmation_text(fp),
            f"2 cameras across the district held {self.WINDOW} "
            "(Aware bookmarks, no expiry); 1 camera has a footage gap; 1 failed.",
        )

    def test_not_retained_and_unheld_sites_are_named(self):
        cameras = [
            camera("cam-1", "Gym Entrance", "PHS"),
            camera(
                "cam-2", "Staff Parking", "PHS",
                pruned_to=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            ),
        ]
        fp = self._preserve(
            "video at Peninsula High School and Harbor Heights on 2026-09-22",
            cameras=cameras,
        )
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(
            triage.preservation_confirmation_text(fp),
            f"1 camera at Peninsula High School held {self.WINDOW} "
            "(Aware bookmarks, no expiry); 0 cameras have footage gaps; "
            "1 with no retained footage for the window; 0 failed; "
            "none held at Harbor Heights Elementary School.",
        )

    def test_excluded_cameras_are_not_counted_as_held(self):
        fp = self._preserve("video at Peninsula High School on 2026-09-22")
        officer = get_user_model().objects.create_user(
            "confirmation-officer", password="pw", is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        with (
            override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=()),
            mock.patch(f"{CLIENT}.delete_bookmark"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            triage.exclude_camera(
                fp.camera_holds.get(camera_id="cam-2"),
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=officer,
            )
        self.assertTrue(
            confirmation(fp).startswith(
                "1 camera at Peninsula High School held"
            )
        )

    def test_nothing_to_confirm_unless_held_or_partial(self):
        fp = self._preserve("video at Peninsula High School on 2026-09-22")
        for state in (
            FootagePreservation.State.PENDING,
            FootagePreservation.State.MANUAL,
            FootagePreservation.State.NO_CAMERAS,
            FootagePreservation.State.EXPIRED,
            FootagePreservation.State.RELEASED,
        ):
            fp.state = state
            with self.subTest(state=state):
                self.assertIsNone(triage.preservation_confirmation_text(fp))


RELEASE_REASON = (
    "Request fulfilled; installment produced and the requester confirmed receipt."
)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CAMERA_RETENTION_DAYS=10,
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
)
class ReleaseGateTests(TestCase):
    """Release ends a hold for good: Aware deletes the bookmarks, and the
    footage ages out. Only a current Records Officer, after the request
    closes, with a reason and a litigation-hold attestation."""

    def _records_officer(self):
        officer = get_user_model().objects.create_user(
            f"release-officer-{get_user_model().objects.count()}",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        return officer

    def _held_preservation(self, *, status=RequestStatus.FULFILLED, hold=full_hold):
        with aware(hold=hold):
            prr = make_request(f"gym entrance footage {days_ago(1)}")
            fp = preserve(prr)
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(status=status)
        fp.refresh_from_db()
        return fp

    def _release(self, fp, **overrides):
        kwargs = {
            "actor": overrides.pop("actor", None) or self._records_officer(),
            "reason": RELEASE_REASON,
            "attest_no_litigation_hold": True,
        }
        kwargs.update(overrides)
        return triage.release_preservation(fp, **kwargs)

    def assertNothingReleased(self, fp, delete_bookmark):
        delete_bookmark.assert_not_called()
        fp.refresh_from_db()
        self.assertIn(
            fp.state,
            (FootagePreservation.State.HELD, FootagePreservation.State.PARTIAL),
        )
        self.assertIsNone(fp.released_at)
        self.assertIsNone(fp.released_by_id)
        self.assertEqual(fp.release_reason, "")
        self.assertFalse(
            fp.camera_holds.filter(state=CameraHold.State.RELEASED).exists()
        )
        self.assertFalse(
            AuditLog.objects.filter(action="footage.preservation_released").exists()
        )

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_requires_a_current_records_officer(self, delete_bookmark):
        fp = self._held_preservation()
        administrator = get_user_model().objects.create_user(
            "release-application-admin", password="pw", is_staff=True,
        )
        apply_staff_roles(administrator, {StaffRole.ADMIN})
        revoked = self._records_officer()
        apply_staff_roles(revoked, set())

        for actor in (None, administrator, revoked):
            with (
                self.subTest(actor=getattr(actor, "username", None)),
                self.assertRaisesMessage(
                    PermissionDenied,
                    "Current Records Officer authority is required to release "
                    "a preservation hold.",
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                triage.release_preservation(
                    fp,
                    actor=actor,
                    reason=RELEASE_REASON,
                    attest_no_litigation_hold=True,
                )
        self.assertNothingReleased(fp, delete_bookmark)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_requires_a_substantive_reason(self, delete_bookmark):
        fp = self._held_preservation()
        for bad in ("", "   ", "n/a", None, "x" * 501):
            with (
                self.subTest(reason=bad if bad is None else bad[:12]),
                self.assertRaises(ValueError),
                self.captureOnCommitCallbacks(execute=True),
            ):
                self._release(fp, reason=bad)
        self.assertNothingReleased(fp, delete_bookmark)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_requires_the_litigation_hold_attestation(self, delete_bookmark):
        fp = self._held_preservation()
        for attestation in (False, None, "true", 1):
            with (
                self.subTest(attestation=attestation),
                self.assertRaisesMessage(ValueError, "litigation hold"),
                self.captureOnCommitCallbacks(execute=True),
            ):
                self._release(fp, attest_no_litigation_hold=attestation)
        self.assertNothingReleased(fp, delete_bookmark)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_refuses_a_request_that_is_still_open(self, delete_bookmark):
        for status in (RequestStatus.RECEIVED, RequestStatus.ACKNOWLEDGED):
            fp = self._held_preservation(status=status)
            with (
                self.subTest(status=status),
                self.assertRaisesMessage(ValueError, "still open"),
                self.captureOnCommitCallbacks(execute=True),
            ):
                self._release(fp)
            self.assertNothingReleased(fp, delete_bookmark)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_refuses_a_duplicate_whose_case_of_record_is_open(
        self, delete_bookmark,
    ):
        # A merge leaves the preservation on the duplicate, so its bookmarks
        # still hold the footage the case of record is answering for.
        fp = self._held_preservation(status=RequestStatus.CLOSED_DUPLICATE)
        target = make_request("the same footage request, filed again")
        PublicRecordsRequest.objects.filter(pk=fp.request_id).update(
            merged_into=target,
        )
        with (
            self.assertRaisesMessage(ValueError, target.number),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._release(fp)
        self.assertNothingReleased(fp, delete_bookmark)

        PublicRecordsRequest.objects.filter(pk=target.pk).update(
            status=RequestStatus.FULFILLED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            released = self._release(fp)
        self.assertEqual(released.state, FootagePreservation.State.RELEASED)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_refuses_a_preservation_that_holds_nothing(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        for state in (
            FootagePreservation.State.PENDING,
            FootagePreservation.State.MANUAL,
            FootagePreservation.State.NO_CAMERAS,
            FootagePreservation.State.EXPIRED,
            FootagePreservation.State.RELEASED,
        ):
            FootagePreservation.objects.filter(pk=fp.pk).update(state=state)
            with (
                self.subTest(state=state),
                self.assertRaisesMessage(ValueError, "Only a held or partly held"),
                self.captureOnCommitCallbacks(execute=True),
            ):
                self._release(fp)
        delete_bookmark.assert_not_called()

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_valid_release_deletes_every_bookmark_and_records_why(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        officer = self._records_officer()
        with self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(
                fp.camera_holds.get(camera_id="cam-2"),
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=officer,
            )
        delete_bookmark.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            released = triage.release_preservation(
                fp,
                actor=officer,
                reason=f"  {RELEASE_REASON}  ",
                attest_no_litigation_hold=True,
            )

        fp.refresh_from_db()
        self.assertEqual(released.pk, fp.pk)
        self.assertEqual(fp.state, FootagePreservation.State.RELEASED)
        self.assertIsNotNone(fp.released_at)
        self.assertEqual(fp.released_by, officer)
        self.assertEqual(fp.release_reason, RELEASE_REASON)
        self.assertEqual(fp.held_count, 0)
        # The excluded camera's bookmark went with its exclusion; only the
        # cameras still held are deleted now.
        self.assertEqual(
            sorted(c.args[0] for c in delete_bookmark.call_args_list),
            ["bm-cam-1", "bm-cam-3"],
        )
        self.assertEqual(
            set(fp.camera_holds.values_list("state", flat=True)),
            {CameraHold.State.RELEASED},
        )
        excluded = fp.camera_holds.get(camera_id="cam-2")
        self.assertIsNotNone(excluded.excluded_at)

        log = AuditLog.objects.get(action="footage.preservation_released")
        self.assertEqual(log.actor, officer)
        self.assertEqual(log.target, fp.request.number)
        self.assertEqual(log.detail["reason"], RELEASE_REASON)
        self.assertIs(log.detail["attest_no_litigation_hold"], True)
        self.assertEqual(log.detail["bookmarks"], 2)

        event = fp.request.events.get(note__startswith="Footage preservation released")
        self.assertEqual(event.actor, officer)
        self.assertIn(RELEASE_REASON, event.note)
        self.assertIn("litigation hold", event.note)
        self.assertFalse(event.visible_to_requester)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_partial_preservation_releases_gap_bookmarks_too(self, delete_bookmark):
        def gap_on_cam_3(cam, start, end, *, name, description):
            if cam.id == "cam-3":
                return alta.BookmarkHold(
                    id="bm-cam-3", coverage=(alta.HeadCoverage("0", 0.4),),
                )
            if cam.id == "cam-2":
                raise alta.AltaError("Aware refused the bookmark")
            return full_hold(cam, start, end, name=name, description=description)

        fp = self._held_preservation(hold=gap_on_cam_3)
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)

        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        self.assertEqual(
            sorted(c.args[0] for c in delete_bookmark.call_args_list),
            ["bm-cam-1", "bm-cam-3"],
        )
        # A failed camera never had a bookmark; its row keeps saying so.
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-2").state, CameraHold.State.FAILED,
        )
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-3").state, CameraHold.State.RELEASED,
        )

    @mock.patch("apps.records.triage.audit")
    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_does_not_delete_bookmarks_when_audit_fails(
        self, delete_bookmark, audit,
    ):
        fp = self._held_preservation()
        officer = self._records_officer()
        audit.reset_mock()
        audit.side_effect = RuntimeError("audit persistence failed")

        with (
            self.assertRaisesMessage(RuntimeError, "audit persistence failed"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._release(fp, actor=officer)

        audit.assert_called_once()
        self.assertNothingReleased(fp, delete_bookmark)

    @mock.patch(
        f"{CLIENT}.delete_bookmark",
        side_effect=alta.AltaError("Aware delete unavailable"),
    )
    def test_bookmark_delete_failure_is_audited_without_undoing_release(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        officer = self._records_officer()

        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp, actor=officer)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.RELEASED)
        self.assertEqual(delete_bookmark.call_count, 3)
        failures = AuditLog.objects.filter(action="footage.bookmark_release_failed")
        self.assertEqual(failures.count(), 3)
        failure = failures.get(detail__bookmark="bm-cam-1")
        self.assertEqual(failure.actor, officer)
        self.assertEqual(failure.detail["camera"], "Gym Entrance")
        self.assertEqual(failure.detail["error"], "Aware delete unavailable")
