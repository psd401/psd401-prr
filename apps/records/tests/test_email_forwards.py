# ABOUTME: Forwarded requests open under the original sender and send date.
# ABOUTME: Real Gmail, Outlook, and Apple Mail forward header blocks, nested.
import datetime as dt
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from apps.records.email_forwards import ForwardedOrigin, original_sender

PACIFIC = ZoneInfo("America/Los_Angeles")

# Doe forwarded Roe's forward of the original request (Oct 15 2026).
NESTED_GMAIL = (
    "This came in for us.\n"
    "\n"
    "Best regards,\n"
    "Dana Doe\n"
    "\n"
    "---------- Forwarded message ---------\n"
    "From: Roe, Erin <erin@psd401.net>\n"
    "Date: Thu, Oct 15, 2026 at 12:43\N{NARROW NO-BREAK SPACE}PM\n"
    "Subject: Fwd: Data request\n"
    "To: Doe, Dana <dana@psd401.net>\n"
    "\n"
    "Please advise\n"
    "\n"
    "---------- Forwarded message ---------\n"
    "From: Pat Analyst <pat.analyst@salarysurvey.example>\n"
    "Date: Wed, Oct 14, 2026 at 9:05\N{NARROW NO-BREAK SPACE}AM\n"
    "Subject: Data request\n"
    "To: <erin@psd401.net>\n"
    "\n"
    "Please complete the attached 2026 salary questionnaire.\n"
)


class OriginalSenderTests(SimpleTestCase):
    def _origin(self, subject, body) -> ForwardedOrigin:
        origin = original_sender(subject, body)
        assert origin is not None
        return origin

    def test_nested_gmail_forward_uses_the_deepest_sender_and_date(self):
        origin = self._origin("Fwd: Data request", NESTED_GMAIL)

        self.assertEqual(origin.email, "pat.analyst@salarysurvey.example")
        self.assertEqual(origin.name, "Pat Analyst")
        self.assertEqual(
            origin.sent_at,
            dt.datetime(2026, 10, 14, 9, 5, tzinfo=PACIFIC),
        )

    def test_outlook_forward_with_comma_name_and_sent_line(self):
        body = (
            "FYI\n\n"
            "________________________________\n"
            "From: Doe, Jane <jane.doe@example.org>\n"
            "Sent: Thursday, October 15, 2026 12:43 PM\n"
            "To: Records <publicrecords@psd401.net>\n"
            "Subject: Records request\n\n"
            "I request all bus contracts.\n"
        )

        origin = self._origin("FW: Records request", body)

        self.assertEqual(origin.email, "jane.doe@example.org")
        self.assertEqual(origin.name, "Doe, Jane")
        self.assertEqual(
            origin.sent_at,
            dt.datetime(2026, 10, 15, 12, 43, tzinfo=PACIFIC),
        )

    def test_apple_mail_forward_marker_without_fwd_subject(self):
        body = (
            "Begin forwarded message:\n\n"
            "From: Sam Requester <sam@example.net>\n"
            "Subject: Request\n"
            "Date: October 15, 2026 at 8:01:02 AM PDT\n"
            "To: Kim <kim@psd401.net>\n\n"
            "Please send the minutes.\n"
        )

        origin = self._origin("Request", body)

        self.assertEqual(origin.email, "sam@example.net")
        self.assertEqual(
            origin.sent_at,
            dt.datetime(2026, 10, 15, 8, 1, 2, tzinfo=PACIFIC),
        )

    def test_rfc_date_and_bold_markers(self):
        body = (
            "---------- Forwarded message ---------\n"
            "*From:* *Lee* <lee@example.org>\n"
            "*Date:* Thu, 15 Oct 2026 19:43:00 +0000\n"
            "*Subject:* request\n\n"
            "Body\n"
        )

        origin = self._origin("Fwd: request", body)

        self.assertEqual(origin.email, "lee@example.org")
        self.assertEqual(origin.name, "Lee")
        self.assertEqual(
            origin.sent_at,
            dt.datetime(2026, 10, 15, 19, 43, tzinfo=dt.UTC),
        )

    def test_unparseable_date_keeps_sender_without_a_date(self):
        body = (
            "---------- Forwarded message ---------\n"
            "From: Lee <lee@example.org>\n"
            "Date: sometime last week\n\n"
            "Body\n"
        )

        origin = self._origin("Fwd: request", body)

        self.assertEqual(origin.email, "lee@example.org")
        self.assertIsNone(origin.sent_at)

    def test_ordinary_email_is_not_a_forward(self):
        self.assertIsNone(
            original_sender(
                "Public records request",
                "Please send the board minutes.\nFrom: a@example.org\nDate: x\n",
            )
        )

    def test_forward_without_a_header_block_is_not_attributed(self):
        self.assertIsNone(original_sender("Fwd: request", "Please handle.\n"))
