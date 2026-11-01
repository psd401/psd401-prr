# ABOUTME: Defines the normalized, privacy-minimized RFC 3464 DSN contract.
# ABOUTME: Delivery reports retain only the original Message-ID and outcomes.
from dataclasses import FrozenInstanceError, asdict

from django.test import SimpleTestCase

from apps.correspondence import mime


def _limits(**overrides):
    defaults = {
        "max_raw_bytes": 1024 * 1024,
        "max_header_count": 100,
        "max_header_line_bytes": 998,
        "max_parts": 50,
        "max_depth": 10,
        "max_body_bytes": 256 * 1024,
        "max_attachment_count": 10,
        "max_attachment_bytes": 256 * 1024,
        "max_total_attachment_bytes": 512 * 1024,
    }
    defaults.update(overrides)
    return mime.InboundMimeLimits(**defaults)


def _recipient(
    *,
    address="requester@example.org",
    action="failed",
    status="5.1.1",
    diagnostic="smtp; 550 confidential provider prose",
):
    diagnostic_field = (
        f"Diagnostic-Code: {diagnostic}\r\n" if diagnostic is not None else ""
    )
    return (
        f"Final-Recipient: rfc822; {address}\r\n"
        f"Action: {action}\r\n"
        f"Status: {status}\r\n"
        f"{diagnostic_field}\r\n"
    ).encode("ascii")


def _dsn(
    *,
    recipients=None,
    original_content_type="text/rfc822-headers",
    original_headers=b"Message-ID: <application-message@example.org>\r\n",
    report_type="delivery-status",
    delivery_status_parts=1,
):
    recipients = recipients or (_recipient(),)
    machine_parts = b"".join(
        (
            b"--dsn-boundary\r\n"
            b"Content-Type: message/delivery-status\r\n\r\n"
            b"Reporting-MTA: dns; mail.example.org\r\n\r\n"
            + b"".join(recipients)
        )
        for _index in range(delivery_status_parts)
    )
    return (
        b"From: mailer-daemon@example.org\r\n"
        b"To: records@example.org\r\n"
        b"Subject: Delivery Status Notification\r\n"
        b"Message-ID: <dsn-notification@example.org>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type="
        + report_type.encode("ascii")
        + b"; boundary=\"dsn-boundary\"\r\n\r\n"
        b"--dsn-boundary\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"The delivery provider supplied human-readable details.\r\n"
        + machine_parts
        + b"--dsn-boundary\r\n"
        b"Content-Type: "
        + original_content_type.encode("ascii")
        + b"\r\n\r\n"
        + original_headers
        + b"\r\n"
        b"--dsn-boundary--\r\n"
    )


def _parse(raw, **limit_overrides):
    return mime.parse_inbound_message(raw, limits=_limits(**limit_overrides))


class DeliveryStatusReportExtractionTests(SimpleTestCase):
    def assert_malformed(self, raw, **limits):
        with self.assertRaises(mime.InboundMimeSecurityError) as caught:
            _parse(raw, **limits)
        self.assertEqual(caught.exception.code, "malformed_message")
        self.assertNotIn("requester@example.org", str(caught.exception))
        self.assertNotIn("confidential provider prose", str(caught.exception))

    def test_normalizes_ordered_recipient_outcomes_without_pii_or_diagnostics(self):
        parsed = _parse(
            _dsn(
                recipients=(
                    _recipient(
                        address="first-requester@example.org",
                        action="failed",
                        status="5.1.1",
                    ),
                    _recipient(
                        address="second-requester@example.org",
                        action="delayed",
                        status="4.2.0",
                    ),
                    _recipient(
                        address="third-requester@example.org",
                        action="relayed",
                        status="2.1.5",
                    ),
                )
            )
        )

        report = parsed.delivery_status_report
        self.assertIsInstance(report, mime.ParsedDeliveryStatusReport)
        self.assertEqual(
            report.original_message_id,
            "<application-message@example.org>",
        )
        self.assertEqual(
            report.recipients,
            (
                mime.ParsedDeliveryStatusRecipient(1, "failed", "5.1.1"),
                mime.ParsedDeliveryStatusRecipient(2, "delayed", "4.2.0"),
                mime.ParsedDeliveryStatusRecipient(3, "relayed", "2.1.5"),
            ),
        )
        normalized = repr(asdict(report))
        self.assertNotIn("first-requester@example.org", normalized)
        self.assertNotIn("second-requester@example.org", normalized)
        self.assertNotIn("third-requester@example.org", normalized)
        self.assertNotIn("confidential provider prose", normalized)
        self.assertEqual(
            set(asdict(report)),
            {"original_message_id", "recipients"},
        )
        self.assertEqual(
            set(asdict(report)["recipients"][0]),
            {
                "recipient_index",
                "action",
                "status",
                "_recipient_fingerprint",
            },
        )
        self.assertTrue(
            report.recipients[0].matches_recipient_email(
                "FIRST-REQUESTER@example.org"
            )
        )
        self.assertFalse(
            report.recipients[0].matches_recipient_email(
                "unrelated@example.org"
            )
        )
        self.assertEqual(parsed.body, "")
        self.assertEqual(parsed.attachments, ())

        with self.assertRaises(FrozenInstanceError):
            report.original_message_id = "<changed@example.org>"
        with self.assertRaises(FrozenInstanceError):
            report.recipients[0].status = "2.0.0"

    def test_supports_original_message_rfc822_part(self):
        parsed = _parse(
            _dsn(
                original_content_type="message/rfc822",
                original_headers=(
                    b"From: records@example.org\r\n"
                    b"To: requester@example.org\r\n"
                    b"Subject: Original response\r\n"
                    b"Message-ID: <rfc822-application-message@example.org>\r\n"
                    b"\r\n"
                    b"Original body.\r\n"
                ),
            )
        )

        self.assertEqual(
            parsed.delivery_status_report.original_message_id,
            "<rfc822-application-message@example.org>",
        )
        self.assertEqual(parsed.body, "")
        self.assertEqual(parsed.attachments, ())

    def test_supports_encoded_first_colon_in_quoted_printable_original(self):
        raw = _dsn(
            original_content_type="message/rfc822",
            original_headers=(
                b"Message-ID=3A <quoted-original@example.org>\r\n"
                b"From: records@example.org\r\n"
                b"To: requester@example.org\r\n"
                b"Subject: Original response\r\n"
                b"\r\n"
                b"Original body.\r\n"
            ),
        ).replace(
            b"Content-Type: message/rfc822\r\n\r\n",
            (
                b"Content-Type: message/rfc822\r\n"
                b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            ),
        )

        parsed = _parse(raw)

        self.assertEqual(
            parsed.delivery_status_report.original_message_id,
            "<quoted-original@example.org>",
        )
        self.assertEqual(parsed.body, "")
        self.assertEqual(parsed.attachments, ())

    def test_accepts_every_rfc3464_action_with_matching_status_class(self):
        outcomes = (
            ("failed", "5.0.0"),
            ("delayed", "4.7.999"),
            ("delivered", "2.0.0"),
            ("relayed", "2.1.5"),
            ("expanded", "2.1.5"),
        )

        parsed = _parse(
            _dsn(
                recipients=tuple(
                    _recipient(action=action, status=status)
                    for action, status in outcomes
                )
            )
        )

        self.assertEqual(
            tuple(
                (recipient.action, recipient.status)
                for recipient in parsed.delivery_status_report.recipients
            ),
            outcomes,
        )

    def test_does_not_classify_an_ordinary_attached_eml_as_a_dsn(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: Attached message\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"mixed\"\r\n\r\n"
            b"--mixed\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"See attached.\r\n"
            b"--mixed\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"Content-Disposition: attachment; filename=message.eml\r\n\r\n"
            b"From: sender@example.org\r\n"
            b"To: recipient@example.org\r\n"
            b"Message-ID: <ordinary-attachment@example.org>\r\n\r\n"
            b"Ordinary attached message.\r\n"
            b"--mixed--\r\n"
        )

        parsed = _parse(raw)

        self.assertIsNone(parsed.delivery_status_report)
        self.assertEqual(len(parsed.attachments), 1)

    def test_only_top_level_delivery_status_report_is_classified(self):
        nested = _dsn()
        _headers, _separator, nested_body = nested.partition(b"\r\n\r\n")
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: Forwarded report\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"outer\"\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Forwarded delivery report.\r\n"
            b"--outer\r\n"
            b"Content-Type: multipart/report; report-type=delivery-status; "
            b"boundary=\"dsn-boundary\"\r\n\r\n"
            + nested_body
            + b"--outer--\r\n"
        )

        parsed = _parse(raw)

        self.assertIsNone(parsed.delivery_status_report)

    def test_other_or_missing_top_level_report_types_fail_closed(self):
        self.assert_malformed(_dsn(report_type="disposition-notification"))
        self.assert_malformed(
            _dsn().replace(b"report-type=delivery-status; ", b"")
        )

    def test_rejects_missing_or_multiple_embedded_message_ids(self):
        cases = (
            ("missing header", b"Subject: Original response\r\n"),
            (
                "multiple values",
                (
                    b"Message-ID: <first-application@example.org> "
                    b"<second-application@example.org>\r\n"
                ),
            ),
            (
                "multiple fields",
                (
                    b"Message-ID: <first-application@example.org>\r\n"
                    b"Message-ID: <second-application@example.org>\r\n"
                ),
            ),
            (
                "prose around identifier",
                (
                    b"Message-ID: application message "
                    b"<application-message@example.org>\r\n"
                ),
            ),
        )
        for label, original_headers in cases:
            with self.subTest(label=label):
                self.assert_malformed(
                    _dsn(original_headers=original_headers),
                )

    def test_rejects_ambiguous_machine_or_original_report_parts(self):
        self.assert_malformed(_dsn(delivery_status_parts=2))
        duplicate_original = _dsn().replace(
            b"--dsn-boundary--\r\n",
            (
                b"--dsn-boundary\r\n"
                b"Content-Type: text/rfc822-headers\r\n\r\n"
                b"Message-ID: <second-application@example.org>\r\n\r\n"
                b"--dsn-boundary--\r\n"
            ),
        )
        self.assert_malformed(duplicate_original)

    def test_rejects_malformed_recipient_fields_and_action_status_mismatches(self):
        cases = (
            (
                "missing final recipient",
                (
                    b"Action: failed\r\n"
                    b"Status: 5.1.1\r\n\r\n"
                ),
            ),
            (
                "missing action",
                (
                    b"Final-Recipient: rfc822; requester@example.org\r\n"
                    b"Status: 5.1.1\r\n\r\n"
                ),
            ),
            (
                "unsupported action",
                _recipient(action="quarantined", status="5.1.1"),
            ),
            (
                "invalid status",
                _recipient(action="failed", status="five.one.one"),
            ),
            (
                "failed with transient status",
                _recipient(action="failed", status="4.1.1"),
            ),
            (
                "delayed with permanent status",
                _recipient(action="delayed", status="5.1.1"),
            ),
            (
                "delivered with failure status",
                _recipient(action="delivered", status="5.1.1"),
            ),
            (
                "duplicate action",
                (
                    b"Final-Recipient: rfc822; requester@example.org\r\n"
                    b"Action: failed\r\n"
                    b"Action: failed\r\n"
                    b"Status: 5.1.1\r\n\r\n"
                ),
            ),
        )
        for label, recipient in cases:
            with self.subTest(label=label):
                self.assert_malformed(_dsn(recipients=(recipient,)))

    def test_embedded_header_evidence_obeys_existing_bounds(self):
        raw = _dsn(
            original_headers=(
                b"Message-ID: <application-message@example.org>\r\n"
                b"X-Extra-One: one\r\n"
                b"X-Extra-Two: two\r\n"
            )
        )

        baseline = _parse(raw)
        self.assertIsNotNone(baseline.delivery_status_report)

        self.assert_malformed(
            _dsn(
                original_headers=(
                    b"Message-ID: <application-message@example.org>\r\n"
                    b"Malformed header without a delimiter\r\n"
                )
            )
        )
        with self.assertRaises(mime.InboundMimeSecurityError) as caught:
            _parse(raw, max_header_count=16)
        self.assertEqual(caught.exception.code, "too_many_headers")
