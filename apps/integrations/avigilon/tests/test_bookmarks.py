"""Bookmark holds: create, confirm coverage, delete.

A bookmark exempts its window from pruning, but creating one proves nothing
about whether the footage exists. After the create, each head's store
timeline is read and the covered fraction reported; reading coverage never
raises, because by then the bookmark exists and the caller must learn its id.
"""

import dataclasses
import datetime as dt
import itertools
from unittest import mock
from zoneinfo import ZoneInfo

import requests

from apps.integrations.avigilon import client
from apps.integrations.avigilon.tests import fakes

UTC = dt.UTC
PACIFIC = ZoneInfo("America/Los_Angeles")
START = dt.datetime(2026, 11, 5, 11, 0, tzinfo=UTC)
END = dt.datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
TIMELINE = "/store/timelineInformation"


def _created_at(bookmark_id):
    return {"Location": f"/api/v1/bookmarks/{bookmark_id}"}


def _camera(*sources, server_group=fakes.SERVER_GROUP):
    return client.Camera(
        id="guid-1",
        name="Front door",
        site="SITE-A",
        device_group_id=fakes.GROUP_A,
        server_group_id=server_group,
        status="CONNECTED",
        pruned_to=None,
        sources=tuple(sources or ("1",)),
    )


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(value):
    return int(value.timestamp() * 1000)


def _store(start, end):
    return {
        "store_start_time": _iso(start) if isinstance(start, dt.datetime) else start,
        "store_end_time": _iso(end) if isinstance(end, dt.datetime) else end,
    }


def _stores_by_head(segments):
    """A timeline answer keyed on the head the request asked about."""

    def answer(record):
        (source,) = record["json"]["deviceSources"]
        return {"stores": [_store(*s) for s in segments.get(source["source"], [])]}

    return answer


class _BookmarkTestCase(fakes.TenantTestCase):
    def setUp(self):
        super().setUp()
        # Live tenant, 2026-11-05: 201, empty body, id only in Location.
        self.tenant.on("POST", "/bookmarks", fakes.response(201, headers=_created_at("bm-1")))
        self.tenant.on("POST", TIMELINE, {"stores": []})
        now = mock.patch.object(
            client, "_now", return_value=dt.datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
        )
        now.start()
        self.addCleanup(now.stop)

    def _hold(self, camera=None, start=START, end=END, **kwargs):
        kwargs.setdefault("name", "PRR 2026-0042 footage hold")
        kwargs.setdefault("description", "Public records preservation hold.")
        return client.create_bookmark_hold(camera or _camera(), start, end, **kwargs)

    def _posted(self):
        (call,) = self.tenant.calls_to("POST", "/bookmarks")
        return call["json"]


class CreateBookmarkTests(_BookmarkTestCase):
    def test_the_bookmark_body(self):
        hold = self._hold(_camera("1", "3"))

        self.assertEqual(hold.id, "bm-1")
        self.assertEqual(
            self._posted(),
            {
                "name": "PRR 2026-0042 footage hold",
                "description": "Public records preservation hold.",
                "expiry_time": None,
                "origin": "ALTA Aware",
                "able_to_download": True,
                "allow_audio": False,
                "device_infos": [
                    {
                        "device_id": "guid-1",
                        "server_group_id": fakes.SERVER_GROUP,
                        "source": sid,
                        "start_time": "2026-11-05T11:00:00Z",
                        "end_time": "2026-11-05T12:00:00Z",
                    }
                    for sid in ("1", "3")
                ],
            },
        )

    def test_no_entry_carries_a_source_id(self):
        self._hold(_camera("1", "2"))

        for entry in self._posted()["device_infos"]:
            self.assertNotIn("source_id", entry)

    def test_a_multisensor_camera_gets_one_entry_per_head(self):
        self._hold(_camera("0", "1", "2", "3"))

        self.assertEqual([e["source"] for e in self._posted()["device_infos"]], ["0", "1", "2", "3"])

    def test_repeated_heads_are_sent_once(self):
        self._hold(_camera("1", "1", "2"))

        self.assertEqual([e["source"] for e in self._posted()["device_infos"]], ["1", "2"])

    def test_times_are_utc_z_across_the_fall_back_change(self):
        """2026-11-01 is 25 hours long in Pacific time."""
        self._hold(
            start=dt.datetime(2026, 11, 1, 0, 0, tzinfo=PACIFIC),
            end=dt.datetime(2026, 11, 2, 0, 0, tzinfo=PACIFIC),
        )

        (entry,) = self._posted()["device_infos"]
        self.assertEqual(entry["start_time"], "2026-11-01T07:00:00Z")
        self.assertEqual(entry["end_time"], "2026-11-02T08:00:00Z")

    def test_times_are_widened_to_whole_seconds(self):
        self._hold(
            start=START.replace(microsecond=500_000),
            end=END.replace(microsecond=200_000),
        )

        (entry,) = self._posted()["device_infos"]
        self.assertEqual(entry["start_time"], "2026-11-05T11:00:00Z")
        self.assertEqual(entry["end_time"], "2026-11-05T12:00:01Z")

    def test_the_id_comes_from_the_location_of_an_empty_201(self):
        self.tenant.on(
            "POST", "/bookmarks", fakes.response(201, headers=_created_at("c7f8e364-abac"))
        )

        self.assertEqual(self._hold().id, "c7f8e364-abac")

    def test_an_absolute_location_on_the_configured_origin_is_accepted(self):
        location = fakes.BASE + "/api/v1/bookmarks/bm-abs"
        self.tenant.on("POST", "/bookmarks", fakes.response(201, headers={"Location": location}))

        self.assertEqual(self._hold().id, "bm-abs")

    def test_a_location_that_is_not_one_bookmark_raises(self):
        for location in (
            "/api/v1/devices/bm-1",
            "/api/v1/bookmarks/",
            "/api/v1/bookmarks/a/b",
            "/api/v1/bookmarks/bm-1?x=1",
            "https://elsewhere.example.com/api/v1/bookmarks/bm-1",
        ):
            with self.subTest(location=location):
                self.tenant.on(
                    "POST", "/bookmarks", fakes.response(201, headers={"Location": location})
                )
                with self.assertRaisesRegex(client.AltaError, "id"):
                    self._hold()

    def test_a_200_with_an_id_is_accepted(self):
        self.tenant.on("POST", "/bookmarks", {"id": "bm-200"})

        self.assertEqual(self._hold().id, "bm-200")

    def test_refusals_before_any_request(self):
        naive = dt.datetime(2026, 11, 5, 11, 0)
        cases = {
            "naive start": {"start": naive},
            "naive end": {"end": naive + dt.timedelta(hours=1)},
            "empty window": {"start": END, "end": END},
            "backwards window": {"start": END, "end": START},
            "no heads": {"camera": dataclasses.replace(_camera(), sources=())},
            "no server group": {"camera": _camera(server_group="")},
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(client.AltaError):
                    self._hold(**kwargs)
        self.assertEqual(self.tenant.calls, [])

    def test_a_camera_whose_every_head_is_streamless_is_refused_for_a_hand_check(self):
        camera = dataclasses.replace(_camera(), sources=(), skipped_sources=("1", "2"))

        with self.assertRaisesRegex(client.AltaError, "2 head.*no stream.*by hand"):
            self._hold(camera)

        self.assertEqual(self.tenant.calls, [])

    def test_a_server_error_on_create_is_not_retried(self):
        self.tenant.on("POST", "/bookmarks", fakes.response(503))

        with self.assertRaisesRegex(client.AltaError, "503") as raised:
            self._hold()

        self.assertEqual(len(self.tenant.calls_to("POST", "/bookmarks")), 1)
        self.sleep.assert_not_called()
        self._assert_may_exist(raised.exception)

    def test_a_timeout_on_create_is_not_retried(self):
        self.tenant.on("POST", "/bookmarks", requests.Timeout("slow"))

        with self.assertRaises(client.AltaError) as raised:
            self._hold()

        self.assertEqual(len(self.tenant.calls_to("POST", "/bookmarks")), 1)
        self._assert_may_exist(raised.exception)

    def test_every_server_error_on_create_may_have_landed(self):
        for status in (500, 502, 504):
            with self.subTest(status=status):
                self.tenant.on("POST", "/bookmarks", fakes.response(status))
                with self.assertRaises(client.AltaError) as raised:
                    self._hold()
                self._assert_may_exist(raised.exception)

    def test_a_refused_create_does_not_claim_a_bookmark_may_exist(self):
        for answer in (
            fakes.response(400, {"kind": "BAD_REQUEST"}),
            fakes.response(403, {"kind": "PERMISSION_DENIED"}),
        ):
            with self.subTest(status=answer.status_code):
                self.tenant.on("POST", "/bookmarks", answer)
                with self.assertRaises(client.AltaError) as raised:
                    self._hold()
                self.assertNotIsInstance(raised.exception, client.AltaWriteUncertain)
                self.assertNotIn("may exist", str(raised.exception))

    def _assert_may_exist(self, exc):
        self.assertIsInstance(exc, client.AltaWriteUncertain)
        message = str(exc)
        self.assertIn("may exist", message)
        self.assertIn("guid-1", message)
        self.assertIn("before retrying", message)

    def test_an_expired_session_on_create_logs_in_and_sends_it_once_more(self):
        """A 401 is refused before the create runs, so resending cannot
        duplicate the bookmark."""
        client.list_device_groups()
        self.tenant.expire_all_sessions()

        self.assertEqual(self._hold().id, "bm-1")

        self.assertEqual(self.tenant.logins, 2)
        self.assertEqual(len(self.tenant.calls_to("POST", "/bookmarks")), 2)

    def test_a_create_without_an_id_raises(self):
        for answer in (fakes.response(201, {}), fakes.response(201), fakes.response(201, [])):
            with self.subTest(answer=answer.content):
                self.tenant.on("POST", "/bookmarks", answer)
                with self.assertRaisesRegex(client.AltaError, "id"):
                    self._hold()

    def test_a_bad_request_reports_the_kind_and_message(self):
        self.tenant.on(
            "POST",
            "/bookmarks",
            fakes.response(400, {"kind": "BAD_REQUEST", "message": "end before start"}),
        )

        with self.assertRaisesRegex(client.AltaError, "BAD_REQUEST: end before start"):
            self._hold()


class CoverageTests(_BookmarkTestCase):
    def _coverage(self, segments, camera=None, start=START, end=END):
        self.tenant.on("POST", TIMELINE, _stores_by_head(segments))
        return self._hold(camera, start, end).coverage

    def test_full_coverage(self):
        (head,) = self._coverage({"1": [(START - dt.timedelta(hours=1), END + dt.timedelta(hours=1))]})

        self.assertEqual(head, client.HeadCoverage("1", 1.0))

    def test_partial_coverage(self):
        (head,) = self._coverage({"1": [(START, START + dt.timedelta(minutes=30))]})

        self.assertEqual(head, client.HeadCoverage("1", 0.5))

    def test_no_coverage(self):
        """An explicit empty stores list is a confirmed 0%, with no error."""
        (head,) = self._coverage({})

        self.assertEqual(head, client.HeadCoverage("1", 0.0))

    def test_a_timeline_without_stores_is_unconfirmed_not_zero(self):
        self.tenant.on("POST", TIMELINE, {"timeline": []})

        (head,) = self._hold().coverage

        self.assertEqual(head.covered_fraction, 0.0)
        self.assertIn("no stores", head.error)

    def test_each_head_is_measured_on_its_own(self):
        coverage = self._coverage(
            {"1": [(START, END)], "2": [(START, START + dt.timedelta(minutes=15))]},
            camera=_camera("1", "2", "3"),
        )

        self.assertEqual(
            coverage,
            (
                client.HeadCoverage("1", 1.0),
                client.HeadCoverage("2", 0.25),
                client.HeadCoverage("3", 0.0),
            ),
        )

    def test_overlapping_segments_are_not_counted_twice(self):
        (head,) = self._coverage(
            {
                "1": [
                    (START, START + dt.timedelta(minutes=40)),
                    (START + dt.timedelta(minutes=20), START + dt.timedelta(minutes=50)),
                ]
            }
        )

        self.assertAlmostEqual(head.covered_fraction, 50 / 60)

    def test_epoch_millisecond_store_times_are_read(self):
        (head,) = self._coverage({"1": [(_ms(START), _ms(START + dt.timedelta(minutes=45)))]})

        self.assertAlmostEqual(head.covered_fraction, 0.75)

    def test_an_open_segment_runs_to_now(self):
        with mock.patch.object(
            client, "_now", return_value=START + dt.timedelta(minutes=45)
        ):
            (head,) = self._coverage({"1": [(START, None)]})

        self.assertAlmostEqual(head.covered_fraction, 0.75)

    def test_the_timeline_request(self):
        self._coverage({}, camera=_camera("2"))

        (call,) = self.tenant.calls_to("POST", TIMELINE)
        self.assertEqual(
            call["json"],
            {
                "deviceSources": [
                    {"deviceId": "guid-1", "serverGroupId": fakes.SERVER_GROUP, "source": "2"}
                ],
                "timeAfter": _ms(START),
                "timeBefore": _ms(END),
                "timeStep": 60_000,
                "includeStoreInfo": True,
            },
        )

    def test_long_windows_are_read_in_chunks_of_at_most_six_days(self):
        start = dt.datetime(2026, 10, 1, tzinfo=UTC)
        end = start + dt.timedelta(days=13)

        (head,) = self._coverage({"1": [(start, end)]}, start=start, end=end)

        calls = self.tenant.calls_to("POST", TIMELINE)
        windows = [(c["json"]["timeAfter"], c["json"]["timeBefore"]) for c in calls]
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], _ms(start))
        self.assertEqual(windows[-1][1], _ms(end))
        for (_, before), (after, _) in itertools.pairwise(windows):
            self.assertEqual(before, after)
        for after, before in windows:
            self.assertLessEqual(before - after, 6 * 86_400_000)
        for call in calls:
            body = call["json"]
            self.assertEqual(
                body["timeStep"], max(60_000, (body["timeBefore"] - body["timeAfter"]) // 100)
            )
        self.assertEqual(head.covered_fraction, 1.0)

    def test_a_transient_timeline_error_is_retried(self):
        self.tenant.on("POST", TIMELINE, fakes.response(503), {"stores": [_store(START, END)]})

        (head,) = self._hold().coverage

        self.assertEqual(head, client.HeadCoverage("1", 1.0))

    def test_unreadable_coverage_is_reported_never_raised(self):
        answers = {
            "bad request": fakes.response(400, {"kind": "BAD_REQUEST"}),
            "not an object": fakes.response(200, ["stores"]),
            "stores not a list": {"stores": "none"},
            "no stores key": {},
            "stores null": {"stores": None},
            "store without a start": {"stores": [{"store_end_time": _iso(END)}]},
            "unparseable time": {"stores": [_store("soon", _iso(END))]},
            "transport": requests.ConnectionError("reset"),
            "permission": fakes.response(403, {"kind": "PERMISSION_DENIED"}),
        }
        for label, answer in answers.items():
            with self.subTest(case=label):
                self.tenant.on("POST", TIMELINE, answer)

                hold = self._hold()

                self.assertEqual(hold.id, "bm-1")
                (head,) = hold.coverage
                self.assertEqual(head.source, "1")
                self.assertEqual(head.covered_fraction, 0.0)
                self.assertTrue(head.error)

    def test_a_streamless_head_is_a_gap_in_coverage_but_not_bookmarked(self):
        """Whether Aware accepts a streamless head is unverified, so it is not
        sent; it is reported as an uncovered head for a hand check."""
        camera = dataclasses.replace(_camera("1"), skipped_sources=("2", "3"))

        coverage = self._coverage({"1": [(START, END)]}, camera=camera)

        self.assertEqual([e["source"] for e in self._posted()["device_infos"]], ["1"])
        self.assertEqual(
            coverage,
            (
                client.HeadCoverage("1", 1.0),
                client.HeadCoverage("2", 0.0, client.SKIPPED_HEAD_ERROR),
                client.HeadCoverage("3", 0.0, client.SKIPPED_HEAD_ERROR),
            ),
        )
        self.assertEqual(
            client.SKIPPED_HEAD_ERROR, "head reports no stream; not bookmarked, check by hand"
        )
        self.assertEqual(
            [c["json"]["deviceSources"][0]["source"] for c in self.tenant.calls_to("POST", TIMELINE)],
            ["1"],
        )

    def test_a_skipped_head_also_listed_as_a_source_is_measured_not_doubled(self):
        camera = dataclasses.replace(_camera("1"), skipped_sources=("1", "2", "2"))

        coverage = self._coverage({"1": [(START, END)]}, camera=camera)

        self.assertEqual([c.source for c in coverage], ["1", "2"])
        self.assertEqual(coverage[0], client.HeadCoverage("1", 1.0))

    def test_one_unreadable_head_does_not_hide_the_others(self):
        def answer(record):
            (source,) = record["json"]["deviceSources"]
            if source["source"] == "2":
                return fakes.response(400, {"kind": "BAD_REQUEST"})
            return {"stores": [_store(START, END)]}

        self.tenant.on("POST", TIMELINE, answer)

        first, second = self._hold(_camera("1", "2")).coverage

        self.assertEqual(first, client.HeadCoverage("1", 1.0))
        self.assertEqual(second.covered_fraction, 0.0)
        self.assertIn("BAD_REQUEST", second.error)


class DeleteBookmarkTests(fakes.TenantTestCase):
    def test_a_delete(self):
        self.tenant.on("DELETE", "/bookmarks/bm-1", fakes.response(200, {}))

        client.delete_bookmark("bm-1")

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-1")), 1)

    def test_an_empty_success_body_is_fine(self):
        self.tenant.on("DELETE", "/bookmarks/bm-1", fakes.response(200))

        client.delete_bookmark("bm-1")

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-1")), 1)

    def test_a_bookmark_already_gone_is_not_an_error(self):
        self.tenant.on("DELETE", "/bookmarks/bm-1", fakes.json_not_found())

        client.delete_bookmark("bm-1")
        client.delete_bookmark("bm-1")

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-1")), 2)

    def test_a_text_404_is_a_bad_path_and_raises(self):
        self.tenant.on("DELETE", "/bookmarks/bm-1", fakes.text_not_found())

        with self.assertRaises(client.AltaError):
            client.delete_bookmark("bm-1")

    def test_other_failures_raise_without_retrying(self):
        for answer in (fakes.response(500), fakes.response(400, {"kind": "BAD_REQUEST"})):
            with self.subTest(status=answer.status_code):
                self.tenant.calls.clear()
                self.tenant.on("DELETE", "/bookmarks/bm-1", answer)
                with self.assertRaises(client.AltaError):
                    client.delete_bookmark("bm-1")
                self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-1")), 1)

    def test_a_permission_denial_raises(self):
        self.tenant.on(
            "DELETE", "/bookmarks/bm-1", fakes.response(403, {"kind": "PERMISSION_DENIED"})
        )

        with self.assertRaises(client.AltaPermissionDenied):
            client.delete_bookmark("bm-1")

    def test_the_id_is_escaped_into_one_path_segment(self):
        self.tenant.on("DELETE", "/bookmarks/a%2F..%2Fdevices", fakes.response(200))

        client.delete_bookmark("a/../devices")

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/a%2F..%2Fdevices")), 1)

    def test_a_blank_id_is_refused_before_any_request(self):
        with self.assertRaises(client.AltaError):
            client.delete_bookmark("  ")

        self.assertEqual(self.tenant.calls, [])


class LegacyClipInterfaceTests(_BookmarkTestCase):
    def test_create_clip_hold_returns_the_bookmark_id(self):
        self.tenant.on(
            "GET", "/devices", [fakes.device("guid-1", "Front", heads=[fakes.head("1"), fakes.head("2")])]
        )

        clip_id = client.create_clip_hold("guid-1", START, END, label="PRR hold")

        self.assertEqual(clip_id, "bm-1")
        posted = self._posted()
        self.assertEqual(posted["name"], "PRR hold")
        self.assertEqual(posted["expiry_time"], None)
        self.assertEqual([e["source"] for e in posted["device_infos"]], ["1", "2"])

    def test_create_clip_hold_on_an_unknown_camera_raises(self):
        with self.assertRaises(client.AltaError):
            client.create_clip_hold("no-such-guid", START, END, label="PRR hold")

        self.assertEqual(self.tenant.calls_to("POST", "/bookmarks"), [])

    def test_delete_clip_deletes_the_bookmark(self):
        self.tenant.on("DELETE", "/bookmarks/bm-1", fakes.response(200))

        client.delete_clip("bm-1")

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-1")), 1)
