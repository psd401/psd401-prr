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
from apps.records import footage_sites, tasks, triage
from apps.records.models import (
    CameraHold,
    FootagePreservation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)

CLIENT = "apps.integrations.avigilon.client"
FAKE_BASE = "https://example.us4.aware.avasecurity.com"
DEVICE_GUID = "00000000-0000-0000-0000-000000000001"
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
# An invented school: its Aware group exists in FAKE_GROUPS with no cameras.
EMPTY_SITE = footage_sites.Site(
    "ZZE", "Zephyr Hollow Elementary School", ("Zephyr Hollow",),
)
FAKE_GROUPS = [
    alta.DeviceGroup(id=f"group-{code}", name=code)
    for code in ("PHS", "KMS", EMPTY_SITE.code)
]


@contextlib.contextmanager
def with_empty_site():
    """Make EMPTY_SITE resolvable from request text for the duration."""
    with mock.patch.object(footage_sites, "SITES", (*footage_sites.SITES, EMPTY_SITE)), \
         mock.patch.dict(footage_sites._BY_CODE, {EMPTY_SITE.code: EMPTY_SITE}):
        yield


def head(source, fraction, start, end, error=""):
    """A head whose stored footage fills ``fraction`` of [start, end)."""
    span = (end - start).total_seconds()
    return alta.HeadCoverage(source, fraction * span, span, error)


def full_hold(cam, start, end, *, name, description):
    return alta.BookmarkHold(
        id=f"bm-{cam.id}",
        coverage=tuple(head(source, 1.0, start, end) for source in cam.sources),
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


def preserve_uncommitted(prr) -> FootagePreservation:
    """triage_footage for a request the test knows is a footage request."""
    fp = triage.triage_footage(prr)
    assert fp is not None, "expected a footage request"
    return fp


def preserve(prr) -> FootagePreservation:
    """Triage as intake does, then commit, so the hold task runs."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        fp = preserve_uncommitted(prr)
    fp.refresh_from_db()
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
class CoverageScoringTests(SimpleTestCase):
    NOW = dt.datetime(2026, 11, 5, 18, 0, tzinfo=dt.UTC)

    def test_a_window_still_open_is_scored_on_what_has_happened(self):
        """Live (2026-11-05): a bookmark from now-10min to now+10min read 50%
        of its whole span; every second that existed was stored."""
        start = self.NOW - dt.timedelta(minutes=10)
        end = self.NOW + dt.timedelta(minutes=10)
        bookmark = alta.BookmarkHold("bm-1", (alta.HeadCoverage("0", 600.0, 1200.0),))

        fraction, notes, verified = tasks._bookmark_coverage(
            bookmark, sources=("0",), start=start, end=end, now=self.NOW,
        )

        self.assertEqual((fraction, notes, verified), (1.0, [], True))

    def test_footage_missing_before_the_oldest_retained_counts_against_the_window(self):
        start = self.NOW - dt.timedelta(hours=4)
        end = self.NOW - dt.timedelta(hours=2)
        bookmark = alta.BookmarkHold("bm-1", (alta.HeadCoverage("0", 3600.0, 3600.0),))

        fraction, _, verified = tasks._bookmark_coverage(
            bookmark, sources=("0",), start=start, end=end, now=self.NOW,
        )

        self.assertAlmostEqual(fraction, 0.5)
        self.assertTrue(verified)

    def test_a_bookmarked_head_with_no_coverage_entry_is_unverified(self):
        start = self.NOW - dt.timedelta(hours=2)
        end = self.NOW - dt.timedelta(hours=1)
        bookmark = alta.BookmarkHold("bm-1", (alta.HeadCoverage("0", 3600.0, 3600.0),))

        fraction, notes, verified = tasks._bookmark_coverage(
            bookmark, sources=("0", "1"), start=start, end=end, now=self.NOW,
        )

        self.assertFalse(verified)
        self.assertEqual(fraction, 0.0)
        self.assertIn("head 1", " ".join(notes))


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

    def test_a_named_site_with_no_cameras_is_never_held(self):
        prr = make_request(
            f"All video from Zephyr Hollow Elementary on {days_ago(1)}; "
            "please preserve it and confirm the hold in writing."
        )
        with with_empty_site(), aware() as m, \
             mock.patch("apps.records.tasks.notifications.notify_adequate_search_review") as review, \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold") as manual, \
             mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete") as complete:
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.NO_CAMERAS)
        self.assertEqual(fp.site_codes, ["ZZE"])
        self.assertEqual(fp.held_count, 0)
        m.hold.assert_not_called()
        self.assertFalse(fp.camera_holds.exists())
        review.assert_called_once()
        self.assertEqual(review.call_args.kwargs["no_camera_sites"], ["ZZE"])
        manual.assert_called_once()
        self.assertEqual(manual.call_args.kwargs["site_codes"], ["ZZE"])
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
            f"video at Peninsula High School and Zephyr Hollow on {days_ago(1)}"
        )
        with with_empty_site(), aware() as m, \
             mock.patch("apps.records.tasks.notifications.notify_adequate_search_review") as review:
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(m.hold.call_count, 2)
        self.assertEqual(review.call_args.kwargs["no_camera_sites"], ["ZZE"])

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
        with aware(cameras) as m, \
             mock.patch("apps.records.triage.notifications.notify_adequate_search_review") as review:
            fp = preserve(prr)

        self.assertEqual([c.args[0].id for c in m.hold.call_args_list], ["cam-1"])
        pruned = fp.camera_holds.get(camera_id="cam-2")
        self.assertEqual(pruned.state, CameraHold.State.NOT_RETAINED)
        self.assertEqual(pruned.clip_id, "")
        self.assertIn("after the window end", pruned.coverage_note)
        self.assertIn("other users' bookmarks", pruned.coverage_note)
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        review.assert_called_once()
        self.assertIn("Staff Parking (PHS)", review.call_args.kwargs["not_retained_cameras"][0])
        self.assertIn("other users' bookmarks", review.call_args.kwargs["note"])
        checklist = prr.events.get(note__contains="HUMAN MUST COMPLETE")
        self.assertIn("other users' bookmarks", checklist.note)

    def test_a_naive_oldest_footage_time_is_read_as_utc(self):
        _, end = triage.calendar_day_window(
            [timezone.localdate() - dt.timedelta(days=2)]
        )
        pruned_utc = end.astimezone(dt.UTC) - dt.timedelta(hours=1)
        cam = camera("cam-1", "Gym", "PHS", pruned_to=pruned_utc.replace(tzinfo=None))
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam]) as m:
            preserve(prr)

        m.hold.assert_called_once()
        self.assertEqual(m.hold.call_args.args[1], pruned_utc)

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
        checklist = prr.events.get(note__startswith="TRIAGE:")
        for phrase in ("HUMAN REVIEW REQUIRED", "HUMAN MUST COMPLETE",
                       "incident-hold", "email & Drive", "other users' bookmarks"):
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
                coverage=(head("0", 1.0, start, end),
                          head("1", 0.6, start, end, "recording gap")),
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

    def test_a_head_aware_did_not_report_is_never_a_clean_hold(self):
        cam = camera("cam-1", "Multisensor", "PHS", sources=("0", "1"))

        def one_head(cam, start, end, *, name, description):
            return alta.BookmarkHold(id="bm-1", coverage=(head("0", 1.0, start, end),))

        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([cam], hold=one_head):
            fp = preserve(prr)

        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.GAP)
        self.assertIn("head 1", hold.coverage_note)

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

    def test_coverage_is_scored_against_when_the_bookmark_was_made(self):
        """A camera bookmarked late in a long run is scored on the footage
        that existed when its bookmark was made, not when the run began."""
        run_start = timezone.now()
        fp = FootagePreservation.objects.create(
            request=make_request("camera footage"),
            window_start=run_start - dt.timedelta(hours=1),
            window_end=run_start + dt.timedelta(hours=1),
            site_codes=["PHS"], site_resolution="Named: Peninsula High School",
        )
        clock = [run_start]

        def slow_hold(cam, start, end, *, name, description):
            clock[0] += dt.timedelta(minutes=30)  # earlier cameras took a while
            # The camera stopped recording when the run began.
            stored = (run_start - fp.window_start).total_seconds()
            return alta.BookmarkHold(
                id=f"bm-{cam.id}", coverage=(alta.HeadCoverage("0", stored, stored),),
            )

        with aware([camera("cam-1", "Gym", "PHS")], hold=slow_hold), \
             mock.patch("django.utils.timezone.now", side_effect=lambda: clock[0]):
            tasks.execute_footage_hold(fp.id)

        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.GAP)
        self.assertAlmostEqual(hold.coverage_fraction, 2 / 3)

    def test_a_crash_after_the_bookmark_is_made_keeps_its_id(self):
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([camera("cam-1", "Gym", "PHS")]), \
             mock.patch("apps.records.tasks._bookmark_coverage",
                        side_effect=RuntimeError("boom")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            fp = preserve(prr)

        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.FAILED)
        self.assertEqual(hold.clip_id, "bm-cam-1")
        self.assertIn("bookmark exists", hold.error)
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        manual.assert_called_once()

    def test_a_rerun_never_makes_a_second_bookmark_for_a_camera(self):
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([camera("cam-1", "Gym", "PHS")]) as m, \
             mock.patch("apps.records.tasks._bookmark_coverage",
                        side_effect=RuntimeError("boom")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold"):
            fp = preserve(prr)
            tasks.execute_footage_hold(fp.id)

        m.hold.assert_called_once()

    def test_a_task_that_dies_midway_never_leaves_the_hold_pending(self):
        prr = make_request("camera footage")
        now = timezone.now()
        fp = FootagePreservation.objects.create(
            request=prr, window_start=now - dt.timedelta(days=1), window_end=now,
        )
        with aware(), \
             mock.patch("apps.records.tasks._hold_one_camera",
                        side_effect=RuntimeError("worker lost")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual, \
             self.assertRaises(RuntimeError):
            tasks.execute_footage_hold(fp.id)

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        manual.assert_called_once()
        self.assertIn("stopped before it finished", manual.call_args.kwargs["error"])

    def test_the_sweeper_moves_only_stale_pending_holds_to_manual(self):
        now = timezone.now()
        stale = FootagePreservation.objects.create(
            request=make_request("camera footage"),
            window_start=now - dt.timedelta(days=1), window_end=now,
        )
        FootagePreservation.objects.filter(pk=stale.pk).update(
            hold_heartbeat_at=now - dt.timedelta(minutes=16),
        )
        # Old, but its run is still heartbeating: a long district-wide hold.
        busy = FootagePreservation.objects.create(
            request=make_request("camera footage"),
            window_start=now - dt.timedelta(days=1), window_end=now,
            hold_run_started_at=now - dt.timedelta(hours=2),
        )
        FootagePreservation.objects.filter(pk=busy.pk).update(
            created_at=now - dt.timedelta(hours=2),
        )
        with mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            tasks.sweep_stalled_footage_holds()
            tasks.sweep_stalled_footage_holds()

        stale.refresh_from_db()
        busy.refresh_from_db()
        self.assertEqual(stale.state, FootagePreservation.State.MANUAL)
        self.assertEqual(busy.state, FootagePreservation.State.PENDING)
        manual.assert_called_once()
        self.assertEqual(manual.call_args.args[0].pk, stale.pk)

    def test_the_sweeper_reports_a_stalled_widen_without_downgrading_the_hold(self):
        now = timezone.now()
        fp = FootagePreservation.objects.create(
            request=make_request("camera footage"),
            window_start=now - dt.timedelta(days=1), window_end=now,
            state=FootagePreservation.State.PARTIAL,
            hold_run_started_at=now - dt.timedelta(minutes=30),
        )
        FootagePreservation.objects.filter(pk=fp.pk).update(
            hold_heartbeat_at=now - dt.timedelta(minutes=16),
        )
        with mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            tasks.sweep_stalled_footage_holds()
            tasks.sweep_stalled_footage_holds()

        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertIsNone(fp.hold_run_started_at)
        manual.assert_called_once()
        self.assertIn("widen", manual.call_args.kwargs["error"].lower())

    def test_intake_marks_a_run_in_progress(self):
        prr = make_request(f"video at Peninsula High School on {days_ago(3)}")
        fp = preserve_uncommitted(prr)  # queued, not yet run
        self.assertIsNotNone(fp.hold_run_started_at)
        self.assertIsNotNone(fp.hold_heartbeat_at)

    def test_a_run_heartbeats_on_every_camera_and_clears_its_marker(self):
        seen = []
        real = tasks._hold_one_camera

        def spy(fp, cam, ch):
            row = FootagePreservation.objects.get(pk=fp.pk)
            seen.append((row.hold_run_started_at, row.hold_heartbeat_at))
            real(fp, cam, ch)

        prr = make_request(f"camera footage of the gym on {days_ago(2)}")
        started = timezone.now()
        with aware(), mock.patch("apps.records.tasks._hold_one_camera", side_effect=spy):
            fp = preserve(prr)

        self.assertEqual(len(seen), 3)
        for run_marker, heartbeat in seen:
            self.assertIsNotNone(run_marker)
            self.assertGreaterEqual(heartbeat, started)
        self.assertEqual(sorted(h for _, h in seen), [h for _, h in seen])
        self.assertIsNone(fp.hold_run_started_at)
        self.assertEqual(fp.state, FootagePreservation.State.HELD)

    def test_a_run_the_sweeper_took_over_never_overwrites_its_manual(self):
        real = tasks._hold_one_camera

        def swept_midway(fp, cam, ch):
            if cam.id == "cam-1":  # the sweeper fires while cam-1 is bookmarking
                FootagePreservation.objects.filter(pk=fp.pk).update(
                    state=FootagePreservation.State.MANUAL, hold_run_started_at=None,
                )
            real(fp, cam, ch)

        prr = make_request(f"camera footage of the gym on {days_ago(2)}")
        with aware(), \
             mock.patch("apps.records.tasks._hold_one_camera", side_effect=swept_midway), \
             mock.patch("apps.records.tasks.notifications.notify_footage_hold_complete") as done:
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        done.assert_not_called()
        self.assertTrue(AuditLog.objects.filter(
            action="footage.hold_result_superseded", target=prr.number,
        ).exists())

    def test_the_sweeper_runs_on_the_beat_schedule(self):
        from config.celery import app

        tasks_scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.records.tasks.sweep_stalled_footage_holds", tasks_scheduled)

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

    def test_api_down_with_an_old_window_is_urgent_manual_never_expired(self):
        """Some cameras keep footage longer than the setting, and only Aware
        knows which: with Aware unreachable nothing may be written off."""
        prr = make_request(f"cctv footage of the parking lot on {days_ago(30)}")
        with mock.patch(f"{CLIENT}.list_device_cameras",
                        side_effect=alta.AltaError("Aware unreachable")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual, \
             mock.patch("apps.records.triage.notifications.notify_adequate_search_review") as review:
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.MANUAL)
        self.assertEqual(fp.camera_holds.count(), 0)
        manual.assert_called_once()
        self.assertIn("retention", manual.call_args.kwargs["error"])
        review.assert_not_called()
        prr.refresh_from_db()
        self.assertEqual(prr.status, "received")
        self.assertFalse(
            AuditLog.objects.filter(action="footage.expired_window", target=prr.number).exists()
        )

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
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-2").error,
            "Aware bookmark failed (RuntimeError).",
        )
        notify_manual_hold.assert_called_once()
        kwargs = notify_manual_hold.call_args.kwargs
        self.assertEqual(len(kwargs["cameras"]), 1)
        self.assertIn("Staff Parking (PHS)", kwargs["cameras"][0])
        self.assertIn("failed", kwargs["cameras"][0])
        notify_complete.assert_called_once_with(
            fp, held=2, gap=0, failed=1, not_retained=0,
        )

    def test_a_partial_hold_names_every_camera_not_confirmed_held(self):
        def gap_on_cam_3(cam, start, end, *, name, description):
            if cam.id == "cam-3":
                return alta.BookmarkHold("bm-cam-3", (head("0", 0.4, start, end),))
            return full_hold(cam, start, end, name=name, description=description)

        prr = make_request(f"camera footage of the gym on {days_ago(2)}")
        with aware(hold=gap_on_cam_3), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            fp = preserve(prr)

        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        manual.assert_called_once()
        (line,) = manual.call_args.kwargs["cameras"]
        self.assertIn("Main Office (KMS)", line)
        self.assertIn("gap", line)
        self.assertTrue(manual.call_args.kwargs["condition"].startswith("partial"))

    def test_a_failed_hold_stores_no_address_or_device_id(self):
        import requests

        failures = {
            "alta": (
                alta.AltaError(
                    f"Alta POST {FAKE_BASE}/api/v1/bookmarks returned HTTP 400 "
                    f"BAD_REQUEST for device {DEVICE_GUID}."
                ),
                "Aware bookmark failed (AltaError, HTTP 400).",
            ),
            "transport": (
                requests.ConnectionError(
                    "HTTPSConnectionPool(host='example.us4.aware.avasecurity.com', "
                    "port=443): Max retries exceeded with url: /api/v1/bookmarks"
                ),
                "Aware bookmark failed (ConnectionError).",
            ),
        }
        for label, (failure, expected) in failures.items():
            with self.subTest(case=label):
                prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
                with aware([camera(DEVICE_GUID, "Gym", "PHS")], hold=failure), \
                     mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold"):
                    fp = preserve(prr)

                hold = fp.camera_holds.get()
                self.assertEqual(hold.state, CameraHold.State.FAILED)
                self.assertEqual(hold.error, expected)
                for leak in ("://", "example.us4", DEVICE_GUID, "/api/"):
                    self.assertNotIn(leak, hold.error + hold.coverage_note)

    def test_an_uncertain_create_says_a_bookmark_may_already_exist(self):
        uncertain = alta.AltaWriteUncertain(
            "Alta POST /bookmarks returned HTTP 503. A bookmark may exist anyway: "
            f"check Alta device {DEVICE_GUID} for it before retrying."
        )
        prr = make_request(f"video at Peninsula High School on {days_ago(2)}")
        with aware([camera(DEVICE_GUID, "Gym", "PHS")], hold=uncertain), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            fp = preserve(prr)

        hold = fp.camera_holds.get()
        self.assertEqual(hold.state, CameraHold.State.FAILED)
        self.assertIn("HTTP 503", hold.error)
        self.assertIn("may already exist", hold.error)
        self.assertIn("may already exist", hold.coverage_note)
        self.assertNotIn(DEVICE_GUID, hold.error + hold.coverage_note)
        self.assertIn("may already exist", manual.call_args.kwargs["error"])


NARROWED = "apps.records.triage.notifications.notify_footage_scope_narrowed"


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CAMERA_RETENTION_DAYS=10)
class EnqueueAfterCommitTests(TestCase):
    def test_the_hold_task_is_not_enqueued_until_intake_commits(self):
        # A worker must never read a preservation row that has not committed.
        prr = make_request(f"video at Peninsula High School on {days_ago(1)}")
        with mock.patch("apps.records.tasks.execute_footage_hold.delay") as delay, \
             mock.patch(NARROWED):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                fp = preserve_uncommitted(prr)
            delay.assert_not_called()
            for callback in callbacks:
                callback()

        delay.assert_called_once_with(fp.id)


class HoldSpanCapTests(TestCase):
    """A birth date beside the incident date must not become an 11-year
    bookmark on every camera: hold the most recent days, and tell a human."""

    BORN = "Video of my son (born 2015-03-04) in the gym on 2026-09-22."

    def test_a_long_span_holds_the_most_recent_days_ending_at_the_window_end(self):
        prr = make_request(self.BORN)
        with aware() as m, mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertEqual(fp.window_end, dt.datetime(2026, 9, 23, 7, tzinfo=dt.UTC))
        self.assertEqual(fp.window_start, dt.datetime(2026, 8, 23, 7, tzinfo=dt.UTC))
        self.assertEqual(m.hold.call_args.args[1], fp.window_start)
        self.assertIn("2015-03-04", fp.detected_dates_note)
        self.assertIn("2026-08-23..2026-09-22", fp.detected_dates_note)
        narrowed.assert_called_once()
        reasons = " ".join(narrowed.call_args.kwargs["reasons"])
        self.assertIn("2015-03-04..2026-09-22", reasons)
        self.assertIn("31", reasons)
        self.assertTrue(
            prr.events.filter(note__icontains="narrower than the request").exists()
        )

    @override_settings(CAMERA_HOLD_MAX_SPAN_DAYS=7)
    def test_the_cap_is_the_runtime_setting(self):
        prr = make_request(self.BORN)
        with aware(), mock.patch(NARROWED):
            fp = preserve(prr)

        self.assertEqual(fp.window_start, dt.datetime(2026, 9, 16, 7, tzinfo=dt.UTC))

    def test_a_span_within_the_cap_is_held_whole_and_raises_no_notice(self):
        prr = make_request("Video in the gym on 2026-09-01 and 2026-09-22.")
        with aware(), mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertEqual(fp.window_start, dt.datetime(2026, 9, 1, 7, tzinfo=dt.UTC))
        narrowed.assert_not_called()

    def test_exactly_the_cap_in_calendar_days_is_not_clipped_across_fall_dst(self):
        # 31 calendar days, but 31 days + 1 hour of elapsed time (Nov 1).
        prr = make_request("Video in the gym on 2026-10-10 and 2026-11-09.")
        with aware(), mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertEqual(fp.window_start, dt.datetime(2026, 10, 10, 7, tzinfo=dt.UTC))
        self.assertEqual(fp.window_end, dt.datetime(2026, 11, 10, 8, tzinfo=dt.UTC))
        narrowed.assert_not_called()

    def test_a_clipped_span_across_spring_dst_starts_at_local_midnight(self):
        # 32 calendar days crossing 2026-03-08: hold 2026-02-21..2026-03-23.
        prr = make_request("Video in the gym on 2026-02-20 and 2026-03-23.")
        with aware(), mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertEqual(fp.window_start, dt.datetime(2026, 2, 21, 8, tzinfo=dt.UTC))
        self.assertEqual(fp.window_end, dt.datetime(2026, 3, 24, 7, tzinfo=dt.UTC))
        self.assertIn("2026-02-21..2026-03-23", fp.detected_dates_note)
        reasons = " ".join(narrowed.call_args.kwargs["reasons"])
        self.assertIn("2026-02-20..2026-03-23", reasons)
        self.assertIn("only 2026-02-21..2026-03-23 is held", reasons)

    def test_the_no_date_window_is_unchanged(self):
        prr = make_request("I want the surveillance video of the incident at the gym")
        with aware(), mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertGreaterEqual((fp.window_end - fp.window_start).days, 9)
        narrowed.assert_not_called()

    def test_the_narrowed_notice_is_urgent_and_names_the_clipped_range(self):
        prr = make_request(self.BORN)
        with aware(), mock.patch(
            "apps.records.notifications.enqueue_staff_notification"
        ) as enqueue:
            preserve(prr)

        calls = [
            c.kwargs for c in enqueue.call_args_list
            if c.kwargs["signal_type"] == "footage.hold_scope_narrowed"
        ]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["immediate"])
        self.assertIn("URGENT", calls[0]["subject"])
        self.assertIn("2015-03-04..2026-09-22", " ".join(calls[0]["context"]["reasons"]))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CAMERA_RETENTION_DAYS=10)
class SiteScopeNoticeTests(TestCase):
    """A hold narrowed to the sites a request names misses any place the
    table cannot read ("the bus loop", "the Purdy field"). Say so, URGENTLY."""

    def test_a_named_site_hold_sends_an_urgent_scope_notice(self):
        prr = make_request(f"video at Peninsula High School on {days_ago(3)}")
        with aware(), mock.patch(NARROWED) as narrowed:
            fp = preserve(prr)

        self.assertEqual(fp.site_codes, ["PHS"])
        narrowed.assert_called_once()
        reasons = " ".join(narrowed.call_args.kwargs["reasons"])
        self.assertIn("only Peninsula High School (PHS)", reasons)
        self.assertIn("widen", reasons)
        self.assertTrue(
            prr.events.filter(note__icontains="narrower than the request").exists()
        )

    def test_a_district_wide_hold_sends_no_scope_notice(self):
        prr = make_request(f"camera footage of the gym entrance on {days_ago(3)}")
        with aware(), mock.patch(NARROWED) as narrowed:
            preserve(prr)

        narrowed.assert_not_called()

    def test_a_clipped_span_and_named_site_share_one_notice(self):
        prr = make_request(
            "Video of my son (born 2015-03-04) at Peninsula High School on 2026-09-22."
        )
        with aware(), mock.patch(NARROWED) as narrowed:
            preserve(prr)

        narrowed.assert_called_once()
        self.assertEqual(len(narrowed.call_args.kwargs["reasons"]), 2)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CAMERA_RETENTION_DAYS=10,
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
)
class WidenTests(TestCase):
    """A Records Officer widens a site-narrowed hold to every camera."""

    REASON = "Request also names the bus loop and the district office lot."

    def _officer(self):
        officer = get_user_model().objects.create_user(
            f"widen-officer-{get_user_model().objects.count()}",
            password="pw", is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        return officer

    def _phs_hold(self, hold=full_hold):
        prr = make_request(f"video at Peninsula High School on {days_ago(3)}")
        with aware(hold=hold), mock.patch(NARROWED):
            fp = preserve(prr)
        self.assertEqual(fp.site_codes, ["PHS"])
        return fp

    def _widen(self, fp, reason: object = REASON, actor=None):
        with self.captureOnCommitCallbacks(execute=True):
            return triage.widen_preservation(
                fp, reason=reason, actor=actor or self._officer(),
            )

    def test_widen_holds_district_wide_only_for_cameras_with_no_row(self):
        fp = self._phs_hold()
        with aware() as m:
            fp = self._widen(fp)

        fp.refresh_from_db()
        self.assertEqual(fp.site_codes, [])
        self.assertIn("district-wide", fp.site_resolution)
        self.assertEqual([c.args[0].id for c in m.hold.call_args_list], ["cam-3"])
        self.assertEqual(fp.held_count, 3)
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        self.assertTrue(fp.request.events.filter(note__contains=self.REASON).exists())
        self.assertTrue(
            AuditLog.objects.filter(action="footage.hold_widened",
                                    target=fp.request.number).exists()
        )

    def test_widen_never_retries_a_camera_that_already_has_a_row(self):
        # A failed create may have landed; a widen must not make a second one.
        def fail_cam_2(cam, start, end, *, name, description):
            if cam.id == "cam-2":
                raise alta.AltaWriteUncertain("timed out")
            return full_hold(cam, start, end, name=name, description=description)

        fp = self._phs_hold(hold=fail_cam_2)
        with aware() as m:
            self._widen(fp)

        self.assertEqual([c.args[0].id for c in m.hold.call_args_list], ["cam-3"])
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-2").state, CameraHold.State.FAILED,
        )

    def test_widen_requires_a_substantive_string_reason(self):
        fp = self._phs_hold()
        for bad in ("", "   ", "n/a", None, ["a long enough reason"], 12345678901):
            with self.subTest(reason=bad), self.assertRaises(ValueError):
                self._widen(fp, reason=bad)
        with self.assertRaises(ValueError):
            self._widen(fp, reason="x" * 501)
        fp.refresh_from_db()
        self.assertEqual(fp.site_codes, ["PHS"])

    def test_widen_requires_a_current_records_officer(self):
        fp = self._phs_hold()
        clerk = get_user_model().objects.create_user("clerk", password="pw", is_staff=True)
        with self.assertRaises(PermissionDenied):
            self._widen(fp, actor=clerk)

    def test_widen_refuses_a_district_wide_or_released_hold(self):
        prr = make_request(f"camera footage of the gym entrance on {days_ago(3)}")
        with aware():
            wide = preserve(prr)
        with self.assertRaises(ValueError):
            self._widen(wide)

        fp = self._phs_hold()
        FootagePreservation.objects.filter(pk=fp.pk).update(
            state=FootagePreservation.State.RELEASED,
        )
        with self.assertRaises(ValueError):
            self._widen(fp)

    def test_widen_is_refused_while_the_first_run_is_still_pending(self):
        # Two runs at once could bookmark the same camera twice.
        fp = self._phs_hold()
        FootagePreservation.objects.filter(pk=fp.pk).update(
            state=FootagePreservation.State.PENDING,
        )
        with aware() as m, self.assertRaises(ValueError):
            self._widen(fp)

        m.hold.assert_not_called()
        fp.refresh_from_db()
        self.assertEqual(fp.site_codes, ["PHS"])

    def test_a_camera_another_run_has_claimed_is_never_bookmarked_again(self):
        # The widen created and claimed cam-3's row; the stalled first run
        # reaches cam-3 afterwards and must leave it alone.
        prr = make_request(f"camera footage of the gym entrance on {days_ago(3)}")
        fp = preserve_uncommitted(prr)  # its task never runs: no commit
        CameraHold.objects.create(
            preservation=fp, camera_id="cam-3", camera_name="Main Office", site="KMS",
            hold_claimed_at=timezone.now(),
        )
        with aware() as m:
            tasks.execute_footage_hold(fp.id)

        self.assertEqual(
            sorted(c.args[0].id for c in m.hold.call_args_list), ["cam-1", "cam-2"],
        )

    def test_a_widen_marks_its_run_in_progress_for_the_sweeper(self):
        fp = self._phs_hold()
        self.assertIsNone(fp.hold_run_started_at)
        with mock.patch("apps.records.tasks.execute_footage_hold.delay"):
            self._widen(fp)

        fp.refresh_from_db()
        self.assertIsNotNone(fp.hold_run_started_at)
        self.assertIsNotNone(fp.hold_heartbeat_at)

    def _assert_widen_failure_keeps_the_hold(self, fp, manual):
        before = (fp.state, fp.held_count, confirmation(fp), fp.site_codes)
        fp.refresh_from_db()
        # The scope returns to the sites actually held, so the widen can be
        # posted again; facilities has the district-wide instructions.
        self.assertEqual(
            (fp.state, fp.held_count, confirmation(fp), fp.site_codes), before,
        )
        self.assertIsNone(fp.hold_run_started_at)
        manual.assert_called_once()
        self.assertEqual(manual.call_args.kwargs["site_codes"], [])
        self.assertTrue(fp.request.events.filter(
            note__contains="widen it again once Aware answers",
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action="footage.hold_widen_not_run", target=fp.request.number,
        ).exists())

    def test_a_widen_that_cannot_list_cameras_keeps_what_is_held(self):
        fp = self._phs_hold()
        self.assertEqual(fp.state, FootagePreservation.State.HELD)
        with mock.patch(f"{CLIENT}.list_device_cameras",
                        side_effect=alta.AltaNotConfigured("no token")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            self._widen(fp)

        self._assert_widen_failure_keeps_the_hold(fp, manual)

    def test_a_widen_that_sees_zero_cameras_keeps_what_is_held(self):
        fp = self._phs_hold()
        with aware(cameras=[]), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold",
                        return_value=True) as manual:
            self._widen(fp)

        self._assert_widen_failure_keeps_the_hold(fp, manual)

    def test_a_run_records_its_claim_on_each_camera_it_bookmarks(self):
        fp = self._phs_hold()
        self.assertTrue(all(
            ch.hold_claimed_at is not None for ch in fp.camera_holds.all()
        ))

    def test_a_create_that_may_have_landed_keeps_its_claim_so_no_rerun_repeats_it(self):
        def cam_2_uncertain(cam, start, end, *, name, description):
            if cam.id == "cam-2":
                raise alta.AltaWriteUncertain("timed out")
            return full_hold(cam, start, end, name=name, description=description)

        fp = self._phs_hold(hold=cam_2_uncertain)
        self.assertIsNotNone(fp.camera_holds.get(camera_id="cam-2").hold_claimed_at)
        with aware() as m:
            tasks.execute_footage_hold(fp.id)

        m.hold.assert_not_called()

    def test_a_widen_run_that_crashes_sends_an_urgent_manual_notice(self):
        fp = self._phs_hold()
        with aware(), \
             mock.patch("apps.records.tasks._hold_one_camera", side_effect=RuntimeError("boom")), \
             mock.patch("apps.records.tasks.notifications.notify_footage_manual_hold") as manual, \
             contextlib.suppress(RuntimeError):
            self._widen(fp)

        manual.assert_called()
        self.assertIn("widen", manual.call_args.kwargs["error"].lower())


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
        # Still excluded, but unconfirmed until a release deletes the bookmark.
        self.assertEqual(ch.state, CameraHold.State.RELEASE_FAILED)
        self.assertEqual(ch.excluded_by, officer)
        self.assertEqual(ch.exclusion_reason, reason)
        delete_bookmark.assert_called_once_with("bm-cam-2")
        audit.assert_any_call(
            "footage.camera_release_failed",
            target=ch.preservation.request.number,
            actor=officer,
            camera=ch.camera_name or ch.camera_id,
            error="Aware bookmark delete failed (AltaError).",
        )

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_exclusion_refuses_a_released_preservation(self, delete_bookmark):
        ch = self._held_camera()
        FootagePreservation.objects.filter(pk=ch.preservation_id).update(
            state=FootagePreservation.State.RELEASED,
        )
        ch.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "released"), \
             self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(
                ch, "Camera faces staff parking; not the gym entrance.",
                actor=self._records_officer(),
            )
        delete_bookmark.assert_not_called()
        ch.refresh_from_db()
        self.assertIsNone(ch.excluded_at)

    def test_exclusion_refuses_a_non_text_reason(self):
        ch = self._held_camera()
        with self.assertRaises(ValueError):
            triage.exclude_camera(
                ch, ["a reason long enough"], actor=self._records_officer(),
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

    def test_a_camera_still_pending_is_counted_as_pending_not_failed(self):
        fp = self._preserve("video at Peninsula High School on 2026-09-22")
        fp.camera_holds.filter(camera_id="cam-2").update(
            state=CameraHold.State.PENDING, clip_id="",
        )
        FootagePreservation.objects.filter(pk=fp.pk).update(
            state=FootagePreservation.State.PARTIAL,
        )
        fp.refresh_from_db()
        text = triage.preservation_confirmation_text(fp)
        assert text is not None
        self.assertIn("1 pending", text)
        self.assertTrue(text.endswith("0 failed."), text)

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
                return alta.BookmarkHold("bm-cam-3", (head("0", 0.5, start, end),))
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
        with with_empty_site():
            fp = self._preserve(
                "video at Peninsula High School and Zephyr Hollow on 2026-09-22",
                cameras=cameras,
            )
            text = triage.preservation_confirmation_text(fp)
        self.assertEqual(fp.state, FootagePreservation.State.PARTIAL)
        self.assertEqual(
            text,
            f"1 camera at Peninsula High School held {self.WINDOW} "
            "(Aware bookmarks, no expiry); 0 cameras have footage gaps; "
            "1 with no retained footage for the window; 0 failed; "
            "none held at Zephyr Hollow Elementary School.",
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
                    id="bm-cam-3", coverage=(head("0", 0.4, start, end),),
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
        self.assertEqual(failure.detail["error"], "Aware bookmark delete failed (AltaError).")

    # A bookmark Aware would not delete stays visible and can be retried.

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_a_failed_delete_is_recorded_and_a_repost_retries_only_it(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()

        def refuse_cam_3(bookmark_id):
            if bookmark_id == "bm-cam-3":
                raise RuntimeError(f"{FAKE_BASE}/api/v1/devices/{DEVICE_GUID} reset")

        delete_bookmark.side_effect = refuse_cam_3
        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        failed = fp.camera_holds.get(camera_id="cam-3")
        self.assertEqual(failed.state, CameraHold.State.RELEASE_FAILED)
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-1").state, CameraHold.State.RELEASED,
        )
        log = AuditLog.objects.get(action="footage.bookmark_release_failed")
        self.assertEqual(log.detail["error"], "Aware bookmark delete failed (RuntimeError).")
        self.assertTrue(
            fp.request.events.filter(note__contains="could not be deleted").exists()
        )

        delete_bookmark.reset_mock()
        delete_bookmark.side_effect = None
        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        delete_bookmark.assert_called_once_with("bm-cam-3")
        failed.refresh_from_db()
        self.assertEqual(failed.state, CameraHold.State.RELEASED)
        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.RELEASED)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_a_bookmark_is_not_released_until_aware_confirms_its_delete(
        self, delete_bookmark,
    ):
        # A crash between commit and the deletes must leave them retryable.
        fp = self._held_preservation()
        with self.captureOnCommitCallbacks(execute=False) as deletes:
            self._release(fp)

        delete_bookmark.assert_not_called()
        self.assertEqual(
            set(fp.camera_holds.values_list("state", flat=True)),
            {CameraHold.State.RELEASE_FAILED},
        )
        fp.refresh_from_db()
        self.assertEqual(fp.state, FootagePreservation.State.RELEASED)
        self.assertTrue(triage.is_releasable(fp))

        for callback in deletes:
            callback()

        self.assertEqual(delete_bookmark.call_count, 3)
        self.assertEqual(
            set(fp.camera_holds.values_list("state", flat=True)),
            {CameraHold.State.RELEASED},
        )

    def test_an_unconfirmed_delete_is_worded_as_not_yet_confirmed_deleted(self):
        self.assertIn(
            "not yet confirmed deleted", CameraHold.State.RELEASE_FAILED.label.lower(),
        )

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_deletes_a_failed_hold_that_recorded_a_bookmark(
        self, delete_bookmark,
    ):
        # The hold crashed after Aware made the bookmark (C2): FAILED, with an id.
        fp = self._held_preservation()
        fp.camera_holds.filter(camera_id="cam-2").update(state=CameraHold.State.FAILED)

        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        self.assertIn("bm-cam-2", [c.args[0] for c in delete_bookmark.call_args_list])
        self.assertEqual(
            fp.camera_holds.get(camera_id="cam-2").state, CameraHold.State.RELEASED,
        )

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_a_released_preservation_with_nothing_failed_is_not_released_again(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)
        delete_bookmark.reset_mock()

        with self.assertRaises(ValueError), self.captureOnCommitCallbacks(execute=True):
            self._release(fp)
        delete_bookmark.assert_not_called()

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_a_failed_exclusion_delete_is_audited_without_detail(self, delete_bookmark):
        fp = self._held_preservation()
        delete_bookmark.side_effect = RuntimeError(f"{FAKE_BASE}/{DEVICE_GUID}")

        with self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(
                fp.camera_holds.get(camera_id="cam-2"),
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=self._records_officer(),
            )

        log = AuditLog.objects.get(action="footage.camera_release_failed")
        self.assertEqual(log.detail["error"], "Aware bookmark delete failed (RuntimeError).")

    def _exclude_cam_2_with_a_failed_delete(self, fp, delete_bookmark):
        delete_bookmark.side_effect = alta.AltaError("Aware delete unavailable")
        with self.captureOnCommitCallbacks(execute=True):
            triage.exclude_camera(
                fp.camera_holds.get(camera_id="cam-2"),
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=self._records_officer(),
            )
        delete_bookmark.reset_mock()
        delete_bookmark.side_effect = None
        excluded = fp.camera_holds.get(camera_id="cam-2")
        self.assertEqual(excluded.state, CameraHold.State.RELEASE_FAILED)
        self.assertIsNotNone(excluded.excluded_at)
        return excluded

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_release_deletes_an_excluded_bookmark_aware_would_not_delete(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        excluded = self._exclude_cam_2_with_a_failed_delete(fp, delete_bookmark)

        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        self.assertIn("bm-cam-2", [c.args[0] for c in delete_bookmark.call_args_list])
        excluded.refresh_from_db()
        self.assertEqual(excluded.state, CameraHold.State.RELEASED)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_a_release_retry_picks_up_a_failed_exclusion_delete(self, delete_bookmark):
        fp = self._held_preservation()
        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)
        excluded = fp.camera_holds.get(camera_id="cam-2")
        # The exclusion's delete failed after the release removed the rest.
        CameraHold.objects.filter(pk=excluded.pk).update(
            state=CameraHold.State.RELEASE_FAILED, excluded_at=timezone.now(),
        )
        delete_bookmark.reset_mock()
        fp.refresh_from_db()
        self.assertTrue(triage.is_releasable(fp))

        with self.captureOnCommitCallbacks(execute=True):
            self._release(fp)

        delete_bookmark.assert_called_once_with("bm-cam-2")
        excluded.refresh_from_db()
        self.assertEqual(excluded.state, CameraHold.State.RELEASED)

    @mock.patch(f"{CLIENT}.delete_bookmark")
    def test_an_exclusion_is_unconfirmed_until_aware_deletes_its_bookmark(
        self, delete_bookmark,
    ):
        fp = self._held_preservation()
        hold = fp.camera_holds.get(camera_id="cam-2")
        with self.captureOnCommitCallbacks(execute=False) as deletes:
            triage.exclude_camera(
                hold,
                "Camera faces staff parking; request concerns the gym entrance only.",
                actor=self._records_officer(),
            )
        hold.refresh_from_db()
        self.assertEqual(hold.state, CameraHold.State.RELEASE_FAILED)

        for callback in deletes:
            callback()

        hold.refresh_from_db()
        self.assertEqual(hold.state, CameraHold.State.RELEASED)
