"""Review throughput: email metadata, Message-ID dedup, tags, bulk review.

The three outcomes this phase promises:

* one message found in N mailboxes appears once in review, carrying N
  custodians;
* every out-of-office reply on a case can be marked in one pass;
* counsel-domain items are separable before anything ships.
"""

import hashlib
import io
from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records import record_tags, vault_ingest
from apps.records.email_metadata import parse_email_metadata
from apps.records.management.commands.fold_duplicate_record_copies import (
    Command as FoldCommand,
)
from apps.records.models import (
    CollectionTask,
    CounselDomain,
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    RecordItemCustodian,
    RecordItemWithholdingExemption,
    Redaction,
    Requester,
    VaultMessageArtifact,
)
from apps.records.review import (
    BULK_REVIEW_MAX_ITEMS,
    bulk_review_records,
)
from apps.records.vault_ingest import (
    ConvertedVaultRecord,
    ingest_completed_export,
)


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        source = kwargs["Body"]
        position = source.tell()
        payload = source.read()
        source.seek(position)
        if (
            kwargs["Key"] in self.objects
            and not kwargs["Key"].startswith("quarantine/")
        ):
            raise AssertionError("immutable object was overwritten")
        self.objects[kwargs["Key"]] = payload

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}


def build_message(
    *,
    subject="Board packet",
    body="First body",
    message_id="<one@psd401.net>",
    sender="author@psd401.net",
    to=("recipient@psd401.net",),
    cc=(),
    date="Mon, 03 Aug 2026 09:14:00 -0700",
    headers=(),
    calendar=False,
    attachment=None,
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = date
    for name, value in headers:
        message[name] = value
    if attachment is not None:
        # An attachment-only message: no text part at all, which is what a
        # bare forwarded document or a calendar-only invitation looks like.
        message.set_content(
            attachment,
            maintype="application",
            subtype="octet-stream",
            filename="scan.bin",
        )
    else:
        message.set_content(body)
    if calendar:
        message.add_attachment(
            b"BEGIN:VCALENDAR\nEND:VCALENDAR\n",
            maintype="text",
            subtype="calendar",
            filename="invite.ics",
        )
    return message.as_bytes()


def _fixed_identity(metadata):
    """Force two differing messages to share one deduplication key.

    Stands in for a legacy pair that really is the same message stored twice,
    without needing byte-identical fixtures the ingest stub would itself
    deduplicate before the backfill ever ran.
    """
    metadata.body_sha256 = "0" * 64
    return metadata


def mbox(*messages):
    output = bytearray()
    for message in messages:
        output.extend(b"From author@psd401.net Sat Aug  1 12:00:00 2026\n")
        output.extend(message.replace(b"\r\n", b"\n"))
        output.extend(b"\n\n")
    return bytes(output)


class _VaultIngestFixture:
    """Ingest stubbed Vault exports for a test case that sets ``self.prr`` and
    ``self.client_stub``. Shared by the dedup and fold tests, which build the
    same shape of history."""

    def _task(self, custodians, export_id):
        return CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.INGESTING,
            instructions="Retain and ingest the completed export.",
            custodian_emails=custodians,
            vault_matter_id="matter-1",
            vault_export_id=export_id,
        )

    def _ingest(self, task, source, export_id):
        export = {
            "id": export_id,
            "status": "COMPLETED",
            "cloudStorageSink": {
                "files": [{
                    "bucketName": "vault-bucket",
                    "objectName": f"mail/{export_id}.mbox",
                    "size": str(len(source)),
                }],
            },
        }

        def download(bucket, object_name, destination):
            destination.write(source)

        def convert(raw_message):
            digest = hashlib.sha256(raw_message).hexdigest()
            return ConvertedVaultRecord(
                content=b"%PDF-1.7\n" + digest.encode(),
                filename="Board packet.pdf",
                content_type="application/pdf",
                ocr_text="Board packet",
            )

        def scan(storage_key, *, expected_size, expected_sha256, client):
            return True, "OK"

        def cleanup(storage_key, *, client):
            client.objects.pop(storage_key, None)

        return ingest_completed_export(
            task.pk,
            export=export,
            _download=download,
            _client=self.client_stub,
            _convert=convert,
            _scan=scan,
            _cleanup_quarantine=cleanup,
        )


class EmailMetadataParsingTests(TestCase):
    def test_every_reviewer_relevant_header_is_read(self):
        raw = build_message(
            subject="Automatic reply: budget",
            sender="Ann Author <ann@vendor.example>",
            to=("one@psd401.net", "two@psd401.net"),
            cc=("counsel@lawfirm.example",),
            message_id="<abc@vendor.example>",
            headers=(
                ("In-Reply-To", "<prior@psd401.net>"),
                ("Auto-Submitted", "auto-replied"),
                ("List-Id", "staff-news <news.psd401.net>"),
                ("Precedence", "bulk"),
            ),
            calendar=True,
        )

        metadata = parse_email_metadata(raw)

        self.assertEqual(metadata.message_id, "<abc@vendor.example>")
        self.assertEqual(metadata.in_reply_to, "<prior@psd401.net>")
        self.assertEqual(metadata.from_address, "ann@vendor.example")
        self.assertEqual(metadata.sender_domain, "vendor.example")
        self.assertEqual(
            metadata.to_addresses, "one@psd401.net, two@psd401.net"
        )
        self.assertEqual(metadata.cc_addresses, "counsel@lawfirm.example")
        self.assertEqual(metadata.auto_submitted, "auto-replied")
        self.assertEqual(metadata.precedence, "bulk")
        self.assertIn("news.psd401.net", metadata.list_id)
        self.assertTrue(metadata.has_calendar_part)
        self.assertIsNotNone(metadata.sent_at)
        self.assertEqual(metadata.sent_at.year, 2026)

    def test_the_same_words_hash_the_same_whatever_the_mailbox(self):
        # The per-recipient copies of one message differ in their To header
        # and their Received trail. What they say is identical, and that is
        # what the digest is taken over.
        first = parse_email_metadata(
            build_message(body="Agenda attached.", to=("alice@psd401.net",))
        )
        second = parse_email_metadata(
            build_message(body="Agenda attached.", to=("bob@psd401.net",))
        )
        different = parse_email_metadata(build_message(body="Something else."))

        self.assertEqual(first.body_sha256, second.body_sha256)
        self.assertNotEqual(first.body_sha256, different.body_sha256)
        self.assertEqual(len(first.body_sha256), 64)

    def test_a_message_with_no_text_has_no_fingerprint_to_offer(self):
        # The digest of nothing is the same for every message that has
        # nothing, so an attachment-only message must not claim one. An empty
        # digest says "I cannot corroborate my own Message-ID", and
        # deduplication has to fall back to the byte hashes.
        metadata = parse_email_metadata(
            build_message(attachment=b"scanned pages")
        )

        self.assertEqual(metadata.body_sha256, "")

    def test_an_invented_charset_is_read_anyway_rather_than_raising(self):
        # errors="replace" only rescues a codec Python knows. A garbled or
        # invented charset label raises LookupError instead, and uncaught that
        # fails the whole export object -- which is retried whole, hits the
        # same message, and never completes. A message that lies about its
        # encoding is a message to read as best we can, not an outage.
        message = EmailMessage()
        message["Message-ID"] = "<odd-charset@psd401.net>"
        message["From"] = "ann@vendor.example"
        message.set_content("Agenda attached.")
        raw = message.as_bytes().replace(b"utf-8", b"x-not-a-charset")

        metadata = parse_email_metadata(raw)

        self.assertEqual(metadata.message_id, "<odd-charset@psd401.net>")
        self.assertEqual(len(metadata.body_sha256), 64)

    def test_unusable_date_is_left_empty_rather_than_invented(self):
        raw = build_message(date="not a date at all")

        self.assertIsNone(parse_email_metadata(raw).sent_at)

    def test_missing_headers_produce_empty_values_not_errors(self):
        message = EmailMessage()
        message.set_content("body only")

        metadata = parse_email_metadata(message.as_bytes())

        self.assertEqual(metadata.message_id, "")
        self.assertEqual(metadata.sender_domain, "")
        self.assertFalse(metadata.has_calendar_part)

    def test_list_unsubscribe_answers_when_list_id_is_absent(self):
        raw = build_message(
            headers=(("List-Unsubscribe", "<mailto:stop@news.example>"),),
        )

        self.assertIn("stop@news.example", parse_email_metadata(raw).list_id)


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_VAULT_EXPORT_MAX_BYTES=10 * 1024 * 1024,
    PRR_VAULT_EXPORT_TOTAL_MAX_BYTES=20 * 1024 * 1024,
    PRR_VAULT_ARCHIVE_MAX_MEMBERS=100,
    PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES=20 * 1024 * 1024,
)
class MessageIdDeduplicationTests(_VaultIngestFixture, TestCase):
    """One message in N mailboxes is one record carrying N custodians."""

    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.client_stub = _ObjectClient()

    def test_one_message_in_two_mailboxes_is_one_record_with_two_custodians(self):
        # The per-recipient copies differ only in their To header, so the
        # rendered bytes differ and only Message-ID can see they are one
        # public record.
        first = build_message(
            message_id="<shared@psd401.net>",
            to=("alice@psd401.net",),
        )
        second = build_message(
            message_id="<shared@psd401.net>",
            to=("bob@psd401.net",),
        )

        self._ingest(
            self._task(["alice@psd401.net"], "export-alice"),
            mbox(first),
            "export-alice",
        )
        self._ingest(
            self._task(["bob@psd401.net"], "export-bob"),
            mbox(second),
            "export-bob",
        )

        items = list(RecordItem.objects.filter(request=self.prr))
        self.assertEqual(len(items), 1)
        self.assertEqual(
            sorted(row.custodian for row in items[0].custodians.all()),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        self.assertEqual(
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.DUPLICATE,
            ).count(),
            1,
        )

    def test_a_reused_message_id_with_a_different_body_stays_its_own_record(self):
        # Message-ID is a sender-supplied header, not an identity. Folding a
        # genuinely different message into an existing record on the strength
        # of a reused one is how a responsive record goes missing from a
        # production, so the body digest has to disagree.
        self._ingest(
            self._task(["alice@psd401.net"], "export-first"),
            mbox(build_message(
                message_id="<reused@psd401.net>", body="The real agenda.",
            )),
            "export-first",
        )
        self._ingest(
            self._task(["bob@psd401.net"], "export-second"),
            mbox(build_message(
                message_id="<reused@psd401.net>", body="A different message.",
            )),
            "export-second",
        )

        self.assertEqual(RecordItem.objects.filter(request=self.prr).count(), 2)
        self.assertFalse(
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.DUPLICATE,
            ).exists()
        )

    def test_two_textless_messages_sharing_a_message_id_stay_separate(self):
        # Two different scans, each with no text part, reusing one Message-ID.
        # Both fingerprint to nothing, so Message-ID is the only thing left --
        # and it is exactly the thing that is not trusted alone. Each has to
        # reach review as its own record.
        self._ingest(
            self._task(["alice@psd401.net"], "export-scan-one"),
            mbox(build_message(
                message_id="<scan@psd401.net>", attachment=b"first scan",
            )),
            "export-scan-one",
        )
        self._ingest(
            self._task(["bob@psd401.net"], "export-scan-two"),
            mbox(build_message(
                message_id="<scan@psd401.net>", attachment=b"second scan",
            )),
            "export-scan-two",
        )

        self.assertEqual(RecordItem.objects.filter(request=self.prr).count(), 2)
        self.assertFalse(
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.DUPLICATE,
            ).exists()
        )

    def test_a_lost_create_race_keeps_its_custodian_instead_of_raising(self):
        # Two custodian mailboxes of one request ingest concurrently and both
        # see no duplicate before either commits. The unique constraint
        # decides it; the loser must still record the mailbox it found the
        # message in.
        self._ingest(
            self._task(["alice@psd401.net"], "export-race-first"),
            mbox(build_message(message_id="<race@psd401.net>")),
            "export-race-first",
        )
        winner = RecordItem.objects.get(request=self.prr)

        real_duplicate = vault_ingest._message_duplicate
        calls = []

        def blind_first_check(request_id, metadata):
            calls.append(request_id)
            if len(calls) == 1:
                # Stands in for the concurrent run that had not committed yet.
                return None
            return real_duplicate(request_id, metadata)

        with patch.object(
            vault_ingest, "_message_duplicate", blind_first_check,
        ):
            self._ingest(
                self._task(["bob@psd401.net"], "export-race-second"),
                mbox(build_message(message_id="<race@psd401.net>")),
                "export-race-second",
            )

        self.assertEqual(RecordItem.objects.filter(request=self.prr).count(), 1)
        self.assertEqual(
            sorted(row.custodian for row in winner.custodians.all()),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        self.assertEqual(
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.DUPLICATE,
            ).count(),
            1,
        )

    def test_ingest_stores_the_email_metadata_on_the_record(self):
        raw = build_message(
            sender="ann@vendor.example",
            message_id="<meta@vendor.example>",
            headers=(("Auto-Submitted", "auto-replied"),),
        )

        self._ingest(
            self._task(["alice@psd401.net"], "export-meta"),
            mbox(raw),
            "export-meta",
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(item.email_message_id, "<meta@vendor.example>")
        self.assertEqual(item.email_sender_domain, "vendor.example")
        self.assertEqual(item.email_auto_submitted, "auto-replied")
        self.assertIsNotNone(item.email_sent_at)

    def test_multiple_custodians_on_one_task_never_join_into_one_string(self):
        self._ingest(
            self._task(
                ["alice@psd401.net", "bob@psd401.net"],
                "export-both",
            ),
            mbox(build_message(message_id="<both@psd401.net>")),
            "export-both",
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertNotIn(",", item.custodian)
        self.assertEqual(item.custodian, "alice@psd401.net")
        self.assertEqual(item.custodians.count(), 2)

    def test_backfill_command_fills_metadata_from_the_retained_message(self):
        self._ingest(
            self._task(["alice@psd401.net"], "export-backfill"),
            mbox(build_message(message_id="<backfill@psd401.net>")),
            "export-backfill",
        )
        RecordItem.objects.update(
            email_message_id="",
            email_sender_domain="",
            email_from="",
        )

        with patch(
            "apps.records.storage._object_store_client",
            return_value=self.client_stub,
        ):
            call_command(
                "backfill_record_email_metadata",
                "--commit",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(item.email_message_id, "<backfill@psd401.net>")
        self.assertEqual(item.email_sender_domain, "psd401.net")
        self.assertTrue(
            AuditLog.objects.filter(
                action="record.email_metadata.backfilled",
            ).exists()
        )

    def test_backfill_reports_a_pre_existing_collision_instead_of_crashing(self):
        # Records ingested before deduplication existed can already hold two
        # copies of one message. The backfill describes what it found and
        # leaves the second record for a human; it must not abort the pass and
        # strand every later record without metadata.
        self._ingest(
            self._task(["alice@psd401.net"], "export-collide-one"),
            mbox(build_message(message_id="<collide@psd401.net>", body="Same")),
            "export-collide-one",
        )
        self._ingest(
            self._task(["bob@psd401.net"], "export-collide-two"),
            mbox(build_message(
                message_id="<collide@psd401.net>", body="Same different bytes",
            )),
            "export-collide-two",
        )
        self.assertEqual(RecordItem.objects.filter(request=self.prr).count(), 2)
        # Rewind both to the pre-metadata state and make them true copies, the
        # shape a legacy ingest would have left behind.
        RecordItem.objects.update(email_message_id="", email_body_sha256="")
        stderr = io.StringIO()

        with patch(
            "apps.records.storage._object_store_client",
            return_value=self.client_stub,
        ), patch(
            "apps.records.management.commands."
            "backfill_record_email_metadata.parse_email_metadata",
            side_effect=lambda raw: _fixed_identity(parse_email_metadata(raw)),
        ):
            call_command(
                "backfill_record_email_metadata",
                "--commit",
                stdout=io.StringIO(),
                stderr=stderr,
            )

        filled = RecordItem.objects.exclude(email_message_id="")
        self.assertEqual(filled.count(), 1)
        self.assertIn("already carries this message", stderr.getvalue())
        self.assertTrue(
            AuditLog.objects.filter(
                action="record.email_metadata.backfilled",
            ).exists()
        )

    def test_a_dry_run_predicts_the_collisions_a_commit_would_hit(self):
        # A dry run that always reports zero collisions overstates what
        # --commit will do. An operator reading "would write metadata for 2 of
        # 2" and then seeing one record left untouched has been told the wrong
        # thing about the record set.
        self._ingest(
            self._task(["alice@psd401.net"], "export-dry-collide-one"),
            mbox(build_message(message_id="<dry-collide@psd401.net>")),
            "export-dry-collide-one",
        )
        self._ingest(
            self._task(["bob@psd401.net"], "export-dry-collide-two"),
            mbox(build_message(
                message_id="<dry-collide@psd401.net>", body="Other bytes",
            )),
            "export-dry-collide-two",
        )
        self.assertEqual(RecordItem.objects.filter(request=self.prr).count(), 2)
        RecordItem.objects.update(email_message_id="", email_body_sha256="")
        stderr = io.StringIO()
        stdout = io.StringIO()

        with patch(
            "apps.records.storage._object_store_client",
            return_value=self.client_stub,
        ), patch(
            "apps.records.management.commands."
            "backfill_record_email_metadata.parse_email_metadata",
            side_effect=lambda raw: _fixed_identity(parse_email_metadata(raw)),
        ):
            call_command(
                "backfill_record_email_metadata",
                stdout=stdout,
                stderr=stderr,
            )

        self.assertIn("would leave it unchanged", stderr.getvalue())
        self.assertIn("1 already recorded on another record", stdout.getvalue())
        # Still a dry run: nothing was written.
        self.assertFalse(RecordItem.objects.exclude(email_message_id="").exists())

    def test_backfill_writes_nothing_without_commit(self):
        self._ingest(
            self._task(["alice@psd401.net"], "export-dry"),
            mbox(build_message(message_id="<dry@psd401.net>")),
            "export-dry",
        )
        RecordItem.objects.update(email_message_id="")

        call_command(
            "backfill_record_email_metadata",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        self.assertEqual(
            RecordItem.objects.get(request=self.prr).email_message_id, ""
        )


@override_settings(PRR_DISTRICT_EMAIL_DOMAINS=("psd401.net",))
class RecordTagTests(TestCase):
    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Tagging.",
        )
        self.index = 0

    def _item(self, **fields):
        self.index += 1
        defaults = {
            "request": self.prr,
            "original_filename": f"record-{self.index}.pdf",
            "storage_key": f"records/{self.prr.pk}/{self.index}.pdf",
            "content_length": 10,
            "content_type": "application/pdf",
            "sha256": f"{self.index:064d}",
        }
        defaults.update(fields)
        return RecordItem.objects.create(**defaults)

    @override_settings(PRR_DISTRICT_EMAIL_DOMAINS=())
    def test_nothing_is_external_when_no_district_domain_is_configured(self):
        # With nothing configured as the district's own, "external" is not a
        # fact the application has. Tagging every record external would tell a
        # reviewer that staff mail came from outside, on the strength of an
        # unset setting.
        staff = self._item(email_sender_domain="psd401.net")
        vendor = self._item(email_sender_domain="vendor.example")

        self.assertNotIn(
            record_tags.EXTERNAL_SENDER, record_tags.tags_for_item(staff),
        )
        self.assertNotIn(
            record_tags.EXTERNAL_SENDER, record_tags.tags_for_item(vendor),
        )
        self.assertEqual(
            record_tags.filter_by_tag(
                RecordItem.objects.all(), record_tags.EXTERNAL_SENDER,
            ).count(),
            0,
        )

    def test_auto_reply_is_tagged_from_the_header_or_the_subject(self):
        from_header = self._item(email_auto_submitted="auto-replied")
        from_subject = self._item(
            original_filename="Out of Office: budget question.pdf",
        )
        plain = self._item(email_auto_submitted="no")

        self.assertIn(record_tags.AUTO_REPLY, record_tags.tags_for_item(from_header))
        self.assertIn(record_tags.AUTO_REPLY, record_tags.tags_for_item(from_subject))
        self.assertNotIn(record_tags.AUTO_REPLY, record_tags.tags_for_item(plain))

    def test_calendar_bulk_and_external_tags_read_stored_facts(self):
        calendar = self._item(email_has_calendar_part=True)
        bulk = self._item(email_list_id="staff-news <news.psd401.net>")
        external = self._item(email_sender_domain="vendor.example")
        internal = self._item(email_sender_domain="psd401.net")

        self.assertIn(record_tags.CALENDAR, record_tags.tags_for_item(calendar))
        self.assertIn(record_tags.BULK_LIST, record_tags.tags_for_item(bulk))
        self.assertIn(
            record_tags.EXTERNAL_SENDER, record_tags.tags_for_item(external)
        )
        self.assertNotIn(
            record_tags.EXTERNAL_SENDER, record_tags.tags_for_item(internal)
        )

    def test_counsel_domain_tags_nothing_until_an_administrator_enters_one(self):
        item = self._item(email_sender_domain="lawfirm.example")

        self.assertNotIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

        CounselDomain.objects.create(domain="lawfirm.example")

        self.assertIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

    def test_counsel_domain_matches_a_recipient_not_only_the_sender(self):
        CounselDomain.objects.create(domain="lawfirm.example")
        item = self._item(
            email_sender_domain="psd401.net",
            email_cc="counsel@lawfirm.example",
        )

        self.assertIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

    def test_counsel_second_in_a_multi_address_from_is_still_matched(self):
        # A From header may carry several addresses, and email_sender_domain
        # records only the first. Counsel sitting second would otherwise be
        # missed by both halves of the test, in a feature whose whole job is
        # not missing privileged mail.
        CounselDomain.objects.create(domain="lawfirm.example")
        item = self._item(
            email_sender_domain="vendor.example",
            email_from="ann@vendor.example, counsel@lawfirm.example",
        )

        self.assertIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

    def test_a_lookalike_domain_in_a_multi_address_from_is_not_matched(self):
        CounselDomain.objects.create(domain="lawfirm.example")
        item = self._item(
            email_sender_domain="vendor.example",
            email_from=(
                "ann@vendor.example, imposter@lawfirm.example.attacker.test"
            ),
        )

        self.assertNotIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

    def test_an_inactive_counsel_domain_stops_tagging(self):
        CounselDomain.objects.create(domain="lawfirm.example", active=False)
        item = self._item(email_sender_domain="lawfirm.example")

        self.assertNotIn(
            record_tags.COUNSEL_DOMAIN, record_tags.tags_for_item(item)
        )

    def test_privilege_marker_reads_the_subject_and_the_extracted_text(self):
        subject = self._item(
            original_filename="Privileged and Confidential memo.pdf",
        )
        body = self._item(ocr_text="This is attorney work product, do not share.")
        plain = self._item(ocr_text="Bus routes for September.")

        self.assertIn(
            record_tags.PRIVILEGE_MARKER, record_tags.tags_for_item(subject)
        )
        self.assertIn(
            record_tags.PRIVILEGE_MARKER, record_tags.tags_for_item(body)
        )
        self.assertNotIn(
            record_tags.PRIVILEGE_MARKER, record_tags.tags_for_item(plain)
        )

    def test_displayed_tags_and_filtered_tags_always_agree(self):
        CounselDomain.objects.create(domain="lawfirm.example")
        items = [
            self._item(email_auto_submitted="auto-generated"),
            self._item(email_has_calendar_part=True),
            self._item(email_precedence="bulk"),
            self._item(email_sender_domain="vendor.example"),
            self._item(email_to="counsel@lawfirm.example"),
            self._item(ocr_text="attorney-client communication"),
            self._item(email_sender_domain="psd401.net"),
        ]

        for tag in record_tags.TAG_NAMES:
            filtered = set(
                record_tags.filter_by_tag(
                    RecordItem.objects.all(), tag,
                ).values_list("pk", flat=True)
            )
            displayed = {
                item.pk
                for item in items
                if tag in record_tags.tags_for_item(item)
            }
            self.assertEqual(filtered, displayed, f"tag {tag} disagrees")

    def test_privilege_queue_is_the_union_of_the_two_privilege_tags(self):
        CounselDomain.objects.create(domain="lawfirm.example")
        counsel = self._item(email_sender_domain="lawfirm.example")
        marker = self._item(ocr_text="attorney work product")
        other = self._item(email_sender_domain="vendor.example")

        queue = set(
            record_tags.filter_by_tag(
                RecordItem.objects.all(), "privilege",
            ).values_list("pk", flat=True)
        )

        self.assertEqual(queue, {counsel.pk, marker.pk})
        self.assertNotIn(other.pk, queue)

    def test_an_unknown_tag_matches_nothing_rather_than_everything(self):
        self._item()

        self.assertEqual(
            record_tags.filter_by_tag(
                RecordItem.objects.all(), "not-a-tag",
            ).count(),
            0,
        )


class BulkReviewTests(TestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user(
            "bulk-reviewer", "bulk-reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.outsider = get_user_model().objects.create_user(
            "bulk-outsider", "bulk-outsider@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.outsider, [StaffRole.INTAKE_CLERK])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Bulk review.",
        )
        self.items = [self._item(index) for index in range(5)]

    def _item(self, index, request=None):
        request = request or self.prr
        return RecordItem.objects.create(
            request=request,
            original_filename=f"Automatic reply {index}.pdf",
            storage_key=f"records/{request.pk}/bulk-{index}.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(
                f"{request.pk}-{index}".encode()
            ).hexdigest(),
        )

    def test_one_pass_marks_every_selected_record_with_its_own_audit_row(self):
        reviewed = bulk_review_records(
            [item.pk for item in self.items],
            state=RecordItem.ReviewState.NON_RESPONSIVE,
            actor=self.reviewer,
        )

        self.assertEqual(len(reviewed), 5)
        self.assertEqual(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            ).count(),
            5,
        )
        self.assertEqual(
            AuditLog.objects.filter(action="record.reviewed").count(), 5
        )
        bulk_rows = AuditLog.objects.filter(action="record.bulk_reviewed")
        self.assertEqual(bulk_rows.count(), 1)
        self.assertEqual(bulk_rows.first().detail["record_item_count"], 5)
        self.assertEqual(bulk_rows.first().actor_id, self.reviewer.pk)

    def test_a_record_with_no_redaction_is_never_treated_as_redacted(self):
        # The blocked set is asked of the redactions themselves. Asking
        # RecordItem for `redactions__voided_at__isnull=True` promotes the
        # join to a LEFT OUTER and matches every record that has no redaction
        # at all -- which is every out-of-office reply this feature exists to
        # clear in one pass.
        reviewed = bulk_review_records(
            [item.pk for item in self.items],
            state=RecordItem.ReviewState.NON_RESPONSIVE,
            actor=self.reviewer,
        )

        self.assertEqual(len(reviewed), len(self.items))

    def test_one_redacted_record_stops_the_whole_batch_and_writes_nothing(self):
        # All-or-nothing on purpose: a partially applied batch would leave the
        # reviewer believing a decision was recorded for records it was not.
        # The named record is the only one with a redaction; the other four
        # are ordinary and would otherwise have gone through.
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personal information",
            description="Personal information in any file.",
        )
        blocked = self.items[2]
        Redaction.objects.create(
            record_item=blocked,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=exemption,
            created_by=self.reviewer,
        )

        with self.assertRaises(ValidationError) as raised:
            bulk_review_records(
                [item.pk for item in self.items],
                state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertIn(str(blocked.pk), str(raised.exception))
        self.assertEqual(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.UNREVIEWED,
            ).count(),
            5,
        )
        # Nothing was written, so nothing was audited: no half-audited batch.
        self.assertFalse(
            AuditLog.objects.filter(
                action__in=["record.reviewed", "record.bulk_reviewed"],
            ).exists()
        )

    def test_a_record_withheld_in_full_is_revisited_one_at_a_time(self):
        # A withheld record carries an exemption citation a named reviewer
        # chose. One confirmation over a visible set must not quietly drop it.
        withheld = self.items[0]
        exemption = ExemptionCitation.objects.create(
            citation="RCW 5.60.060(2)(a)",
            short_name="Attorney-client privilege",
            description="Communications between the district and counsel.",
        )
        withheld.review_state = RecordItem.ReviewState.WITHHELD
        withheld.withholding_exemption = exemption
        withheld.save(
            update_fields=["review_state", "withholding_exemption"],
        )
        RecordItemWithholdingExemption.objects.create(
            record_item=withheld, exemption=exemption,
        )

        with self.assertRaises(Exception) as caught:
            bulk_review_records(
                [item.pk for item in self.items],
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertIn("withheld in full", str(caught.exception))
        self.assertIn(str(withheld.pk), str(caught.exception))
        # Nothing in the set moved: the action is refused whole.
        self.assertEqual(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.RESPONSIVE,
            ).count(),
            0,
        )

    def test_a_record_marked_duplicate_is_revisited_one_at_a_time(self):
        # A duplicate finding is a reviewer's judgement about which copy is
        # the record. One confirmation over a visible set must not overturn it.
        duplicate = self.items[1]
        duplicate.review_state = RecordItem.ReviewState.DUPLICATE
        duplicate.save(update_fields=["review_state"])

        with self.assertRaises(Exception) as caught:
            bulk_review_records(
                [item.pk for item in self.items],
                state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertIn("marked duplicates", str(caught.exception))
        self.assertIn(str(duplicate.pk), str(caught.exception))
        # Nothing in the set moved, and the duplicate finding stands.
        self.assertEqual(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            ).count(),
            0,
        )
        self.assertEqual(
            RecordItem.objects.get(pk=duplicate.pk).review_state,
            RecordItem.ReviewState.DUPLICATE,
        )
        self.assertFalse(
            AuditLog.objects.filter(
                action__in=["record.reviewed", "record.bulk_reviewed"],
            ).exists()
        )

    def test_the_cap_is_two_hundred_records(self):
        import uuid

        self.assertEqual(BULK_REVIEW_MAX_ITEMS, 200)
        too_many = [uuid.uuid4() for _ in range(BULK_REVIEW_MAX_ITEMS + 1)]

        with self.assertRaises(Exception) as caught:
            bulk_review_records(
                too_many,
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertIn("at most", str(caught.exception))

    def test_duplicate_ids_collapse_instead_of_counting_twice(self):
        reviewed = bulk_review_records(
            [self.items[0].pk, self.items[0].pk, self.items[1].pk],
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )

        self.assertEqual(len(reviewed), 2)

    def test_withheld_and_duplicate_outcomes_are_refused(self):
        for state in (
            RecordItem.ReviewState.WITHHELD,
            RecordItem.ReviewState.DUPLICATE,
            RecordItem.ReviewState.UNREVIEWED,
        ):
            with self.assertRaises(Exception):
                bulk_review_records(
                    [self.items[0].pk], state=state, actor=self.reviewer,
                )

    def test_a_non_review_role_cannot_bulk_review(self):
        with self.assertRaises(Exception):
            bulk_review_records(
                [self.items[0].pk],
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.outsider,
            )

    def test_records_of_two_requests_cannot_be_reviewed_in_one_action(self):
        other = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="second@example.org"),
            description="Another case.",
        )
        stranger = self._item(99, request=other)

        with self.assertRaises(Exception):
            bulk_review_records(
                [self.items[0].pk, stranger.pk],
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )

    def test_a_missing_record_aborts_the_whole_action(self):
        import uuid

        with self.assertRaises(Exception):
            bulk_review_records(
                [self.items[0].pk, uuid.uuid4()],
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )
        self.assertEqual(
            RecordItem.objects.exclude(
                review_state=RecordItem.ReviewState.UNREVIEWED,
            ).count(),
            0,
        )

    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(Exception):
            bulk_review_records(
                [],
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )


class RecordItemCustodianModelTests(TestCase):
    def test_a_custodian_is_recorded_once_per_record(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Custodians.",
        )
        item = RecordItem.objects.create(
            request=prr,
            original_filename="one.pdf",
            storage_key=f"records/{prr.pk}/one.pdf",
            content_length=1,
            content_type="application/pdf",
            sha256="b" * 64,
        )
        RecordItemCustodian.objects.create(
            record_item=item, custodian="alice@psd401.net",
        )

        with self.assertRaises(Exception):
            RecordItemCustodian.objects.create(
                record_item=item, custodian="alice@psd401.net",
            )


@override_settings(PRR_DISTRICT_EMAIL_DOMAINS=("psd401.net",))
class FoldDuplicateRecordCopiesTests(_VaultIngestFixture, TestCase):
    """Pre-dedup per-mailbox copies fold into the record that survived.

    Built the way production got here: the second mailbox's copy was ingested
    and then rewound to the pre-metadata state, so the first mailbox's copy
    reaches ingest with nothing to deduplicate against and becomes its own
    record. That leaves exactly the shape the metadata backfill reported --
    one record carrying the message and one identical copy carrying no
    Message-ID at all.
    """

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "fold-officer", "fold-officer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.outsider = get_user_model().objects.create_user(
            "fold-outsider", "fold-outsider@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.outsider, [StaffRole.INTAKE_CLERK])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.client_stub = _ObjectClient()

    def _pre_dedup_pair(self, *, message_id="<shared@psd401.net>", **kwargs):
        """Return (copy, survivor): bob's rewound copy and alice's record."""
        self._ingest(
            self._task(["bob@psd401.net"], "export-bob"),
            mbox(build_message(
                message_id=message_id, to=("bob@psd401.net",), **kwargs,
            )),
            "export-bob",
        )
        copy = RecordItem.objects.get(request=self.prr)
        RecordItem.objects.filter(pk=copy.pk).update(
            email_message_id="", email_body_sha256="",
        )
        self._ingest(
            self._task(["alice@psd401.net"], "export-alice"),
            mbox(build_message(
                message_id=message_id, to=("alice@psd401.net",), **kwargs,
            )),
            "export-alice",
        )
        survivor = RecordItem.objects.exclude(pk=copy.pk).get(request=self.prr)
        return RecordItem.objects.get(pk=copy.pk), survivor

    def _run(self, **options):
        stdout, stderr = io.StringIO(), io.StringIO()
        options.setdefault("reviewed_by", self.officer.username)
        with patch(
            "apps.records.storage._object_store_client",
            return_value=self.client_stub,
        ):
            call_command(
                "fold_duplicate_record_copies",
                stdout=stdout,
                stderr=stderr,
                **options,
            )
        return stdout.getvalue(), stderr.getvalue()

    def test_two_custodian_copies_fold_to_one_survivor_naming_both(self):
        copy, survivor = self._pre_dedup_pair()

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        survivor.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.DUPLICATE)
        self.assertEqual(copy.reviewed_by, self.officer)
        self.assertEqual(
            survivor.review_state, RecordItem.ReviewState.UNREVIEWED,
        )
        self.assertEqual(
            sorted(survivor.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        self.assertIn("folded : 1", stdout)

    def test_each_fold_and_the_run_are_audited_with_the_previous_state(self):
        copy, survivor = self._pre_dedup_pair()

        self._run(commit=True)

        folded = AuditLog.objects.get(action="record.copy_folded")
        self.assertEqual(folded.target, str(copy.pk))
        self.assertEqual(folded.actor, self.officer)
        self.assertEqual(
            folded.detail["survivor_record_item_id"], str(survivor.pk),
        )
        self.assertEqual(folded.detail["request_number"], self.prr.number)
        reviewed = AuditLog.objects.get(action="record.reviewed")
        self.assertEqual(
            reviewed.detail["before"]["state"],
            RecordItem.ReviewState.UNREVIEWED,
        )
        summary = AuditLog.objects.get(action="record.copies_folded")
        self.assertEqual(summary.detail["folded"], 1)
        self.assertEqual(summary.actor, self.officer)

    def test_a_reviewed_copy_is_skipped_and_named(self):
        copy, _ = self._pre_dedup_pair()
        RecordItem.objects.filter(pk=copy.pk).update(
            review_state=RecordItem.ReviewState.RESPONSIVE,
        )

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.RESPONSIVE)
        self.assertIn(str(copy.pk), stdout)
        self.assertIn("already reviewed", stdout)
        self.assertIn("folded : 0", stdout)

    def test_a_copy_with_an_active_redaction_is_skipped_and_named(self):
        copy, _ = self._pre_dedup_pair()
        Redaction.objects.create(
            record_item=copy,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ExemptionCitation.objects.create(
                citation="RCW 42.56.230(3)",
                short_name="Personal information",
                description="Personal information in any file.",
            ),
            created_by=self.officer,
        )

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("active redaction", stdout)
        self.assertIn(str(copy.pk), stdout)

    def test_a_copy_already_in_an_installment_is_skipped_and_named(self):
        copy, _ = self._pre_dedup_pair()
        installment = Installment.objects.create(request=self.prr, sequence=1)
        installment.items.add(copy)

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("installment", stdout)
        self.assertIn(str(copy.pk), stdout)

    def test_an_attachment_only_copy_never_folds(self):
        # No text part means no body digest, and half an identity is exactly
        # what ingest refuses to deduplicate on. The same rule holds here.
        copy, _survivor = self._pre_dedup_pair(
            message_id="<scan@psd401.net>", attachment=b"scanned bytes",
        )

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("no body digest", stdout)

    def test_a_copy_with_no_survivor_is_skipped_and_named(self):
        self._ingest(
            self._task(["bob@psd401.net"], "export-lonely"),
            mbox(build_message(message_id="<lonely@psd401.net>")),
            "export-lonely",
        )
        copy = RecordItem.objects.get(request=self.prr)
        RecordItem.objects.filter(pk=copy.pk).update(
            email_message_id="", email_body_sha256="",
        )

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("no surviving record", stdout)

    def test_a_survivor_already_marked_duplicate_does_not_absorb_the_copy(self):
        copy, survivor = self._pre_dedup_pair()
        RecordItem.objects.filter(pk=survivor.pk).update(
            review_state=RecordItem.ReviewState.DUPLICATE,
        )

        stdout, _ = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("itself marked duplicate", stdout)

    def test_a_dry_run_writes_nothing_and_matches_the_commit_counts(self):
        copy, survivor = self._pre_dedup_pair()

        dry_stdout, _ = self._run()

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertEqual(survivor.custodians.count(), 1)
        self.assertFalse(
            AuditLog.objects.filter(action="record.copy_folded").exists()
        )
        self.assertFalse(
            AuditLog.objects.filter(action="record.copies_folded").exists()
        )
        self.assertIn("Dry run", dry_stdout)
        self.assertIn("would fold : 1", dry_stdout)
        self.assertIn("skipped : 0", dry_stdout)

        commit_stdout, _ = self._run(commit=True)

        self.assertIn("folded : 1", commit_stdout)
        self.assertIn("skipped : 0", commit_stdout)

    def test_the_pass_is_scoped_to_one_request_when_asked(self):
        copy, _ = self._pre_dedup_pair()
        other = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Another case.",
        )

        stdout, _ = self._run(commit=True, request_number=other.number)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIn("examined : 0", stdout)

    def test_a_copy_reviewed_after_planning_is_left_alone_and_reported(self):
        # Planning reads the queue once; writing happens later. A reviewer who
        # decides the copy in between is not overruled: the fold re-reads the
        # copy under a lock, steps back, and says so.
        copy, survivor = self._pre_dedup_pair()
        original_plan = FoldCommand._plan

        def plan_then_review(command, artifacts):
            planned = original_plan(command, artifacts)
            RecordItem.objects.filter(pk=copy.pk).update(
                review_state=RecordItem.ReviewState.RESPONSIVE,
            )
            return planned

        with patch.object(FoldCommand, "_plan", plan_then_review):
            stdout, stderr = self._run(commit=True)

        copy.refresh_from_db()
        self.assertEqual(copy.review_state, RecordItem.ReviewState.RESPONSIVE)
        self.assertIn("changed since planning", stderr)
        self.assertIn("folded : 0", stdout)
        self.assertIn("failed : 1", stdout)
        self.assertFalse(
            AuditLog.objects.filter(action="record.copy_folded").exists()
        )
        # The survivor was not touched either: nothing about that copy moved.
        self.assertFalse(
            survivor.custodians.filter(custodian="bob@psd401.net").exists()
        )

    def test_two_copies_folding_into_one_survivor_each_see_the_other(self):
        # Both copies fold in one run. The survivor is re-read for each fold,
        # so the second sees the primary custodian the first repaired rather
        # than a stale plan-time instance that would write over it.
        for custodian, export_id in (
            ("bob@psd401.net", "export-bob"),
            ("carol@psd401.net", "export-carol"),
        ):
            self._ingest(
                self._task([custodian], export_id),
                mbox(build_message(to=(custodian,))),
                export_id,
            )
            RecordItem.objects.filter(request=self.prr).update(
                email_message_id="", email_body_sha256="",
            )
        self._ingest(
            self._task(["alice@psd401.net"], "export-alice"),
            mbox(build_message(to=("alice@psd401.net",))),
            "export-alice",
        )
        survivor = RecordItem.objects.get(
            request=self.prr, email_message_id="<one@psd401.net>",
        )
        RecordItem.objects.filter(pk=survivor.pk).update(custodian="")

        stdout, _ = self._run(commit=True)

        survivor.refresh_from_db()
        self.assertIn("folded : 2", stdout)
        self.assertEqual(
            sorted(survivor.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net", "carol@psd401.net"],
        )
        # Repaired once, by the first fold; the second fold saw it filled.
        self.assertEqual(survivor.custodian, "bob@psd401.net")
        self.assertEqual(
            RecordItem.objects.filter(
                request=self.prr,
                review_state=RecordItem.ReviewState.DUPLICATE,
            ).count(),
            2,
        )

    def test_a_folded_copys_message_now_points_at_the_survivor_for_ingest(self):
        copy, survivor = self._pre_dedup_pair()
        bob_bytes = mbox(build_message(
            message_id="<shared@psd401.net>", to=("bob@psd401.net",),
        ))

        self._run(commit=True)

        artifact = VaultMessageArtifact.objects.get(duplicate_of=survivor)
        self.assertEqual(
            artifact.outcome, VaultMessageArtifact.Outcome.DUPLICATE,
        )
        self.assertIsNone(artifact.record_item)
        # A later mailbox yields the byte-identical message. Ingest's byte-hash
        # check must land its custodian on the record under review, not on
        # the copy that was set aside.
        self._ingest(
            self._task(["dave@psd401.net"], "export-dave"),
            bob_bytes,
            "export-dave",
        )
        self.assertIn(
            "dave@psd401.net",
            list(survivor.custodians.values_list("custodian", flat=True)),
        )
        self.assertFalse(
            copy.custodians.filter(custodian="dave@psd401.net").exists()
        )

    def test_an_unknown_request_number_is_refused(self):
        with self.assertRaises(CommandError):
            self._run(request_number="PRR-0000-0000")

    def test_a_user_without_a_review_role_is_refused(self):
        self._pre_dedup_pair()

        with self.assertRaises(CommandError) as raised:
            self._run(commit=True, reviewed_by=self.outsider.username)

        self.assertIn("review", str(raised.exception).lower())
        self.assertFalse(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.DUPLICATE,
            ).exists()
        )

    def test_an_unknown_user_is_refused(self):
        with self.assertRaises(CommandError):
            self._run(commit=True, reviewed_by="nobody-at-all")

    # The guard that no background task can invoke this fold lives with the
    # other human-only entry points, in
    # ``test_compliance.test_no_automated_path_exists``.
