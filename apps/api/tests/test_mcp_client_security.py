from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from integrations.mcp.client import PRRClient


class PRRClientTransportSecurityTests(SimpleTestCase):
    def test_rejects_remote_plaintext_base_before_creating_authenticated_session(self):
        with patch("integrations.mcp.client.requests.Session") as session:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                PRRClient("http://prr.psd401.net", "read-only-token")

        session.assert_not_called()

    def test_allows_plaintext_only_for_loopback_development(self):
        with patch("integrations.mcp.client.requests.Session"):
            client = PRRClient("http://127.0.0.1:8000", "local-token")

        self.assertEqual(client.base_url, "http://127.0.0.1:8000")

    def test_authenticated_get_never_follows_redirects(self):
        response = Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"results": []}
        session = Mock()
        session.get.return_value = response

        with patch(
            "integrations.mcp.client.requests.Session",
            return_value=session,
        ):
            client = PRRClient("https://prr.psd401.net", "read-only-token")
            client.list_requests()

        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
