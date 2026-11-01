import datetime as dt
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.avigilon import client


class AltaTransportSecurityTests(TestCase):
    @override_settings(
        ALTA_API_BASE="http://alta.example.org",
        ALTA_API_TOKEN="private-bearer-token",
    )
    @mock.patch("apps.integrations.avigilon.client.requests.Session")
    def test_plaintext_base_url_is_rejected_before_session_creation(self, session):
        # The stored setting refuses a plaintext address, so one arriving from
        # the deployed environment resolves to the safe default: not
        # configured. Either way no bearer request is built.
        with self.assertRaises(client.AltaError):
            client.list_cameras()

        session.assert_not_called()

    def test_the_client_refuses_an_unsafe_address_on_its_own(self):
        """Defense in depth below the registry pattern: if a value ever
        reached the client without passing it, the client still refuses."""
        for unsafe in (
            "http://alta.example.org",
            "https://user:pass@alta.example.org",
            "https://alta.example.org/api?token=x",
            "https://alta.example.org/api#fragment",
        ):
            with self.subTest(address=unsafe), mock.patch.object(
                client.runtime, "get", return_value=unsafe
            ):
                with self.assertRaisesRegex(client.AltaError, "HTTPS"):
                    client._validated_base_url()

    @override_settings(
        ALTA_API_BASE="https://alta.example.org",
        ALTA_API_TOKEN="private-bearer-token",
    )
    @mock.patch("apps.integrations.avigilon.client.requests.Session")
    def test_all_bearer_requests_disable_redirects(self, session_factory):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"cameras": []}
        session = session_factory.return_value
        session.get.return_value = response
        session.post.return_value = response
        session.delete.return_value = mock.Mock(status_code=204)
        now = timezone.now()

        client.list_cameras()
        client.create_clip_hold(
            "camera-1",
            now - dt.timedelta(minutes=5),
            now,
            "PRR hold",
        )
        client.delete_clip("clip-1")

        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertFalse(session.delete.call_args.kwargs["allow_redirects"])

    @override_settings(
        ALTA_API_BASE="https://alta.example.org",
        ALTA_API_TOKEN="private-bearer-token",
    )
    @mock.patch("apps.integrations.avigilon.client.requests.Session")
    def test_redirect_response_is_rejected_without_following(self, session_factory):
        response = mock.Mock(status_code=302)
        session_factory.return_value.get.return_value = response

        with self.assertRaisesRegex(client.AltaError, "redirect"):
            client.list_cameras()

        self.assertFalse(
            session_factory.return_value.get.call_args.kwargs[
                "allow_redirects"
            ]
        )
