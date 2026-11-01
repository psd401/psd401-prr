# ABOUTME: Re-parsing a whole-message quarantine after the parser was corrected.
# ABOUTME: Retained raw evidence is re-read, verified, and released exactly once.
import datetime as dt
from io import BytesIO
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.correspondence import gmail_sync, receiving
from apps.correspondence.mime import InboundMimeSecurityError
from apps.correspondence.models import (
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
)

MAILBOX = "publicrecords@psd401.net"
RECEIVED_AT = dt.datetime(2026, 10, 15, 18, 13, 51, tzinfo=dt.UTC)

# The shape of the Oct 15 2026 forward that the old parser quarantined: a
# Gmail inline signature image whose Content-ID is ``<ii_...>``.
GMAIL_FORWARD = (
    b'From: "Doe, Dana" <dana@psd401.net>\r\n'
    b"To: publicrecords@psd401.net\r\n"
    b"Date: Thu, 15 Oct 2026 18:13:51 -0700\r\n"
    b"Message-ID: <forward-1@mail.gmail.com>\r\n"
    b"Subject: Fwd: Data request\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="mixed"\r\n\r\n'
    b"--mixed\r\n"
    b'Content-Type: multipart/related; boundary="related"\r\n\r\n'
    b"--related\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Please see the forwarded request.\r\n"
    b"--related\r\n"
    b"Content-Type: image/png\r\n"
    b'Content-Disposition: inline; filename="image001.png"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"Content-ID: <ii_mgs7k2ab0>\r\n\r\n"
    b"iVBORw0KGgo=\r\n"
    b"--related--\r\n"
    b"--mixed\r\n"
    b'Content-Type: text/csv; name="questionnaire.csv"\r\n'
    b'Content-Disposition: attachment; filename="questionnaire.csv"\r\n'
    b"Content-Transfer-Encoding: base64\r\n\r\n"
    b"YSxiCjEsMgo=\r\n"
    b"--mixed--\r\n"
)


REPARSE_SETTINGS = override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_GMAIL_MAILBOX=MAILBOX,
    PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES=1024 * 1024,
    PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES=64 * 1024,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS=50,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES=998,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS=25,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH=10,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT=5,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES=256 * 1024,
    PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES=512 * 1024,
)


class ReparseFixture(TestCase):
    def setUp(self):
        self.actor = gmail_sync.system_inbound_principal()
        self.stored = {}

    def _store(self, storage_key, source, **_kwargs):
        position = source.tell()
        source.seek(0)
        self.stored[storage_key] = source.read()
        source.seek(position)

    def _open(self, storage_key):
        return BytesIO(self.stored[storage_key])

    def _quarantined_receipt(self, raw=GMAIL_FORWARD, message_id="m-1"):
        """Receive ``raw`` as the old, over-strict parser did: quarantined."""
        with (
            patch(
                "apps.secure_files.store_quarantined_object",
                side_effect=self._store,
            ),
            patch(
                "apps.correspondence.receiving.parse_inbound_message",
                side_effect=InboundMimeSecurityError("malformed_message"),
            ),
        ):
            result = receiving.receive_inbound_message(
                raw_message=raw,
                provider="gmail",
                provider_account=MAILBOX,
                provider_message_id=message_id,
                provider_thread_id=f"thread-{message_id}",
                envelope_sender="",
                envelope_recipients=[MAILBOX],
                received_at=RECEIVED_AT,
                idempotency_key=f"gmail:poll:{message_id}",
                actor=self.actor,
            )
        receipt = result.receipt
        self.assertEqual(receipt.parsing_status, "quarantined")
        self.assertIsNone(receipt.matched_correspondence)
        return receipt

    def _reparse(self, receipt):
        with (
            patch(
                "apps.secure_files.store_quarantined_object",
                side_effect=self._store,
            ),
            patch(
                "apps.secure_files.open_private_object",
                side_effect=self._open,
            ),
            patch(
                "apps.correspondence.receiving._enqueue_attachment_scan"
            ) as scan,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = receiving.reparse_quarantined_receipt(
                receipt.pk,
                actor=self.actor,
            )
        return result, scan


@REPARSE_SETTINGS
class InboundReparseTests(ReparseFixture):
    def test_reparse_releases_a_whole_message_quarantine(self):
        receipt = self._quarantined_receipt()

        result, scan = self._reparse(receipt)

        self.assertEqual(result.outcome, "released")
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "parsed")
        self.assertFalse(receipt.quarantined)
        self.assertEqual(receipt.quarantine_reason, "")
        self.assertEqual(receipt.matching_status, "unmatched")
        correspondence = receipt.matched_correspondence
        self.assertEqual(correspondence.received_subject, "Fwd: Data request")
        self.assertIn("forwarded request", correspondence.received_body)
        self.assertEqual(correspondence.received_at, RECEIVED_AT)
        filenames = sorted(
            CorrespondenceAttachment.objects.filter(
                inbound_receipt=receipt
            ).values_list("original_filename", flat=True)
        )
        self.assertEqual(filenames, ["image001.png", "questionnaire.csv"])
        self.assertEqual(scan.call_count, 2)
        event = AuditLog.objects.get(action="correspondence.inbound_reparsed")
        self.assertEqual(event.target, str(receipt.pk))
        self.assertEqual(event.detail["previous_reason"], "malformed_message")

    def test_reparse_is_idempotent_once_released(self):
        receipt = self._quarantined_receipt()
        self._reparse(receipt)

        result, _scan = self._reparse(receipt)

        self.assertEqual(result.outcome, "not_quarantined")
        self.assertEqual(
            AuditLog.objects.filter(
                action="correspondence.inbound_reparsed"
            ).count(),
            1,
        )

    def test_message_the_corrected_parser_still_rejects_stays_quarantined(self):
        receipt = self._quarantined_receipt(
            raw=(
                b"From: requester@example.org\r\n"
                b"To: publicrecords@psd401.net\r\n"
                b"Content-Type: multipart/mixed; boundary=expected\r\n\r\n"
                b"--different\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            ),
        )

        result, _scan = self._reparse(receipt)

        self.assertEqual(result.outcome, "still_quarantined")
        self.assertEqual(result.reason, "malformed_message")
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "quarantined")
        self.assertIsNone(receipt.matched_correspondence)

    def test_tampered_raw_evidence_is_refused(self):
        receipt = self._quarantined_receipt()
        self.stored[receipt.raw_storage_key] = GMAIL_FORWARD + b"tampered"

        result, _scan = self._reparse(receipt)

        self.assertEqual(result.outcome, "evidence_mismatch")
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "quarantined")

    def test_parsed_receipts_are_never_reparsed(self):
        with patch(
            "apps.secure_files.store_quarantined_object",
            side_effect=self._store,
        ):
            parsed = receiving.receive_inbound_message(
                raw_message=GMAIL_FORWARD,
                provider="gmail",
                provider_account=MAILBOX,
                provider_message_id="m-parsed",
                provider_thread_id="thread-parsed",
                envelope_sender="",
                envelope_recipients=[MAILBOX],
                received_at=RECEIVED_AT,
                idempotency_key="gmail:poll:m-parsed",
                actor=self.actor,
            ).receipt

        result, _scan = self._reparse(parsed)

        self.assertEqual(result.outcome, "not_quarantined")
        self.assertEqual(
            InboundCorrespondenceReceipt.objects.get(pk=parsed.pk).version,
            parsed.version,
        )


@REPARSE_SETTINGS
class ReparseCommandTests(ReparseFixture):
    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with (
            patch(
                "apps.secure_files.store_quarantined_object",
                side_effect=self._store,
            ),
            patch(
                "apps.secure_files.open_private_object",
                side_effect=self._open,
            ),
            patch("apps.correspondence.receiving._enqueue_attachment_scan"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            call_command("reparse_quarantined_inbound", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_writing(self):
        receipt = self._quarantined_receipt()

        output = self._run()

        self.assertIn(f"WOULD RELEASE | 2026-10-15 18:13 | {receipt.pk}", output)
        self.assertIn("Fwd: Data request", output)
        self.assertIn("DRY RUN: 1", output)
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "quarantined")

    def test_dry_run_names_the_original_sender_of_a_forward(self):
        raw = GMAIL_FORWARD.replace(
            b"Please see the forwarded request.\r\n",
            (
                b"---------- Forwarded message ---------\r\n"
                b"From: Pat Analyst <pat.analyst@salarysurvey.example>\r\n"
                b"Date: Wed, Oct 14, 2026 at 9:05 AM\r\n\r\n"
                b"Please complete the questionnaire.\r\n"
            ),
        )
        self._quarantined_receipt(raw=raw)

        output = self._run()

        self.assertIn(
            "WOULD RELEASE (forward of Pat Analyst "
            "<pat.analyst@salarysurvey.example>, sent 2026-10-14 09:05 PDT)",
            output,
        )

    def test_commit_requires_an_explicit_selection(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._run("--commit")

    def test_commit_releases_and_runs_email_intake(self):
        receipt = self._quarantined_receipt()

        output = self._run("--commit", "--all")

        self.assertIn(f"released | {receipt.pk}", output)
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "parsed")
        self.assertEqual(receipt.matching_status, "matched")
        self.assertEqual(receipt.match_method, "email_intake")
        self.assertIn(receipt.matched_request.number, output)
