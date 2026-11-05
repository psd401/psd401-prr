"""Devices and device groups: what a hold is placed on.

The tenant's device list is a bare list from a plain ``GET /devices``;
offset paging drops devices, so it is never used. Heads are read from each
device's own ``source_ids``/``sources``, never inferred from its model.
"""

import datetime as dt
import json

import requests

from apps.integrations.avigilon import client
from apps.integrations.avigilon.tests import fakes

UTC = dt.UTC


class DeviceListTests(fakes.TenantTestCase):
    def _devices(self, *devices):
        self.tenant.on("GET", "/devices", list(devices))
        return client.list_device_cameras()

    def test_a_device_maps_to_a_camera(self):
        (camera,) = self._devices(
            fakes.device(
                "guid-1",
                "Gym north",
                group=fakes.GROUP_B,
                status="COMMUNICATION_ERROR",
                pruned_to="2026-10-26T20:37:42.086Z",
                heads=[fakes.head("1")],
            )
        )

        self.assertEqual(
            camera,
            client.Camera(
                id="guid-1",
                name="Gym north",
                site="SITE-B",
                device_group_id=fakes.GROUP_B,
                server_group_id=fakes.SERVER_GROUP,
                status="COMMUNICATION_ERROR",
                pruned_to=dt.datetime(2026, 10, 26, 20, 37, 42, 86000, tzinfo=UTC),
                sources=("1",),
            ),
        )

    def test_the_list_is_one_plain_get_without_paging(self):
        self._devices(fakes.device("guid-1", "A"))

        (call,) = self.tenant.calls_to("GET", "/devices")
        self.assertIsNone(call["kwargs"].get("params"))

    def test_an_inactive_device_is_skipped(self):
        cameras = self._devices(
            fakes.device("on", "On"), fakes.device("off", "Off", active=False)
        )

        self.assertEqual([c.id for c in cameras], ["on"])

    def test_a_multisensor_device_lists_every_head_it_reports(self):
        (camera,) = self._devices(
            fakes.device(
                "multi",
                "Quad",
                heads=[fakes.head("1"), fakes.head("2"), fakes.head("3"), fakes.head("4")],
            )
        )

        self.assertEqual(camera.sources, ("1", "2", "3", "4"))

    def test_head_numbering_comes_from_the_device_not_the_model(self):
        """The same model numbers 0-3 on one unit and 1-4 on another."""
        cameras = self._devices(
            fakes.device("a", "A", heads=[fakes.head(s) for s in "0123"]),
            fakes.device("b", "B", heads=[fakes.head(s) for s in "1234"]),
        )

        self.assertEqual([c.sources for c in cameras], [("0", "1", "2", "3"), ("1", "2", "3", "4")])

    def test_inactive_and_streamless_heads_are_skipped(self):
        (camera,) = self._devices(
            fakes.device(
                "multi",
                "Mixed",
                heads=[
                    fakes.head("0", inactive=True, storing="setting_up"),
                    fakes.head("1"),
                    fakes.head("2", storing="no_stream_available"),
                    fakes.head("3", storing="explicit"),
                    fakes.head("4", storing=None),
                ],
            )
        )

        self.assertEqual(camera.sources, ("1", "3", "4"))

    def test_streamless_heads_are_reported_as_skipped_and_inactive_heads_are_not(self):
        """No stream now does not mean no footage in the requested window, so
        a streamless head is surfaced for a hand check; an inactive head is
        retired config and stays out entirely."""
        (camera,) = self._devices(
            fakes.device(
                "multi",
                "Mixed",
                heads=[
                    fakes.head("0", inactive=True, storing="no_stream_available"),
                    fakes.head("1"),
                    fakes.head("2", storing="no_stream_available"),
                    fakes.head("3", storing="no_stream_available"),
                ],
            )
        )

        self.assertEqual(camera.sources, ("1",))
        self.assertEqual(camera.skipped_sources, ("2", "3"))

    def test_an_axis_quad_composite_is_neither_bookmarked_nor_skipped(self):
        """The quad source is a composite view of the other heads."""
        (camera,) = self._devices(
            fakes.device(
                "axis",
                "Axis multisensor",
                heads=[
                    fakes.head("1"),
                    fakes.head("2"),
                    fakes.head("5", source_name="quad"),
                    fakes.head("6", source_name="quad", storing="no_stream_available"),
                ],
            )
        )

        self.assertEqual(camera.sources, ("1", "2"))
        self.assertEqual(camera.skipped_sources, ())

    def test_a_camera_built_without_skipped_sources_has_none(self):
        camera = client.Camera(
            id="g", name="n", site="", device_group_id="", server_group_id="s",
            status="", pruned_to=None, sources=("1",),
        )

        self.assertEqual(camera.skipped_sources, ())

    def test_a_device_with_no_recording_head_is_listed_with_no_sources(self):
        (camera,) = self._devices(
            fakes.device("dark", "Dark", heads=[fakes.head("1", storing="no_stream_available")])
        )

        self.assertEqual(camera.sources, ())
        self.assertEqual(camera.skipped_sources, ("1",))

    def test_sources_fall_back_to_the_sources_map(self):
        record = fakes.device("x", "X", heads=[fakes.head("2"), fakes.head("5")])
        del record["source_ids"]

        (camera,) = self._devices(record)

        self.assertEqual(camera.sources, ("2", "5"))

    def test_a_device_in_an_unknown_group_has_no_site(self):
        (camera,) = self._devices(fakes.device("x", "X", group="no-such-group"))

        self.assertEqual(camera.site, "")
        self.assertEqual(camera.device_group_id, "no-such-group")

    def test_pruned_to_is_utc_or_none(self):
        cameras = self._devices(
            fakes.device("z", "Z", pruned_to="2026-10-26T20:37:42Z"),
            fakes.device("off", "Offset", pruned_to="2026-10-26T13:37:42-07:00"),
            fakes.device("null", "Null", pruned_to=None),
            fakes.device("junk", "Junk", pruned_to="yesterday"),
        )

        expected = dt.datetime(2026, 10, 26, 20, 37, 42, tzinfo=UTC)
        self.assertEqual([c.pruned_to for c in cameras], [expected, expected, None, None])
        offset_parsed = cameras[1].pruned_to
        assert offset_parsed is not None
        self.assertEqual(offset_parsed.utcoffset(), dt.timedelta(0))

    def test_a_true_empty_list_is_no_cameras(self):
        self.assertEqual(self._devices(), [])

    def test_a_payload_that_is_not_a_list_raises(self):
        for payload in ({"data": []}, {"devices": []}, None, "devices"):
            with self.subTest(payload=payload):
                body = json.dumps(payload).encode()
                self.tenant.on(
                    "GET", "/devices", fakes.response(200, body, content_type="application/json")
                )
                with self.assertRaises(client.AltaError):
                    client.list_device_cameras()

    def test_a_record_that_is_not_an_object_raises(self):
        self.tenant.on("GET", "/devices", ["guid-1"])

        with self.assertRaises(client.AltaError):
            client.list_device_cameras()

    def test_a_device_without_a_guid_raises_rather_than_vanishing(self):
        record = fakes.device("x", "X")
        del record["guid"]
        self.tenant.on("GET", "/devices", [record])

        with self.assertRaisesRegex(client.AltaError, "guid"):
            client.list_device_cameras()

    def test_a_text_404_is_a_bad_path_not_an_empty_list(self):
        self.tenant.on("GET", "/devices", fakes.text_not_found())

        with self.assertRaisesRegex(client.AltaError, "404"):
            client.list_device_cameras()

    def test_a_group_list_failure_fails_the_device_list(self):
        self.tenant.on("GET", "/deviceGroups", fakes.text_not_found())

        with self.assertRaises(client.AltaError):
            client.list_device_cameras()

    def test_transport_errors_raise_alta_errors(self):
        for error in (requests.ConnectionError("reset"), requests.Timeout("slow")):
            with self.subTest(error=type(error).__name__):
                self.tenant.on("GET", "/devices", error)
                with self.assertRaisesRegex(client.AltaError, type(error).__name__):
                    client.list_device_cameras()

    def test_a_server_error_is_retried_with_backoff(self):
        self.tenant.on(
            "GET",
            "/devices",
            fakes.response(503),
            fakes.response(429),
            [fakes.device("guid-1", "A")],
        )

        cameras = client.list_device_cameras()

        self.assertEqual([c.id for c in cameras], ["guid-1"])
        self.assertEqual(len(self.tenant.calls_to("GET", "/devices")), 3)
        self.assertEqual(
            [c.args[0] for c in self.sleep.call_args_list],
            list(client.READ_RETRY_DELAYS[:2]),
        )

    def test_retries_are_bounded(self):
        self.tenant.on("GET", "/devices", fakes.response(500))

        with self.assertRaisesRegex(client.AltaError, "500"):
            client.list_device_cameras()

        self.assertEqual(
            len(self.tenant.calls_to("GET", "/devices")), len(client.READ_RETRY_DELAYS) + 1
        )

    def test_a_client_error_is_not_retried(self):
        self.tenant.on(
            "GET", "/devices", fakes.response(400, {"kind": "BAD_REQUEST", "message": "nope"})
        )

        with self.assertRaisesRegex(client.AltaError, "BAD_REQUEST"):
            client.list_device_cameras()

        self.assertEqual(len(self.tenant.calls_to("GET", "/devices")), 1)


class DeviceGroupTests(fakes.TenantTestCase):
    def test_groups_map_to_device_groups(self):
        self.assertEqual(
            client.list_device_groups(),
            [
                client.DeviceGroup(fakes.GROUP_A, "SITE-A"),
                client.DeviceGroup(fakes.GROUP_B, "SITE-B"),
                client.DeviceGroup(fakes.GROUP_EMPTY, "SITE-EMPTY"),
            ],
        )

    def test_a_group_payload_that_is_not_a_list_raises(self):
        self.tenant.on("GET", "/deviceGroups", {"groups": []})

        with self.assertRaises(client.AltaError):
            client.list_device_groups()

    def test_a_group_without_an_id_raises(self):
        self.tenant.on("GET", "/deviceGroups", [{"name": "SITE-A"}])

        with self.assertRaises(client.AltaError):
            client.list_device_groups()


class LegacyCameraListTests(fakes.TenantTestCase):
    def test_list_cameras_keeps_its_dict_shape(self):
        self.tenant.on("GET", "/devices", [fakes.device("guid-1", "Gym", group=fakes.GROUP_A)])

        self.assertEqual(
            client.list_cameras(), [{"id": "guid-1", "name": "Gym", "site": "SITE-A"}]
        )
