import ast
import base64
import datetime as dt
import inspect
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from googleapiclient.errors import HttpError

from apps.audit.models import AuditLog
from apps.correspondence import services as correspondence_services
from apps.integrations.gmail import client

MAILBOX = "publicrecords@psd401.net"
IMPERSONATION_TARGET = "prr-gmail@example-project.iam.gserviceaccount.com"


def _http_error(status):
    response = mock.Mock(status=status, reason="provider error")
    return HttpError(response, b'{"error": {"message": "provider error"}}')


# TestCase, not SimpleTestCase: the mailbox and the transport timeout are
# application-managed settings now, so building a client reads the store.
class GmailCredentialTests(TestCase):
    @override_settings(
        DEBUG=False,
        PRR_GMAIL_MAILBOX=MAILBOX,
        PRR_GMAIL_IMPERSONATION_TARGET=IMPERSONATION_TARGET,
        PRR_GMAIL_CREDENTIALS_FILE="",
    )
    @mock.patch("apps.integrations.gmail.client.impersonated_credentials.Credentials")
    @mock.patch("apps.integrations.gmail.client.google.auth.default")
    def test_keyless_credentials_impersonate_only_the_configured_mailbox(
        self,
        default_credentials,
        impersonated,
    ):
        source = mock.Mock()
        default_credentials.return_value = (source, "project")
        delegated = mock.Mock()
        impersonated.return_value = delegated

        self.assertIs(client._credentials(), delegated)

        default_credentials.assert_called_once_with(scopes=[client.IAM_SCOPE])
        impersonated.assert_called_once_with(
            source_credentials=source,
            target_principal=IMPERSONATION_TARGET,
            target_scopes=client.SCOPES,
            subject=MAILBOX,
            lifetime=client.IMPERSONATION_LIFETIME_SECONDS,
        )

    @override_settings(
        DEBUG=True,
        PRR_GMAIL_MAILBOX=MAILBOX,
        PRR_GMAIL_IMPERSONATION_TARGET="",
        PRR_GMAIL_CREDENTIALS_FILE="/workspace/test-fixtures/local-gmail-key.json",
    )
    @mock.patch(
        "apps.integrations.gmail.client.service_account.Credentials."
        "from_service_account_file"
    )
    def test_local_key_credentials_delegate_to_only_the_configured_mailbox(
        self,
        load_key,
    ):
        base_credentials = mock.Mock()
        delegated = mock.Mock()
        base_credentials.with_subject.return_value = delegated
        load_key.return_value = base_credentials

        self.assertIs(client._credentials(), delegated)

        load_key.assert_called_once_with(
            "/workspace/test-fixtures/local-gmail-key.json",
            scopes=client.SCOPES,
        )
        base_credentials.with_subject.assert_called_once_with(MAILBOX)

    @override_settings(
        DEBUG=False,
        PRR_GMAIL_MAILBOX=MAILBOX,
        PRR_GMAIL_IMPERSONATION_TARGET="",
        PRR_GMAIL_CREDENTIALS_FILE="/workspace/test-fixtures/forbidden-key.json",
    )
    @mock.patch(
        "apps.integrations.gmail.client.service_account.Credentials."
        "from_service_account_file"
    )
    def test_key_file_is_refused_outside_debug(self, load_key):
        with self.assertRaises(client.GmailUnavailable) as caught:
            client._credentials()

        self.assertEqual(caught.exception.category, "authentication_failed")
        self.assertFalse(caught.exception.retryable)
        load_key.assert_not_called()

    @override_settings(
        DEBUG=False,
        PRR_GMAIL_MAILBOX=MAILBOX,
        PRR_GMAIL_IMPERSONATION_TARGET="",
        PRR_GMAIL_CREDENTIALS_FILE="",
    )
    def test_missing_production_impersonation_target_has_actionable_error(self):
        with self.assertRaises(client.GmailUnavailable) as caught:
            client._credentials()

        self.assertIn("PRR_GMAIL_IMPERSONATION_TARGET", str(caught.exception))
        self.assertIn(
            "PRR_GMAIL_IMPERSONATION_TARGET",
            caught.exception.remediation,
        )

    def test_subject_delegation_is_single_and_pinned_in_source(self):
        tree = ast.parse(inspect.getsource(client))
        with_subject_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_subject"
        ]
        self.assertEqual(len(with_subject_calls), 1)
        self.assertEqual(
            ast.unparse(with_subject_calls[0].args[0]),
            "runtime.get('PRR_GMAIL_MAILBOX')",
        )

        for name in client.EXPORTED_OPERATIONS:
            parameters = inspect.signature(getattr(client, name)).parameters
            self.assertNotIn("subject", parameters, name)
            self.assertNotIn("mailbox", parameters, name)

    @override_settings(PRR_GMAIL_TIMEOUT_SECONDS=17)
    @mock.patch("apps.integrations.gmail.client.build")
    @mock.patch("apps.integrations.gmail.client.google_auth_httplib2.AuthorizedHttp")
    @mock.patch("apps.integrations.gmail.client.httplib2.Http")
    @mock.patch("apps.integrations.gmail.client._credentials")
    def test_service_uses_the_bounded_transport_timeout(
        self,
        credentials,
        http,
        authorized_http,
        build,
    ):
        service = mock.Mock()
        build.return_value = service

        self.assertIs(client._gmail_service(), service)

        http.assert_called_once_with(timeout=17)
        authorized_http.assert_called_once_with(
            credentials.return_value,
            http=http.return_value,
        )
        build.assert_called_once_with(
            "gmail",
            "v1",
            http=authorized_http.return_value,
            cache_discovery=False,
        )


@override_settings(
    PRR_GMAIL_MAILBOX=MAILBOX,
    PRR_GMAIL_TIMEOUT_SECONDS=17,
)
class GmailClientOperationTests(TestCase):
    def setUp(self):
        self.service = mock.Mock()
        self.users = self.service.users.return_value
        self.messages = self.users.messages.return_value
        self.history = self.users.history.return_value
        self.labels = self.users.labels.return_value
        self.service_patch = mock.patch(
            "apps.integrations.gmail.client._gmail_service",
            return_value=self.service,
        )
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    def _request(self, response):
        request = mock.Mock()
        request.execute.return_value = response
        return request

    def test_send_raw_encodes_message_preserves_thread_and_audits(self):
        self.messages.send.return_value = self._request(
            {"id": "gmail-message-1", "threadId": "gmail-thread-1"}
        )

        result = client.send_raw(b"Subject: Test\r\n\r\nBody", thread_id="thread-existing")

        self.assertEqual(
            result,
            client.SentMessage("gmail-message-1", "gmail-thread-1"),
        )
        self.messages.send.assert_called_once_with(
            userId="me",
            body={
                "raw": base64.urlsafe_b64encode(
                    b"Subject: Test\r\n\r\nBody"
                ).decode("ascii"),
                "threadId": "thread-existing",
            },
        )
        audit = AuditLog.objects.get(action="gmail.message.sent")
        self.assertEqual(audit.target, "gmail-message-1")
        self.assertEqual(audit.detail, {"thread_id": "gmail-thread-1"})

    def test_find_by_rfc822_message_id_returns_provider_ids_and_audits(self):
        self.messages.list.return_value = self._request(
            {
                "messages": [
                    {"id": "gmail-message-2", "threadId": "gmail-thread-2"}
                ]
            }
        )

        result = client.find_by_rfc822_message_id("<delivery@prr.psd401.net>")

        self.assertEqual(
            result,
            client.SentMessage("gmail-message-2", "gmail-thread-2"),
        )
        self.messages.list.assert_called_once_with(
            userId="me",
            q="rfc822msgid:<delivery@prr.psd401.net>",
            maxResults=1,
        )
        audit = AuditLog.objects.get(action="gmail.message.looked_up")
        self.assertEqual(audit.target, "gmail-message-2")
        self.assertTrue(audit.detail["found"])
        self.assertNotIn("rfc822_message_id", audit.detail)

    def test_find_by_rfc822_message_id_returns_none_and_audits_miss(self):
        self.messages.list.return_value = self._request({})

        self.assertIsNone(
            client.find_by_rfc822_message_id("<missing@prr.psd401.net>")
        )

        audit = AuditLog.objects.get(action="gmail.message.looked_up")
        self.assertEqual(audit.target, MAILBOX)
        self.assertFalse(audit.detail["found"])

    def test_list_history_aggregates_pages_and_audits(self):
        self.history.list.side_effect = [
            self._request(
                {
                    "history": [{"id": "101"}],
                    "historyId": "105",
                    "nextPageToken": "next-page",
                }
            ),
            self._request(
                {
                    "history": [{"id": "106"}],
                    "historyId": "106",
                }
            ),
        ]

        result = client.list_history("100")

        self.assertEqual(
            result,
            {"history": [{"id": "101"}, {"id": "106"}], "historyId": "106"},
        )
        self.assertEqual(self.history.list.call_count, 2)
        self.history.list.assert_has_calls(
            [
                mock.call(
                    userId="me",
                    startHistoryId="100",
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                ),
                mock.call(
                    userId="me",
                    startHistoryId="100",
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken="next-page",
                ),
            ]
        )
        audit = AuditLog.objects.get(action="gmail.history.listed")
        self.assertEqual(audit.detail, {"history_count": 2, "history_id": "106"})

    def test_list_messages_aggregates_pages_and_audits_without_query_pii(self):
        self.messages.list.side_effect = [
            self._request(
                {
                    "messages": [
                        {"id": "message-a", "threadId": "thread-a"}
                    ],
                    "nextPageToken": "next-page",
                }
            ),
            self._request(
                {"messages": [{"id": "message-b", "threadId": "thread-b"}]}
            ),
        ]

        result = client.list_messages("from:requester@example.org")

        self.assertEqual(
            result,
            [
                client.SentMessage("message-a", "thread-a"),
                client.SentMessage("message-b", "thread-b"),
            ],
        )
        audit = AuditLog.objects.get(action="gmail.messages.listed")
        self.assertEqual(audit.detail, {"message_count": 2})
        self.assertNotIn("requester@example.org", str(audit.detail))

    def test_get_raw_decodes_base64url_and_audits(self):
        raw = b"From: requester@example.org\r\n\r\nBody"
        self.messages.get.return_value = self._request(
            {"id": "message-raw", "raw": base64.urlsafe_b64encode(raw).decode()}
        )

        self.assertEqual(client.get_raw("message-raw"), raw)

        self.messages.get.assert_called_once_with(
            userId="me",
            id="message-raw",
            format="raw",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="gmail.message.read",
                target="message-raw",
            ).exists()
        )

    def test_get_message_returns_authoritative_transport_metadata(self):
        raw = b"From: requester@example.org\r\n\r\nBody"
        self.messages.get.return_value = self._request(
            {
                "id": "message-metadata",
                "threadId": "thread-metadata",
                "historyId": "12345",
                "internalDate": "1784834430000",
                "raw": base64.urlsafe_b64encode(raw).decode(),
            }
        )

        result = client.get_message("message-metadata")

        self.assertEqual(result.message_id, "message-metadata")
        self.assertEqual(result.thread_id, "thread-metadata")
        self.assertEqual(result.history_id, "12345")
        self.assertEqual(
            result.received_at,
            dt.datetime.fromtimestamp(1784834430, tz=dt.UTC),
        )
        self.assertEqual(result.raw_bytes, raw)
        self.messages.get.assert_called_once_with(
            userId="me",
            id="message-metadata",
            format="raw",
        )
        audit = AuditLog.objects.get(
            action="gmail.message.read",
            target="message-metadata",
        )
        self.assertEqual(
            audit.detail,
            {"byte_count": len(raw), "history_id": "12345"},
        )

    def test_get_message_carries_gmail_system_labels(self):
        raw = b"From: publicrecords@psd401.net\r\n\r\nBody"
        self.messages.get.return_value = self._request(
            {
                "id": "message-labels",
                "threadId": "thread-labels",
                "historyId": "12346",
                "internalDate": "1784834430000",
                "labelIds": ["SENT", "INBOX", 7, ""],
                "raw": base64.urlsafe_b64encode(raw).decode(),
            }
        )

        result = client.get_message("message-labels")

        self.assertEqual(result.label_ids, ("SENT", "INBOX"))

    def test_get_message_with_malformed_labels_has_no_labels(self):
        self.messages.get.return_value = self._request(
            {
                "id": "message-no-labels",
                "threadId": "thread-no-labels",
                "historyId": "12347",
                "internalDate": "1784834430000",
                "labelIds": "SENT",
                "raw": base64.urlsafe_b64encode(b"Body").decode(),
            }
        )

        self.assertEqual(client.get_message("message-no-labels").label_ids, ())

    def test_get_message_rejects_missing_authoritative_metadata(self):
        self.messages.get.return_value = self._request(
            {
                "id": "message-incomplete",
                "threadId": "thread-incomplete",
                "raw": base64.urlsafe_b64encode(b"Message").decode(),
            }
        )

        with self.assertRaises(client.GmailUnavailable) as raised:
            client.get_message("message-incomplete")

        self.assertEqual(
            raised.exception.category,
            "permanent_provider_failure",
        )

    @override_settings(PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES=3)
    def test_get_message_rejects_raw_payload_over_inbound_limit(self):
        self.messages.get.return_value = self._request(
            {
                "id": "message-oversized",
                "threadId": "thread-oversized",
                "historyId": "12345",
                "internalDate": "1784834430000",
                "raw": base64.urlsafe_b64encode(b"four").decode(),
            }
        )

        with self.assertRaises(client.GmailUnavailable) as raised:
            client.get_message("message-oversized")

        self.assertEqual(raised.exception.category, "message_rejected")

    def test_current_history_id_reads_profile_without_message_content(self):
        self.users.getProfile.return_value = self._request(
            {"emailAddress": MAILBOX, "historyId": "67890"}
        )

        self.assertEqual(client.current_history_id(), "67890")

        self.users.getProfile.assert_called_once_with(userId="me")
        audit = AuditLog.objects.get(action="gmail.history.current")
        self.assertEqual(audit.target, MAILBOX)
        self.assertEqual(audit.detail, {"history_id": "67890"})

    def test_ensure_label_reuses_existing_label_and_audits(self):
        self.labels.list.return_value = self._request(
            {"labels": [{"id": "Label_1", "name": "PRR/Processed"}]}
        )

        self.assertEqual(client.ensure_label("PRR/Processed"), "Label_1")

        self.labels.create.assert_not_called()
        audit = AuditLog.objects.get(action="gmail.label.ensured")
        self.assertEqual(audit.target, "Label_1")
        self.assertFalse(audit.detail["created"])

    def test_ensure_label_creates_missing_label_and_audits(self):
        self.labels.list.return_value = self._request({"labels": []})
        self.labels.create.return_value = self._request(
            {"id": "Label_2", "name": "PRR/Processed"}
        )

        self.assertEqual(client.ensure_label("PRR/Processed"), "Label_2")

        self.labels.create.assert_called_once_with(
            userId="me",
            body={
                "name": "PRR/Processed",
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        audit = AuditLog.objects.get(action="gmail.label.ensured")
        self.assertTrue(audit.detail["created"])

    @mock.patch("apps.integrations.gmail.client.ensure_label", return_value="Label_3")
    def test_add_label_resolves_name_modifies_message_and_audits(self, ensure_label):
        self.messages.modify.return_value = self._request(
            {"id": "message-label", "labelIds": ["Label_3"]}
        )

        client.add_label("message-label", "PRR/Processed")

        ensure_label.assert_called_once_with("PRR/Processed")
        self.messages.modify.assert_called_once_with(
            userId="me",
            id="message-label",
            body={"addLabelIds": ["Label_3"], "removeLabelIds": []},
        )
        audit = AuditLog.objects.get(action="gmail.label.added")
        self.assertEqual(audit.target, "message-label")
        self.assertEqual(
            audit.detail,
            {"label_id": "Label_3", "archived": False},
        )

    @mock.patch("apps.integrations.gmail.client.ensure_label", return_value="Label_3")
    def test_add_label_archives_from_the_inbox_when_asked(self, ensure_label):
        self.messages.modify.return_value = self._request(
            {"id": "message-label", "labelIds": ["Label_3"]}
        )

        client.add_label("message-label", "PRR/Processed", archive=True)

        self.messages.modify.assert_called_once_with(
            userId="me",
            id="message-label",
            body={"addLabelIds": ["Label_3"], "removeLabelIds": ["INBOX"]},
        )
        audit = AuditLog.objects.get(action="gmail.label.added")
        self.assertEqual(
            audit.detail,
            {"label_id": "Label_3", "archived": True},
        )


@override_settings(PRR_GMAIL_MAILBOX=MAILBOX)
class GmailErrorMappingTests(SimpleTestCase):
    @mock.patch("apps.integrations.gmail.client._gmail_service")
    def test_google_statuses_map_to_existing_delivery_categories(self, service):
        expected = {
            429: ("rate_limited", True),
            500: ("temporary_provider_failure", True),
            503: ("provider_unavailable", True),
            403: ("authentication_failed", False),
            400: ("message_rejected", False),
        }
        for status, (category, retryable) in expected.items():
            with self.subTest(status=status):
                request = mock.Mock()
                request.execute.side_effect = _http_error(status)
                service.return_value.users.return_value.messages.return_value.list.return_value = request

                with self.assertRaises(client.GmailUnavailable) as caught:
                    client.list_messages("label:inbox")

                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.retryable, retryable)
                categories = (
                    correspondence_services.RETRYABLE_DELIVERY_FAILURES
                    if retryable
                    else correspondence_services.PERMANENT_DELIVERY_FAILURES
                )
                self.assertIn(category, categories)

    @mock.patch("apps.integrations.gmail.client._gmail_service")
    def test_transport_exception_is_retryable_provider_unavailable(self, service):
        request = mock.Mock()
        request.execute.side_effect = OSError("network unavailable")
        service.return_value.users.return_value.messages.return_value.list.return_value = request

        with self.assertRaises(client.GmailUnavailable) as caught:
            client.list_messages("label:inbox")

        self.assertEqual(caught.exception.category, "provider_unavailable")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("network unavailable", str(caught.exception))


@override_settings(PRR_GMAIL_MAILBOX=MAILBOX)
class GmailVerificationTests(TestCase):
    @mock.patch("apps.integrations.gmail.client._gmail_service")
    @mock.patch("apps.integrations.gmail.client._credentials")
    def test_verification_reads_profile_and_never_sends(self, credentials, service):
        delegated = mock.Mock()
        delegated.subject = None
        delegated._subject = MAILBOX
        delegated.has_scopes.return_value = True
        credentials.return_value = delegated
        profile_request = mock.Mock()
        profile_request.execute.return_value = {"emailAddress": MAILBOX}
        users = service.return_value.users.return_value
        users.getProfile.return_value = profile_request

        report = client.verify_configuration()

        self.assertEqual(report.subject, MAILBOX)
        self.assertEqual(report.granted_scopes, tuple(client.SCOPES))
        self.assertEqual(report.profile_email, MAILBOX)
        users.getProfile.assert_called_once_with(userId="me")
        users.messages.return_value.send.assert_not_called()

    @mock.patch("apps.integrations.gmail.client._gmail_service")
    @mock.patch("apps.integrations.gmail.client._credentials")
    def test_verification_accepts_impersonated_target_scopes(
        self,
        credentials,
        service,
    ):
        delegated = mock.Mock()
        delegated.subject = MAILBOX
        delegated._target_scopes = tuple(client.SCOPES)
        delegated.has_scopes.return_value = False
        credentials.return_value = delegated
        profile_request = (
            service.return_value.users.return_value.getProfile.return_value
        )
        profile_request.execute.return_value = {"emailAddress": MAILBOX}

        report = client.verify_configuration()

        self.assertEqual(report.granted_scopes, tuple(client.SCOPES))
        delegated.has_scopes.assert_not_called()

    @mock.patch("apps.integrations.gmail.client._gmail_service")
    @mock.patch("apps.integrations.gmail.client._credentials")
    def test_verification_rejects_incomplete_impersonated_target_scopes(
        self,
        credentials,
        service,
    ):
        delegated = mock.Mock()
        delegated.subject = MAILBOX
        delegated._target_scopes = (client.GMAIL_SEND_SCOPE,)
        credentials.return_value = delegated

        with self.assertRaises(client.GmailUnavailable) as caught:
            client.verify_configuration()

        self.assertEqual(caught.exception.category, "authentication_failed")
        self.assertIn("missing a required scope", str(caught.exception))
        service.assert_not_called()

    @mock.patch("apps.integrations.gmail.client._gmail_service")
    @mock.patch("apps.integrations.gmail.client._credentials")
    def test_verification_prefers_public_delegated_subject(self, credentials, service):
        delegated = mock.Mock()
        delegated.subject = MAILBOX
        delegated._subject = "wrong-mailbox@psd401.net"
        delegated.has_scopes.return_value = True
        credentials.return_value = delegated
        profile_request = (
            service.return_value.users.return_value.getProfile.return_value
        )
        profile_request.execute.return_value = {"emailAddress": MAILBOX}

        report = client.verify_configuration()

        self.assertEqual(report.subject, MAILBOX)
