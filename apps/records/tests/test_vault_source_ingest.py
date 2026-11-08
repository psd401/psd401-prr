"""Drive files and Chat messages from a Vault export become candidate records.

Chat exports are ZIP archives of MBOX files, so they take the message path
Gmail takes. Drive exports are ZIP archives of native files (Docs as DOCX,
Sheets as XLSX, and so on): each file is quarantined, scanned, and retained
as the original, and its review path comes from ``handling.py``. Every record
says which source it came from.
"""

import hashlib
import io
import zipfile
from email.message import EmailMessage
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    RecordItemCustodian,
    Requester,
    VaultIngestRun,
    VaultMessageArtifact,
)
from apps.records.vault_ingest import (
    ConvertedVaultRecord,
    VaultIngestError,
    VaultIngestPermanentError,
    ingest_completed_export,
)


class _ObjectClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        source = kwargs["Body"]
        position = source.tell()
        payload = source.read()
        source.seek(position)
        if kwargs["Key"] in self.objects and not kwargs["Key"].startswith("quarantine/"):
            raise AssertionError("immutable object was overwritten")
        self.objects[kwargs["Key"]] = payload

    def get_object(self, *, Bucket, Key):
        class _Body(io.BytesIO):
            def iter_chunks(self, chunk_size):
                while chunk := self.read(chunk_size):
                    yield chunk

        return {"Body": _Body(self.objects[Key])}


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _chat_mbox():
    message = EmailMessage()
    message["From"] = "teacher@psd401.net"
    message["To"] = "space@psd401.net"
    message["Subject"] = "Chat: Bus route"
    message["Message-ID"] = "<chat-1@example.org>"
    message.set_content("The route changes Monday.")
    raw = message.as_bytes().replace(b"\r\n", b"\n")
    return b"From teacher@psd401.net Sat Aug  1 12:00:00 2026\n" + raw + b"\n\n"


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_VAULT_EXPORT_MAX_BYTES=10 * 1024 * 1024,
    PRR_VAULT_EXPORT_TOTAL_MAX_BYTES=20 * 1024 * 1024,
    PRR_VAULT_ARCHIVE_MAX_MEMBERS=100,
    PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES=20 * 1024 * 1024,
)
class _Fixture(TestCase):
    kind = CollectionTask.Kind.VAULT_DRIVE

    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="ingest@example.org"),
            description="Records about a bus route.",
        )
        self.task = CollectionTask.objects.create(
            request=self.prr,
            kind=self.kind,
            state=CollectionTask.State.INGESTING,
            custodian_emails=["transport@psd401.net"],
            vault_matter_id="matter-1",
            vault_export_id="export-1",
        )
        self.store = _ObjectClient()
        self.objects = {}
        self.scanned = []
        self.infected = set()
        self.cleaned = []

    def export(self, objects):
        self.objects = objects
        return {
            "id": "export-1",
            "status": "COMPLETED",
            "cloudStorageSink": {"files": [
                {"bucketName": "vault-bucket", "objectName": name, "size": str(len(payload))}
                for name, payload in objects.items()
            ]},
        }

    def download(self, bucket, object_name, destination):
        destination.write(self.objects[object_name])

    def scan(self, storage_key, *, expected_size, expected_sha256, client):
        payload = client.objects[storage_key]
        self.assertEqual(len(payload), expected_size)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
        self.scanned.append(storage_key)
        return (payload not in self.infected), "OK"

    def cleanup(self, storage_key, *, client):
        self.cleaned.append(storage_key)
        client.objects.pop(storage_key, None)

    def convert(self, raw_message):
        return ConvertedVaultRecord(
            content=b"%PDF-1.7\n" + hashlib.sha256(raw_message).hexdigest().encode(),
            filename="Chat.pdf",
            content_type="application/pdf",
            ocr_text="The route changes Monday.",
        )

    def ingest(self, export, **overrides):
        return ingest_completed_export(
            self.task.pk,
            export=export,
            _download=self.download,
            _client=self.store,
            _convert=self.convert,
            _scan=overrides.pop("_scan", self.scan),
            _cleanup_quarantine=self.cleanup,
        )


@patch("apps.records.scanned_records.queue_record_derivative")
class DriveIngestTests(_Fixture):
    def test_each_drive_file_becomes_a_record_from_drive(self, queue_derivative):
        docx = b"PK-docx bytes of a minutes document"
        xlsx = b"PK-xlsx bytes of a route sheet"
        archive = _zip({
            "Minutes_1AbC.docx": docx,
            "folder/Route sheet_2DeF.xlsx": xlsx,
            "folder/": b"",
        })
        export = self.export({
            "drive/export-1_0.zip": archive,
            "drive/export-1-metadata.xml": b"<Root/>",
            "drive/export-1-custodian-docid.csv": b"Custodian,DocID\n",
        })

        with self.captureOnCommitCallbacks(execute=True):
            run = self.ingest(export)

        self.assertEqual(run.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(run.items_created, 2)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.COMPLETE)
        items = {item.original_filename: item for item in RecordItem.objects.all()}
        self.assertEqual(set(items), {"Minutes_1AbC.docx", "Route sheet_2DeF.xlsx"})
        minutes = items["Minutes_1AbC.docx"]
        self.assertEqual(minutes.source_system, "google_vault_drive")
        self.assertEqual(minutes.collection_task, self.task)
        self.assertEqual(
            minutes.content_type,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(minutes.sha256, hashlib.sha256(docx).hexdigest())
        self.assertEqual(self.store.objects[minutes.storage_key], docx)
        self.assertTrue(minutes.storage_key.startswith(f"records/{self.prr.pk}/"))
        self.assertEqual(minutes.custodian, "transport@psd401.net")
        self.assertTrue(RecordItemCustodian.objects.filter(record_item=minutes).exists())
        self.assertEqual(len(self.scanned), 2)
        self.assertTrue(all(key.startswith("quarantine/vault-messages/") for key in self.scanned))
        self.assertFalse([key for key in self.store.objects if key.startswith("quarantine/")])
        self.assertEqual(queue_derivative.call_count, 2)
        artifact = VaultMessageArtifact.objects.get(record_item=minutes)
        self.assertEqual(artifact.raw_sha256, minutes.sha256)
        self.assertTrue(AuditLog.objects.filter(action="vault.ingest.completed").exists())
        self.assertTrue(
            self.prr.events.filter(note__startswith="Vault Drive collection completed: 2 file(s)").exists()
        )

    def test_the_same_file_twice_is_one_record(self, _queue):
        archive = _zip({"a/Minutes_1.docx": b"same", "b/Minutes_1 copy.docx": b"same"})
        run = self.ingest(self.export({"drive/export-1_0.zip": archive}))
        self.assertEqual(run.items_created, 1)
        self.assertEqual(run.duplicates_skipped, 1)

    def test_a_rerun_is_idempotent(self, _queue):
        export = self.export({"drive/export-1_0.zip": _zip({"Minutes.docx": b"one"})})
        self.ingest(export)
        VaultIngestRun.objects.update(state=VaultIngestRun.State.FAILED)
        self.ingest(export)
        self.assertEqual(RecordItem.objects.count(), 1)

    def test_an_infected_file_is_refused_and_the_rest_ingest(self, _queue):
        self.infected.add(b"bad bytes")
        archive = _zip({"bad.docx": b"bad bytes", "good.docx": b"good bytes"})
        run = self.ingest(self.export({"drive/export-1_0.zip": archive}))
        self.assertEqual(run.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(
            list(RecordItem.objects.values_list("original_filename", flat=True)), ["good.docx"],
        )
        failed = VaultMessageArtifact.objects.get(outcome=VaultMessageArtifact.Outcome.FAILED)
        self.assertEqual(failed.last_error_code, "file_malware_detected")
        self.assertTrue(AuditLog.objects.filter(action="vault.file_malware_detected").exists())
        self.assertFalse([key for key in self.store.objects if key.startswith("quarantine/")])

    def test_a_scanner_outage_retries_the_export(self, _queue):
        from apps import secure_files

        def unavailable(*args, **kwargs):
            raise secure_files.AttachmentScanUnavailable("down")

        with self.assertRaises(VaultIngestError) as raised:
            self.ingest(
                self.export({"drive/export-1_0.zip": _zip({"a.docx": b"a"})}), _scan=unavailable,
            )
        self.assertEqual(raised.exception.code, "file_malware_scan_unavailable")
        self.assertFalse(RecordItem.objects.exists())

    def test_a_drive_export_that_is_not_a_zip_is_refused(self, _queue):
        with self.assertRaises(VaultIngestPermanentError):
            self.ingest(self.export({"drive/export-1.pst": b"nope"}))

    def test_an_unsafe_member_is_refused(self, _queue):
        with self.assertRaises(VaultIngestPermanentError):
            self.ingest(self.export({"drive/export-1_0.zip": _zip({"../escape.docx": b"x"})}))

    def test_a_file_over_the_limit_is_recorded_and_the_rest_ingest(self, _queue):
        archive = _zip({"big.mp4": b"too many bytes", "small.docx": b"ok"})
        export = self.export({"drive/export-1_0.zip": archive})
        with patch("apps.records.vault_ingest._drive_file_limit", return_value=4):
            run = self.ingest(export)
        self.assertEqual(run.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(
            list(RecordItem.objects.values_list("original_filename", flat=True)), ["small.docx"],
        )
        failed = VaultMessageArtifact.objects.get(outcome=VaultMessageArtifact.Outcome.FAILED)
        self.assertEqual(failed.last_error_code, "file_too_large")


class ChatIngestTests(_Fixture):
    kind = CollectionTask.Kind.VAULT_CHAT

    def test_chat_messages_take_the_message_path_and_say_chat(self):
        export = self.export({
            "chat/export-1_0.zip": _zip({"transport@psd401.net.mbox": _chat_mbox()}),
            "chat/export-1-metadata.xml": b"<Root/>",
        })
        run = self.ingest(export)
        self.assertEqual(run.state, VaultIngestRun.State.COMPLETE)
        item = RecordItem.objects.get()
        self.assertEqual(item.source_system, "google_vault_chat")
        self.assertEqual(item.collection_task, self.task)


class GmailSourceTests(_Fixture):
    kind = CollectionTask.Kind.VAULT_MAIL

    def test_gmail_records_keep_their_source(self):
        export = self.export({"mail/export-1.mbox": _chat_mbox()})
        self.ingest(export)
        self.assertEqual(RecordItem.objects.get().source_system, "google_vault_gmail")


class ManualTaskTests(_Fixture):
    kind = CollectionTask.Kind.MANUAL

    def test_a_manual_task_has_no_vault_export_to_ingest(self):
        with self.assertRaises(VaultIngestPermanentError):
            self.ingest(self.export({"x.mbox": b""}))


class DefaultDriveScanTests(TestCase):
    @override_settings(PRR_VAULT_EXPORT_MAX_BYTES=2 * 1024 * 1024)
    @patch("apps.records.vault_ingest.secure_files.scan_object_for_malware", return_value=(True, "OK"))
    def test_drive_files_are_scanned_against_the_file_limit(self, scan):
        from apps.records.vault_ingest import _scan_vault_file

        self.assertEqual(
            _scan_vault_file("quarantine/vault-messages/x", expected_size=1, expected_sha256="a", client=None),
            (True, "OK"),
        )
        self.assertEqual(scan.call_args.kwargs["max_bytes"], 2 * 1024 * 1024)
