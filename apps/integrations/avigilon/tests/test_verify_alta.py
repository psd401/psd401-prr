"""``manage.py verify_alta``: a read-only check, plus an opt-in smoke bookmark.

The plain run signs in once and only reads. ``--smoke-bookmark`` creates one
short bookmark, proves it was stored as sent, measures its coverage, and
deletes it, and the delete happens whatever fails in between. Output is ids,
counts, and times: never a device name, a hostname, or an account.
"""

import datetime as dt
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from apps.integrations.avigilon import client
from apps.integrations.avigilon.tests import fakes

UTC = dt.UTC
NOW = dt.datetime(2026, 11, 5, 18, 30, 15, 250_000, tzinfo=UTC)
SMOKE_END = "2026-11-05T17:30:15Z"
SMOKE_START = "2026-11-05T17:20:15Z"
DEVICE = "guid-smoke"


class ChecksTests(fakes.TenantTestCase):
    def setUp(self):
        super().setUp()
        self.tenant.on(
            "GET",
            "/devices",
            [
                fakes.device("a1", "A one", group=fakes.GROUP_A, heads=[fakes.head("1"), fakes.head("2")]),
                fakes.device("b1", "B one", group=fakes.GROUP_B, heads=[fakes.head("1", storing="no_stream_available")]),
                fakes.device("e1", "Retired", group=fakes.GROUP_EMPTY, active=False),
            ],
        )

    def test_checks_counts_devices_heads_and_empty_groups(self):
        self.assertEqual(
            client.checks(),
            {
                "ok": True,
                "device_count": 2,
                "inactive_devices": 1,
                "head_count": 2,
                "devices_without_heads": 1,
                "group_count": 3,
                "empty_groups": ["SITE-EMPTY"],
            },
        )

    def test_checks_signs_in_once_and_only_reads(self):
        client.checks()

        self.assertEqual(self.tenant.logins, 1)
        self.assertEqual({c["method"] for c in self.tenant.api_calls()}, {"GET"})

    def test_the_command_prints_counts_and_codes_only(self):
        out = StringIO()

        call_command("verify_alta", stdout=out)

        output = out.getvalue()
        self.assertIn("2 active devices", output)
        self.assertIn("1 inactive device", output)
        self.assertIn("2 recording heads", output)
        self.assertIn("1 with no recording head", output)
        self.assertIn("3 device groups", output)
        self.assertIn("SITE-EMPTY", output)
        for private in ("A one", "B one", fakes.BASE, fakes.USERNAME):
            self.assertNotIn(private, output)
        self.assertEqual(self.tenant.calls_to("POST", "/bookmarks"), [])

    def test_the_command_fails_on_an_api_error(self):
        self.tenant.on("GET", "/devices", fakes.text_not_found())

        with self.assertRaisesRegex(CommandError, "API error"):
            call_command("verify_alta", stdout=StringIO())

    def test_the_command_fails_when_not_configured(self):
        with self.settings(ALTA_API_PASSWORD=""):
            with self.assertRaisesRegex(CommandError, "Not configured"):
                call_command("verify_alta", stdout=StringIO())


class SmokeBookmarkTests(fakes.TenantTestCase):
    def setUp(self):
        super().setUp()
        self.tenant.on(
            "GET",
            "/devices",
            [fakes.device(DEVICE, "Smoke cam", heads=[fakes.head("1"), fakes.head("2")])],
        )
        self.tenant.on("POST", "/bookmarks", fakes.response(201, {"id": "bm-smoke"}))
        self.tenant.on(
            "POST",
            "/store/timelineInformation",
            {"stores": [{"store_start_time": "2026-11-05T17:00:00Z", "store_end_time": None}]},
        )
        self.tenant.on("GET", "/bookmarks/bm-smoke", self._stored(), fakes.json_not_found())
        self.tenant.on("DELETE", "/bookmarks/bm-smoke", fakes.response(200))
        now = mock.patch.object(client, "_now", return_value=NOW)
        now.start()
        self.addCleanup(now.stop)

    def _stored(self, **changes):
        bookmark = {
            "id": "bm-smoke",
            "name": "PRR smoke test — delete",
            "expiry_time": None,
            "device_infos": [
                {
                    "id": f"entry-{sid}",
                    "device_id": DEVICE,
                    "server_group_id": fakes.SERVER_GROUP,
                    "source": sid,
                    "source_id": "00000000-0000-0000-0000-000000000000",
                    "start_time": SMOKE_START,
                    "end_time": SMOKE_END,
                }
                for sid in ("1", "2")
            ],
        }
        bookmark.update(changes)
        return bookmark

    def _run(self):
        out = StringIO()
        call_command("verify_alta", "--smoke-bookmark", DEVICE, stdout=out)
        return out.getvalue()

    def test_the_smoke_bookmark_is_created_checked_and_deleted(self):
        output = self._run()

        (created,) = self.tenant.calls_to("POST", "/bookmarks")
        self.assertEqual(created["json"]["name"], "PRR smoke test — delete")
        self.assertIsNone(created["json"]["expiry_time"])
        self.assertEqual(
            [(e["source"], e["start_time"], e["end_time"]) for e in created["json"]["device_infos"]],
            [("1", SMOKE_START, SMOKE_END), ("2", SMOKE_START, SMOKE_END)],
        )
        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-smoke")), 1)
        self.assertEqual(len(self.tenant.calls_to("GET", "/bookmarks/bm-smoke")), 2)
        for expected in ("bm-smoke", DEVICE, SMOKE_START, SMOKE_END, "2 heads", "100%", "NOT_FOUND"):
            self.assertIn(expected, output)
        for private in ("Smoke cam", fakes.BASE, fakes.USERNAME):
            self.assertNotIn(private, output)

    def test_the_smoke_result_is_reported_by_the_client(self):
        result = client.smoke_test_bookmark(DEVICE)

        self.assertTrue(result["ok"])
        self.assertEqual(result["bookmark_id"], "bm-smoke")
        self.assertEqual(result["start"], SMOKE_START)
        self.assertEqual(result["end"], SMOKE_END)
        self.assertEqual(result["heads"], 2)
        self.assertEqual(result["stored_entries"], 2)
        self.assertEqual([c["covered_fraction"] for c in result["coverage"]], [1.0, 1.0])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["confirmed_gone"])
        self.assertEqual(result["failures"], [])

    def test_a_failed_read_back_still_deletes(self):
        self.tenant.on("GET", "/bookmarks/bm-smoke", fakes.response(500), fakes.json_not_found())

        with self.assertRaisesRegex(CommandError, "read-back"):
            self._run()

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-smoke")), 1)

    def test_an_unexpected_error_in_the_middle_still_deletes(self):
        with mock.patch.object(client, "_readback_failures", side_effect=KeyError("boom")):
            with self.assertRaises(CommandError):
                self._run()

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-smoke")), 1)

    def test_a_stored_expiry_is_a_failure_and_still_deletes(self):
        self.tenant.on(
            "GET",
            "/bookmarks/bm-smoke",
            self._stored(expiry_time="2026-11-19T17:30:15Z"),
            fakes.json_not_found(),
        )

        with self.assertRaisesRegex(CommandError, "expiry"):
            self._run()

        self.assertEqual(len(self.tenant.calls_to("DELETE", "/bookmarks/bm-smoke")), 1)

    def test_stored_entries_that_differ_are_a_failure(self):
        stored = self._stored()
        stored["device_infos"] = stored["device_infos"][:1]
        self.tenant.on("GET", "/bookmarks/bm-smoke", stored, fakes.json_not_found())

        with self.assertRaisesRegex(CommandError, "device_infos"):
            self._run()

    def test_a_failed_delete_names_the_bookmark_to_remove_by_hand(self):
        self.tenant.on("DELETE", "/bookmarks/bm-smoke", fakes.response(500))

        with self.assertRaisesRegex(CommandError, "bm-smoke"):
            self._run()

    def test_a_bookmark_still_readable_after_delete_is_a_failure(self):
        self.tenant.on("GET", "/bookmarks/bm-smoke", self._stored())

        with self.assertRaisesRegex(CommandError, "still"):
            self._run()

    def test_an_unknown_device_creates_nothing(self):
        with self.assertRaisesRegex(CommandError, "no-such-device"):
            call_command("verify_alta", "--smoke-bookmark", "no-such-device", stdout=StringIO())

        self.assertEqual(self.tenant.calls_to("POST", "/bookmarks"), [])

    def test_a_device_with_no_recording_head_creates_nothing(self):
        self.tenant.on(
            "GET",
            "/devices",
            [fakes.device(DEVICE, "Dark", heads=[fakes.head("1", storing="no_stream_available")])],
        )

        with self.assertRaises(CommandError):
            self._run()

        self.assertEqual(self.tenant.calls_to("POST", "/bookmarks"), [])
