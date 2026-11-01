"""Per-mailbox custodians read from a Vault export's retained metadata CSV.

Three outcomes this covers:

* the parser reads Google's own column names and says what it could not read;
* an ingest whose collection task names no custodian still records every
  *staff* mailbox the export's metadata CSV says held the message, and no
  student mailbox;
* the backfill command attaches the same facts to records ingested before the
  CSV was ever parsed, names what it will not guess at, and touches no
  review state.
"""

import hashlib
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.email_metadata import parse_delivered_to
from apps.records.management.commands.backfill_vault_custodians import (
    Command as BackfillCommand,
)
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    RecordItemCustodian,
    Requester,
    VaultExportObject,
)
from apps.records.storage import generated_vault_export_storage_key
from apps.records.tests.test_review_throughput import (
    _ObjectClient,
    build_message,
    mbox,
)
from apps.records.vault_ingest import (
    ConvertedVaultRecord,
    attach_custodians,
    ingest_completed_export,
)
from apps.records.vault_metadata import (
    custodian_accounts,
    is_metadata_csv_object,
    normalize_message_id,
    parse_metadata_csv,
)

STAFF_DOMAINS = ("psd401.net",)

METADATA_HEADER = "Rfc822MessageId,GmailMessageId,FileName,Account\n"


def metadata_csv(rows):
    """Google's export metadata CSV for the given ``(message_id, account)``."""
    body = "".join(
        f"{message_id},18f00{index},mail-{index}.mbox,{account}\n"
        for index, (message_id, account) in enumerate(rows)
    )
    return (METADATA_HEADER + body).encode("utf-8")


class ParseMetadataCsvTests(TestCase):
    def test_reads_googles_columns_and_normalizes_both_sides(self):
        payload = (
            METADATA_HEADER
            + "<one@psd401.net>,18f001,a.mbox,Alice@PSD401.net\n"
            + "one@psd401.net,18f002,b.mbox,bob@psd401.net\n"
            + "<two@psd401.net>,18f003,c.mbox,carol@psd401.net\n"
        )

        parsed = parse_metadata_csv(payload)

        self.assertEqual(
            parsed.accounts,
            {
                "<one@psd401.net>": {"alice@psd401.net", "bob@psd401.net"},
                "<two@psd401.net>": {"carol@psd401.net"},
            },
        )
        self.assertEqual(parsed.rows, 3)
        self.assertEqual(parsed.message_ids, 2)

    def test_a_row_with_no_message_id_is_ignored_and_counted(self):
        payload = (
            METADATA_HEADER
            + "<one@psd401.net>,18f001,a.mbox,alice@psd401.net\n"
            + ",18f002,b.mbox,ghost@psd401.net\n"
            + "   ,18f003,c.mbox,other@psd401.net\n"
        )

        parsed = parse_metadata_csv(payload)

        self.assertEqual(
            parsed.accounts, {"<one@psd401.net>": {"alice@psd401.net"}},
        )
        self.assertEqual(parsed.rows, 3)
        self.assertEqual(parsed.blank_message_id_rows, 2)
        self.assertNotIn(
            "ghost@psd401.net",
            {account for accounts in parsed.accounts.values() for account in accounts},
        )

    def test_a_row_with_no_account_is_ignored_and_counted(self):
        parsed = parse_metadata_csv(
            METADATA_HEADER + "<one@psd401.net>,18f001,a.mbox,\n"
        )

        self.assertEqual(parsed.accounts, {})
        self.assertEqual(parsed.blank_account_rows, 1)

    def test_a_byte_order_mark_does_not_hide_the_message_id_column(self):
        parsed = parse_metadata_csv(
            "﻿" + METADATA_HEADER
            + "<one@psd401.net>,18f001,a.mbox,alice@psd401.net\n"
        )

        self.assertEqual(
            parsed.accounts, {"<one@psd401.net>": {"alice@psd401.net"}},
        )

    def test_a_file_without_googles_columns_yields_nothing_rather_than_raising(self):
        parsed = parse_metadata_csv("one,two\nthree,four\n")

        self.assertEqual(parsed.accounts, {})
        self.assertEqual(parse_metadata_csv("").accounts, {})

    def test_message_id_normalization_is_stable_in_both_directions(self):
        self.assertEqual(
            normalize_message_id("one@psd401.net"), "<one@psd401.net>",
        )
        self.assertEqual(
            normalize_message_id("  <one@psd401.net>  "), "<one@psd401.net>",
        )
        self.assertEqual(normalize_message_id(""), "")
        self.assertEqual(normalize_message_id("<>"), "")

    def test_an_over_long_message_id_keeps_its_closing_bracket(self):
        oversized = "a" * 2000 + "@psd401.net"

        normalized = normalize_message_id(oversized)

        self.assertTrue(normalized.startswith("<"))
        self.assertTrue(normalized.endswith(">"))
        self.assertLessEqual(len(normalized), 998)
        self.assertEqual(
            normalize_message_id(f"<{oversized}>"), normalized,
        )

    def test_metadata_object_is_recognized_by_its_suffix(self):
        self.assertTrue(is_metadata_csv_object("mail/export-1-metadata.csv"))
        self.assertTrue(is_metadata_csv_object("Export-1-Metadata.CSV"))
        self.assertFalse(is_metadata_csv_object("mail/export-1.mbox"))
        self.assertFalse(is_metadata_csv_object("mail/other.csv"))


@override_settings(PRR_CUSTODIAN_ACCOUNT_DOMAINS=STAFF_DOMAINS)
class CustodianAccountDomainTests(TestCase):
    def test_only_configured_domains_are_attachable_and_the_rest_are_counted(self):
        keep, declined = custodian_accounts([
            "alice@psd401.net",
            "1000567@edtools.psd401.net",
            "vendor@example.org",
            "BOB@psd401.net",
        ])

        self.assertEqual(keep, ["alice@psd401.net", "bob@psd401.net"])
        self.assertEqual(declined, 2)

    @override_settings(PRR_CUSTODIAN_ACCOUNT_DOMAINS=())
    def test_an_empty_domain_list_attaches_nothing(self):
        keep, declined = custodian_accounts(["alice@psd401.net"])

        self.assertEqual(keep, [])
        self.assertEqual(declined, 1)

    def test_a_subdomain_is_not_the_configured_domain(self):
        keep, _declined = custodian_accounts(["a@mail.psd401.net"])

        self.assertEqual(keep, [])


    def test_an_account_that_is_not_one_address_is_declined_not_written(self):
        # A corrupt or hostile CSV cell can wrap a district domain around a
        # control character or a space. An address is one token; anything
        # else is not an address, and it is counted and dropped before the
        # domain check could pass it on the strength of its tail.
        kept, declined = custodian_accounts([
            "alice@psd401.net",
            "legit\r\ninjected@psd401.net",
            "esc\x1b[0m@psd401.net",
            "two words@psd401.net",
        ])

        self.assertEqual(kept, ["alice@psd401.net"])
        self.assertEqual(declined, 3)


class DeliveredToParsingTests(TestCase):
    """What one copy's own ``Delivered-To`` header does and does not prove."""

    def _delivered_to(self, headers):
        return parse_delivered_to(build_message(headers=headers))

    def test_one_header_names_the_mailbox_that_held_this_copy(self):
        self.assertEqual(
            self._delivered_to((("Delivered-To", "Alice@PSD401.net"),)),
            "alice@psd401.net",
        )

    def test_a_display_name_around_the_address_still_names_the_mailbox(self):
        self.assertEqual(
            self._delivered_to(
                (("Delivered-To", "Alice Adams <alice@psd401.net>"),),
            ),
            "alice@psd401.net",
        )

    def test_no_header_names_nothing_rather_than_guessing(self):
        self.assertEqual(self._delivered_to(()), "")

    def test_two_headers_name_nothing_because_neither_hop_is_the_mailbox(self):
        # A forwarded message accumulates one Delivered-To per hop, and the
        # last is not reliably where the copy came to rest. Guessing here
        # would attribute a public record to a mailbox that never held it.
        self.assertEqual(
            self._delivered_to((
                ("Delivered-To", "alice@psd401.net"),
                ("Delivered-To", "list@psd401.net"),
            )),
            "",
        )

    def test_two_addresses_in_one_header_name_nothing(self):
        self.assertEqual(
            self._delivered_to(
                (("Delivered-To", "alice@psd401.net, bob@psd401.net"),),
            ),
            "",
        )

    def test_a_control_character_makes_the_value_not_one_address(self):
        for hostile in (
            "esc\x1b[0m@psd401.net",
            "two words@psd401.net",
            "a@b@psd401.net",
            "@psd401.net",
            "alice@",
        ):
            with self.subTest(hostile=hostile):
                self.assertEqual(
                    self._delivered_to((("Delivered-To", hostile),)), "",
                )

class _MetadataExportFixture:
    """Ingest a stubbed export carrying an optional metadata CSV object."""

    def _task(self, custodians, export_id="export-1"):
        return CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.INGESTING,
            instructions="Retain and ingest the completed export.",
            custodian_emails=custodians,
            vault_matter_id="matter-1",
            vault_export_id=export_id,
        )

    def _ingest(self, task, source, *, csv_payload=None, export_id="export-1"):
        objects = {f"mail/{export_id}.mbox": source}
        if csv_payload is not None:
            objects[f"mail/{export_id}-metadata.csv"] = csv_payload
        export = {
            "id": export_id,
            "status": "COMPLETED",
            "cloudStorageSink": {
                "files": [
                    {
                        "bucketName": "vault-bucket",
                        "objectName": object_name,
                        "size": str(len(payload)),
                    }
                    for object_name, payload in objects.items()
                ],
            },
        }

        def download(bucket, object_name, destination):
            destination.write(objects[object_name])

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


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_CUSTODIAN_ACCOUNT_DOMAINS=STAFF_DOMAINS,
)
class VaultIngestReadsExportMetadataTests(_MetadataExportFixture, TestCase):
    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.client_stub = _ObjectClient()
        self.source = mbox(build_message(message_id="<one@psd401.net>"))

    def test_empty_task_custodian_list_still_records_the_csv_staff_mailboxes(self):
        task = self._task([])

        run = self._ingest(
            task,
            self.source,
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("one@psd401.net", "bob@psd401.net"),
                ("<one@psd401.net>", "1000567@edtools.psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        self.assertEqual(item.custodian, "alice@psd401.net")
        self.assertEqual(
            run.export_snapshot["custodian_metadata"]["status"], "parsed",
        )
        self.assertEqual(
            run.export_snapshot["custodian_metadata"]["message_ids"], 1,
        )
        # Counted so a run can say how much it declined, never named.
        self.assertEqual(
            run.export_snapshot["custodian_metadata"][
                "accounts_outside_domains"
            ],
            1,
        )
        self.assertNotIn(
            "edtools", str(run.export_snapshot["custodian_metadata"]),
        )

    def test_the_task_custodian_list_is_added_to_never_replaced(self):
        task = self._task(["carol@psd401.net"])

        self._ingest(
            task,
            self.source,
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "carol@psd401.net"],
        )
        self.assertEqual(item.custodian, "carol@psd401.net")

    def test_a_second_mailboxs_copy_adds_its_csv_custodians_to_the_one_record(self):
        self._ingest(
            self._task([]),
            self.source,
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
            ]),
        )
        self._ingest(
            self._task([], export_id="export-2"),
            self.source,
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "bob@psd401.net"),
            ]),
            export_id="export-2",
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net"],
        )

    def test_a_sole_record_takes_the_csv_set_once_the_run_has_read_everything(self):
        # One copy, one record. When the run ends it is the sole live record
        # carrying its Message-ID, so the CSV's set is exactly the mailboxes
        # that held the message and every one of them belongs on it.
        run = self._ingest(
            self._task([]),
            mbox(build_message(
                message_id="<one@psd401.net>",
                headers=(("Delivered-To", "alice@psd401.net"),),
            )),
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "bob@psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        summary = run.export_snapshot["custodian_metadata"]
        self.assertEqual(summary["delivered_to_missing"], 0)
        self.assertEqual(summary["csv_sets_attached"], 1)

    def test_the_first_copy_of_a_bulk_mailing_never_takes_the_whole_set(self):
        # Two personalized copies in one export, neither naming its mailbox.
        # The first is, at the moment it is read, the only live record
        # carrying the Message-ID; a per-message decision would give it every
        # recipient. Decided once the run has read both, neither is sole and
        # neither takes the set.
        run = self._ingest(
            self._task([]),
            mbox(
                build_message(message_id="<one@psd401.net>", body="Alice copy"),
                build_message(message_id="<one@psd401.net>", body="Bob copy"),
            ),
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "bob@psd401.net"),
            ]),
        )

        records = list(RecordItem.objects.filter(request=self.prr))
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record.custodians.count(), 0)
            self.assertEqual(record.custodian, "")
        summary = run.export_snapshot["custodian_metadata"]
        self.assertEqual(summary["delivered_to_missing"], 2)
        self.assertEqual(summary["csv_sets_attached"], 0)

    def test_a_delivered_to_the_csv_does_not_corroborate_is_treated_as_absent(self):
        # A sender can write a Delivered-To header. When the export's own
        # metadata names the mailboxes that held the message, a header naming
        # another one is not believed: the copy is counted as naming nothing,
        # and the record gets only what the CSV proves once the run ends.
        run = self._ingest(
            self._task([]),
            mbox(build_message(
                message_id="<one@psd401.net>",
                headers=(("Delivered-To", "mallory@psd401.net"),),
            )),
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            list(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net"],
        )
        self.assertEqual(
            run.export_snapshot["custodian_metadata"]["delivered_to_missing"],
            1,
        )

    def test_folded_copies_each_contribute_their_own_mailbox(self):
        # Nothing is lost by stopping at the copy's own mailbox: a message the
        # fold really does collapse is collapsed one copy at a time, and the
        # surviving record ends up naming every mailbox a copy came from.
        for index, (export_id, mailbox) in enumerate((
            ("export-1", "alice@psd401.net"),
            ("export-2", "bob@psd401.net"),
            ("export-3", "carol@psd401.net"),
        )):
            self._ingest(
                self._task([], export_id=export_id),
                mbox(build_message(
                    message_id="<one@psd401.net>",
                    headers=(("Delivered-To", mailbox),),
                )),
                export_id=export_id,
            )
            self.assertEqual(RecordItem.objects.count(), 1, index)

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net", "carol@psd401.net"],
        )

    def test_a_copy_sharing_a_message_id_gets_only_its_own_delivered_to(self):
        first_task = self._task([])
        self._ingest(
            first_task,
            mbox(build_message(
                message_id="<one@psd401.net>",
                body="Alice copy",
                headers=(("Delivered-To", "alice@psd401.net"),),
            )),
        )
        first = RecordItem.objects.get(request=self.prr)
        # The pre-deduplication shape: a personalized bulk mailing whose
        # copies differ in body, so the fold left them as distinct records
        # and one request holds several live records of one Message-ID.
        RecordItem.objects.filter(pk=first.pk).update(email_body_sha256="")

        self._ingest(
            self._task([], export_id="export-2"),
            mbox(build_message(
                message_id="<one@psd401.net>",
                body="Bob copy",
                headers=(("Delivered-To", "bob@psd401.net"),),
            )),
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "bob@psd401.net"),
                ("<one@psd401.net>", "carol@psd401.net"),
            ]),
            export_id="export-2",
        )

        second = RecordItem.objects.exclude(pk=first.pk).get(request=self.prr)
        # Bob's mailbox, and only Bob's: the CSV's set names every recipient
        # of the mailing, and attaching it here would say this copy sat in
        # Alice's and Carol's mailboxes too.
        self.assertEqual(
            list(second.custodians.values_list("custodian", flat=True)),
            ["bob@psd401.net"],
        )
        self.assertEqual(
            list(first.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net"],
        )

    def test_a_message_with_no_usable_delivered_to_is_counted_on_the_run(self):
        run = self._ingest(
            self._task(["carol@psd401.net"]),
            mbox(build_message(
                message_id="<one@psd401.net>",
                headers=(
                    ("Delivered-To", "alice@psd401.net"),
                    ("Delivered-To", "relay@psd401.net"),
                ),
            )),
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "bob@psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        # Two hops name no mailbox, so this falls back to what the run knew
        # before the header was read: the task's list plus, because this
        # record is sole, the CSV's set.
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["bob@psd401.net", "carol@psd401.net"],
        )
        self.assertEqual(
            run.export_snapshot["custodian_metadata"]["delivered_to_missing"],
            1,
        )

    def test_a_student_delivered_to_is_refused_at_the_write_boundary(self):
        self._ingest(
            self._task([]),
            mbox(build_message(
                message_id="<one@psd401.net>",
                headers=(("Delivered-To", "1000567@edtools.psd401.net"),),
            )),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(item.custodians.count(), 0)
        self.assertEqual(item.custodian, "")

    def test_a_missing_metadata_csv_degrades_to_the_previous_behaviour(self):
        run = self._ingest(self._task(["carol@psd401.net"]), self.source)

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            list(item.custodians.values_list("custodian", flat=True)),
            ["carol@psd401.net"],
        )
        self.assertEqual(
            run.export_snapshot["custodian_metadata"],
            # The fixture message carries no Delivered-To either, so the run
            # says both of the things it could not learn about custody.
            {
                "status": "absent",
                "objects": 0,
                "delivered_to_missing": 1,
                "csv_sets_attached": 0,
            },
        )

    def test_an_unparseable_metadata_csv_is_recorded_and_never_fails_the_export(self):
        run = self._ingest(
            self._task(["carol@psd401.net"]),
            self.source,
            csv_payload=b"not,a,vault,manifest\n1,2,3,4\n",
        )

        self.assertEqual(run.state, run.State.COMPLETE)
        self.assertEqual(
            run.export_snapshot["custodian_metadata"]["status"], "empty",
        )
        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            list(item.custodians.values_list("custodian", flat=True)),
            ["carol@psd401.net"],
        )

    def test_the_write_boundary_itself_refuses_an_account_outside_the_domains(self):
        # Every caller filters first, and the function that writes filters
        # again: a future caller that forgets cannot put a student mailbox on
        # a record. The task's own list is the officer's and is not second-
        # guessed here; only the CSV-derived extras are.
        task = self._task([])
        self._ingest(task, self.source)
        item = RecordItem.objects.get(email_message_id="<one@psd401.net>")

        attach_custodians(
            item, task, ["1000567@edtools.psd401.net", "Bob@psd401.net"],
        )

        self.assertEqual(
            list(item.custodians.values_list("custodian", flat=True)),
            ["bob@psd401.net"],
        )
        item.refresh_from_db()
        self.assertEqual(item.custodian, "bob@psd401.net")

    def test_a_student_only_message_records_no_custodian_at_all(self):
        self._ingest(
            self._task([]),
            self.source,
            csv_payload=metadata_csv([
                ("<one@psd401.net>", "1000567@edtools.psd401.net"),
            ]),
        )

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(item.custodians.count(), 0)
        self.assertEqual(item.custodian, "")


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_CUSTODIAN_ACCOUNT_DOMAINS=STAFF_DOMAINS,
)
class BackfillVaultCustodiansTests(_MetadataExportFixture, TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "custody-officer",
            "custody-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.outsider = get_user_model().objects.create_user(
            "custody-outsider",
            "custody-outsider@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.outsider, [StaffRole.INTAKE_CLERK])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.client_stub = _ObjectClient()

    def _retain_metadata_csv(self, task, payload):
        """Retain a metadata CSV for an export that was ingested without one.

        This is the production shape: the object was retained with the rest of
        the export and never parsed.
        """
        storage_key = generated_vault_export_storage_key(
            task.request_id,
            task.pk,
            object_name=f"mail/{task.vault_export_id}-metadata.csv",
        )
        self.client_stub.objects[storage_key] = payload
        from django.utils import timezone

        return VaultExportObject.objects.create(
            ingest_run=task.vault_ingest_run,
            object_identity_sha256=hashlib.sha256(
                f"vault-bucket\0{task.vault_export_id}-metadata.csv".encode()
            ).hexdigest(),
            bucket_name="vault-bucket",
            object_name=f"mail/{task.vault_export_id}-metadata.csv",
            expected_size=len(payload),
            storage_key=storage_key,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            state=VaultExportObject.State.COMPLETE,
            retained_at=timezone.now(),
            processed_at=timezone.now(),
        )

    def _run(self, **options):
        stdout, stderr = io.StringIO(), io.StringIO()
        options.setdefault("run_by", self.officer.username)
        with patch(
            "apps.records.storage._object_store_client",
            return_value=self.client_stub,
        ):
            call_command(
                "backfill_vault_custodians",
                stdout=stdout,
                stderr=stderr,
                **options,
            )
        return stdout.getvalue(), stderr.getvalue()

    def _ingested_record(self, message_id="<one@psd401.net>"):
        task = self._task([])
        self._ingest(task, mbox(build_message(message_id=message_id)))
        return task, RecordItem.objects.get(email_message_id=message_id)

    def test_dry_run_reports_what_a_commit_would_do_and_writes_nothing(self):
        task, item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "bob@psd401.net"),
                ("<one@psd401.net>", "1000567@edtools.psd401.net"),
            ]),
        )

        stdout, _ = self._run()

        self.assertIn("would attach : 1", stdout)
        self.assertIn("records examined : 1", stdout)
        self.assertIn("accounts outside the configured domains : 1", stdout)
        self.assertIn("Dry run; nothing written.", stdout)
        self.assertEqual(item.custodians.count(), 0)
        self.assertFalse(
            AuditLog.objects.filter(
                action="record.custodians_backfilled",
            ).exists()
        )

    def test_commit_attaches_the_staff_accounts_and_repairs_the_primary(self):
        task, item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([
                ("<one@psd401.net>", "bob@psd401.net"),
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "1000567@edtools.psd401.net"),
            ]),
        )

        stdout, _ = self._run(commit=True)

        item.refresh_from_db()
        self.assertEqual(
            sorted(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net", "bob@psd401.net"],
        )
        self.assertEqual(item.custodian, "alice@psd401.net")
        self.assertEqual(
            item.review_state, RecordItem.ReviewState.UNREVIEWED,
        )
        self.assertIn("attached : 1", stdout)

    def test_the_run_writes_one_audit_row_with_its_counts(self):
        task, _item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([
                ("<one@psd401.net>", "alice@psd401.net"),
                ("<one@psd401.net>", "1000567@edtools.psd401.net"),
            ]),
        )

        self._run(commit=True)

        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.detail["examined"], 1)
        self.assertEqual(row.detail["attached"], 1)
        self.assertEqual(row.detail["accounts_outside_domains"], 1)
        self.assertEqual(row.detail["tasks_with_metadata_csv"], 1)

    def test_a_message_id_the_csv_does_not_carry_is_named_and_skipped(self):
        task, item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([("<other@psd401.net>", "alice@psd401.net")]),
        )

        stdout, _ = self._run(commit=True)

        item.refresh_from_db()
        self.assertEqual(item.custodians.count(), 0)
        self.assertIn("Message-ID absent from this export", stdout)
        self.assertIn(str(item.pk), stdout)

    def test_a_shared_message_id_never_takes_the_csv_set(self):
        """Two live copies, neither naming its mailbox: the CSV set is still
        not attachable, because it describes both copies at once."""
        task, first = self._ingested_record()
        # Strip the first copy's body digest so the second ingest cannot
        # deduplicate onto it: exactly the pre-deduplication shape in which
        # one request holds two live records of one message.
        RecordItem.objects.filter(pk=first.pk).update(email_body_sha256="")
        second_task = self._task([], export_id="export-2")
        self._ingest(
            second_task,
            mbox(build_message(message_id="<one@psd401.net>", body="Other")),
            export_id="export-2",
        )
        self._retain_metadata_csv(
            task,
            metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )
        self.assertEqual(
            RecordItem.objects.filter(
                request=self.prr, email_message_id="<one@psd401.net>",
            ).count(),
            2,
        )

        stdout, _ = self._run(commit=True)

        self.assertIn("share this Message-ID", stdout)
        self.assertEqual(
            RecordItem.objects.get(pk=first.pk).custodians.count(), 0,
        )

    def _ambiguous_pair(self, second_headers):
        """Two live records of one request carrying one Message-ID.

        The production shape behind this command's ``Delivered-To`` path: a
        personalized bulk mailing whose copies differ in body, so the fold
        left each as its own record and the Message-ID-keyed CSV cannot say
        which copy sat in which mailbox. Every copy's custody is cleared
        afterwards so what the pass asserts is what *this pass* attached.
        """
        first_task = self._task([])
        self._ingest(
            first_task,
            mbox(build_message(
                message_id="<one@psd401.net>",
                body="Alice copy",
                headers=(("Delivered-To", "alice@psd401.net"),),
            )),
        )
        first = RecordItem.objects.get(request=self.prr)
        RecordItem.objects.filter(pk=first.pk).update(email_body_sha256="")
        second_task = self._task([], export_id="export-2")
        self._ingest(
            second_task,
            mbox(build_message(
                message_id="<one@psd401.net>",
                body="Bob copy",
                headers=second_headers,
            )),
            export_id="export-2",
        )
        second = RecordItem.objects.exclude(pk=first.pk).get(request=self.prr)
        payload = metadata_csv([
            ("<one@psd401.net>", "alice@psd401.net"),
            ("<one@psd401.net>", "bob@psd401.net"),
            ("<one@psd401.net>", "carol@psd401.net"),
        ])
        self._retain_metadata_csv(first_task, payload)
        self._retain_metadata_csv(second_task, payload)
        RecordItemCustodian.objects.all().delete()
        RecordItem.objects.all().update(custodian="")
        return first, second

    def _custodians(self, record):
        return list(
            RecordItem.objects.get(pk=record.pk)
            .custodians.values_list("custodian", flat=True)
        )

    def test_an_ambiguous_pair_is_resolved_one_mailbox_each(self):
        first, second = self._ambiguous_pair(
            (("Delivered-To", "bob@psd401.net"),),
        )

        stdout, _ = self._run(commit=True)

        # Each copy names its own mailbox and never the other's, and never
        # the CSV's set, which names every recipient of the mailing.
        self.assertEqual(self._custodians(first), ["alice@psd401.net"])
        self.assertEqual(self._custodians(second), ["bob@psd401.net"])
        self.assertIn("resolved from this copy's Delivered-To : 2", stdout)
        self.assertIn("attached : 2", stdout)

    def test_a_dry_run_predicts_the_same_delivered_to_resolution(self):
        first, second = self._ambiguous_pair(
            (("Delivered-To", "bob@psd401.net"),),
        )

        stdout, _ = self._run()

        self.assertIn("resolved from this copy's Delivered-To : 2", stdout)
        self.assertIn("would attach : 2", stdout)
        self.assertEqual(self._custodians(first), [])
        self.assertEqual(self._custodians(second), [])

    def test_a_delivered_to_the_csv_does_not_corroborate_is_named_and_skipped(self):
        # The pair's CSV names alice, bob and carol for this Message-ID; the
        # second copy's header names a mailbox the CSV never saw.
        first, copy = self._ambiguous_pair(
            (("Delivered-To", "mallory@psd401.net"),),
        )

        stdout, stderr = self._run(commit=True)

        self.assertEqual(self._custodians(first), ["alice@psd401.net"])
        self.assertEqual(self._custodians(copy), [])
        self.assertIn("not one the export's metadata CSV names", stdout)
        self.assertNotIn("mallory", stdout + stderr)

    def test_a_copy_with_two_delivered_to_headers_is_named_and_skipped(self):
        _first, second = self._ambiguous_pair((
            ("Delivered-To", "bob@psd401.net"),
            ("Delivered-To", "relay@psd401.net"),
        ))

        stdout, _ = self._run(commit=True)

        self.assertEqual(self._custodians(second), [])
        self.assertIn("carries no", stdout)
        self.assertIn("single Delivered-To header", stdout)
        self.assertIn(str(second.pk), stdout)
        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.detail["delivered_to_missing"], 1)
        self.assertEqual(row.detail["resolved_by_delivered_to"], 1)

    def test_a_student_delivered_to_is_counted_and_never_printed(self):
        _first, second = self._ambiguous_pair(
            (("Delivered-To", "1000567@edtools.psd401.net"),),
        )

        stdout, stderr = self._run(commit=True)

        self.assertEqual(self._custodians(second), [])
        self.assertIn(
            "Delivered-To mailboxes outside the configured domains : 1",
            stdout,
        )
        self.assertNotIn("1000567", stdout)
        self.assertNotIn("1000567", stderr)
        self.assertNotIn("edtools", stdout)
        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.detail["delivered_to_outside_domains"], 1)

    def test_a_copy_with_no_retained_raw_message_is_named_and_skipped(self):
        _first, second = self._ambiguous_pair(
            (("Delivered-To", "bob@psd401.net"),),
        )
        second.vault_source_messages.update(
            raw_storage_key="", raw_sha256="", raw_size=None,
        )

        stdout, _ = self._run(commit=True)

        self.assertEqual(self._custodians(second), [])
        self.assertIn("has no", stdout)
        self.assertIn("retained raw message", stdout)
        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.detail["skipped_no_retained_message"], 1)

    def test_a_retained_raw_message_that_cannot_be_read_is_named_and_skipped(self):
        _first, second = self._ambiguous_pair(
            (("Delivered-To", "bob@psd401.net"),),
        )
        # The retained bytes no longer match the digest recorded for them.
        for artifact in second.vault_source_messages.all():
            self.client_stub.objects[artifact.raw_storage_key] = b"tampered"

        stdout, stderr = self._run(commit=True)

        self.assertEqual(self._custodians(second), [])
        self.assertIn("could not be read", stdout)
        self.assertIn("unusable", stderr)
        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.detail["skipped_retained_message_unreadable"], 1)

    def test_a_task_with_no_retained_csv_is_named_and_skipped(self):
        task, item = self._ingested_record()

        stdout, _ = self._run()

        self.assertIn("no retained metadata CSV", stdout)
        self.assertIn(f"skip task {task.pk}", stdout)
        self.assertEqual(item.custodians.count(), 0)

    def test_a_record_already_marked_duplicate_is_left_alone(self):
        task, item = self._ingested_record()
        RecordItem.objects.filter(pk=item.pk).update(
            review_state=RecordItem.ReviewState.DUPLICATE,
        )
        self._retain_metadata_csv(
            task,
            metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )

        stdout, _ = self._run(commit=True)

        self.assertIn("records examined : 0", stdout)
        self.assertEqual(
            RecordItem.objects.get(pk=item.pk).custodians.count(), 0,
        )

    def test_a_record_marked_duplicate_after_planning_is_left_alone_and_named(self):
        # Planning reads the queue once; writing happens later. A record that
        # a reviewer, or the fold command, marked duplicate in between is
        # re-read under a lock at write time and left alone, and the run says
        # so rather than reviving it with custodians.
        task, item = self._ingested_record()
        self._retain_metadata_csv(
            task, metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )
        original_plan = BackfillCommand._plan_task

        def plan_then_mark_duplicate(command, *args, **kwargs):
            result = original_plan(command, *args, **kwargs)
            RecordItem.objects.filter(pk=item.pk).update(
                review_state=RecordItem.ReviewState.DUPLICATE,
            )
            return result

        with patch.object(BackfillCommand, "_plan_task", plan_then_mark_duplicate):
            stdout, stderr = self._run(commit=True)

        self.assertEqual(item.custodians.count(), 0)
        item.refresh_from_db()
        self.assertEqual(item.custodian, "")
        self.assertIn("changed since planning", stderr)
        self.assertIn("attached : 0", stdout)
        self.assertIn("failed : 1", stdout)

    def test_a_retained_csv_that_names_no_message_reads_differently(self):
        """"No CSV" and "a CSV naming nothing" need different answers."""
        task, item = self._ingested_record()
        self._retain_metadata_csv(task, b"one,two\nthree,four\n")

        stdout, _ = self._run()

        self.assertIn("names no message", stdout)
        self.assertNotIn(
            f"skip task {task.pk} {task.request.number}: no retained",
            stdout,
        )
        self.assertEqual(item.custodians.count(), 0)

    def test_a_retained_csv_that_cannot_be_read_reads_differently(self):
        task, item = self._ingested_record()
        export_object = self._retain_metadata_csv(
            task,
            metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )
        # The retained bytes no longer match the digest recorded for them,
        # which is exactly what the integrity check exists to catch.
        self.client_stub.objects[export_object.storage_key] = (
            b"corrupted payload"
        )

        stdout, stderr = self._run()

        self.assertIn("could not be read", stdout)
        self.assertIn("metadata CSV unusable", stderr)
        self.assertEqual(item.custodians.count(), 0)

    def test_the_audit_row_names_the_officer_who_ran_the_pass(self):
        task, _item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )

        stdout, _ = self._run(commit=True)

        row = AuditLog.objects.get(action="record.custodians_backfilled")
        self.assertEqual(row.actor, self.officer)
        self.assertIn(f"Run by: {self.officer.username}", stdout)

    def test_a_user_without_a_review_role_cannot_run_the_pass(self):
        with self.assertRaises(CommandError):
            self._run(run_by=self.outsider.username)

    def test_an_unknown_username_is_an_error(self):
        with self.assertRaises(CommandError):
            self._run(run_by="nobody-at-all")

    def test_a_mistyped_request_number_is_an_error_not_a_clean_zero(self):
        with self.assertRaises(CommandError):
            self._run(request_number="PRR-2026-9999")

    def test_scoping_to_one_request_leaves_another_untouched(self):
        task, item = self._ingested_record()
        self._retain_metadata_csv(
            task,
            metadata_csv([("<one@psd401.net>", "alice@psd401.net")]),
        )
        other = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Another case.",
        )

        stdout, _ = self._run(request_number=other.number)

        self.assertIn("records examined : 0", stdout)
        self.assertEqual(item.custodians.count(), 0)


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_CUSTODIAN_ACCOUNT_DOMAINS=STAFF_DOMAINS,
)
class VaultIngestResumeReadsRetainedMetadataTests(
    _MetadataExportFixture,
    TestCase,
):
    """A resumed run re-reads the retained CSV instead of downloading again."""

    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.client_stub = _ObjectClient()

    def test_a_metadata_download_failure_fails_the_run_rather_than_degrading(self):
        """Retention is not parsing.

        A transient download failure is a problem with the object, identical
        to the ones the main loop marks failed and re-raises so the ordinary
        retry picks them up. Swallowing it as "unreadable content" would
        permanently strip custodian evidence from a healthy run on a network
        blip -- the very failure this path exists to fix.
        """
        task = self._task([])
        source = mbox(build_message(message_id="<one@psd401.net>"))
        csv_payload = metadata_csv([
            ("<one@psd401.net>", "alice@psd401.net"),
        ])
        export = {
            "id": task.vault_export_id,
            "status": "COMPLETED",
            "cloudStorageSink": {
                "files": [
                    {
                        "bucketName": "vault-bucket",
                        "objectName": object_name,
                        "size": str(size),
                    }
                    for object_name, size in (
                        (f"mail/{task.vault_export_id}.mbox", len(source)),
                        (
                            f"mail/{task.vault_export_id}-metadata.csv",
                            len(csv_payload),
                        ),
                    )
                ],
            },
        }

        def failing_download(bucket, object_name, destination):
            if object_name.endswith("-metadata.csv"):
                raise RuntimeError("connection reset while downloading")
            destination.write(source)

        with self.assertRaises(RuntimeError):
            ingest_completed_export(
                task.pk,
                export=export,
                _download=failing_download,
                _client=self.client_stub,
                _convert=lambda raw: None,
                _scan=lambda key, **kwargs: (True, "OK"),
                _cleanup_quarantine=lambda key, **kwargs: None,
            )

        task.refresh_from_db()
        run = task.vault_ingest_run
        self.assertEqual(run.state, run.State.FAILED)
        failed = VaultExportObject.objects.get(
            ingest_run=run, object_name__endswith="-metadata.csv",
        )
        self.assertEqual(failed.state, VaultExportObject.State.FAILED)
        self.assertNotEqual(failed.last_error_detail, "")
        self.assertNotEqual(task.state, CollectionTask.State.COMPLETE)

    def test_a_retried_run_parses_the_already_retained_metadata_csv(self):
        task = self._task([])
        source = mbox(build_message(message_id="<one@psd401.net>"))
        csv_payload = metadata_csv([
            ("<one@psd401.net>", "alice@psd401.net"),
        ])
        run = self._ingest(task, source, csv_payload=csv_payload)
        # Rewind to the state a crashed attempt leaves behind: the objects are
        # retained, the run is not complete, and the lease is stale.
        run.state = run.State.FAILED
        run.lease_token = None
        run.heartbeat_at = None
        run.save(update_fields=["state", "lease_token", "heartbeat_at"])
        task.refresh_from_db()
        task.state = CollectionTask.State.INGESTING
        task.save(update_fields=["state"])
        VaultExportObject.objects.filter(ingest_run=run).update(
            state=VaultExportObject.State.RETAINED,
            processed_at=None,
        )

        export = {
            "id": task.vault_export_id,
            "status": "COMPLETED",
            "cloudStorageSink": {
                "files": [
                    {
                        "bucketName": "vault-bucket",
                        "objectName": object_name,
                        "size": str(size),
                    }
                    for object_name, size in (
                        (f"mail/{task.vault_export_id}.mbox", len(source)),
                        (
                            f"mail/{task.vault_export_id}-metadata.csv",
                            len(csv_payload),
                        ),
                    )
                ],
            },
        }

        def refuse_download(bucket, object_name, destination):
            raise AssertionError(
                f"{object_name} was downloaded again after retention"
            )

        def convert(raw_message):
            return ConvertedVaultRecord(
                content=b"%PDF-1.7\n",
                filename="Board packet.pdf",
                content_type="application/pdf",
                ocr_text="Board packet",
            )

        def scan(storage_key, *, expected_size, expected_sha256, client):
            return True, "OK"

        def cleanup(storage_key, *, client):
            client.objects.pop(storage_key, None)

        resumed = ingest_completed_export(
            task.pk,
            export=export,
            _download=refuse_download,
            _client=self.client_stub,
            _convert=convert,
            _scan=scan,
            _cleanup_quarantine=cleanup,
        )

        self.assertEqual(resumed.state, resumed.State.COMPLETE)
        self.assertEqual(
            resumed.export_snapshot["custodian_metadata"]["status"], "parsed",
        )
        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(
            list(item.custodians.values_list("custodian", flat=True)),
            ["alice@psd401.net"],
        )
