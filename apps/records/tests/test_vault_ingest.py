"""Vault exports resume safely into retained, deduplicated review records."""

import hashlib
import io
import zipfile
from email.message import EmailMessage
from unittest.mock import call, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps import secure_files
from apps.audit.models import AuditLog
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    StatusEvent,
    VaultExportObject,
    VaultIngestRun,
    VaultMessageArtifact,
)
from apps.records.vault_ingest import (
    ConvertedVaultRecord,
    VaultIngestError,
    VaultIngestPermanentError,
    _default_download,
    convert_message_to_searchable_pdf,
    ingest_completed_export,
)


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.get_calls = []

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
        self.put_calls.append({**kwargs, "Body": payload})

    def get_object(self, *, Bucket, Key):
        self.get_calls.append(Key)
        return {"Body": _Body(self.objects[Key])}


def _message(subject, body, *, message_id):
    message = EmailMessage()
    message["From"] = "custodian@psd401.net"
    message["To"] = "recipient@example.org"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content(body)
    return message.as_bytes()


def _mbox(*messages):
    output = bytearray()
    for message in messages:
        output.extend(b"From custodian@psd401.net Sat Aug  1 12:00:00 2026\n")
        output.extend(message.replace(b"\r\n", b"\n"))
        output.extend(b"\n\n")
    return bytes(output)


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_VAULT_EXPORT_MAX_BYTES=10 * 1024 * 1024,
    PRR_VAULT_EXPORT_TOTAL_MAX_BYTES=20 * 1024 * 1024,
    PRR_VAULT_ARCHIVE_MAX_MEMBERS=100,
    PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES=20 * 1024 * 1024,
)
class VaultIngestTests(TestCase):
    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Export responsive mail.",
        )
        self.task = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.INGESTING,
            instructions="Retain and ingest the completed export.",
            custodian_emails=["custodian@psd401.net"],
            vault_matter_id="matter-1",
            vault_export_id="export-1",
        )
        self.messages = [
            _message("Board packet", "First body", message_id="<one@example.org>"),
            _message("Budget", "Second body", message_id="<two@example.org>"),
        ]
        self.source = _mbox(*self.messages)
        self.export = {
            "id": "export-1",
            "status": "COMPLETED",
            "cloudStorageSink": {
                "files": [{
                    "bucketName": "vault-bucket",
                    "objectName": "mail/export-1.mbox",
                    "size": str(len(self.source)),
                }],
            },
        }
        self.client = _ObjectClient()
        self.download_calls = []
        self.convert_calls = []
        self.scan_calls = []
        self.quarantine_cleanup_calls = []

    def download(self, bucket, object_name, destination):
        self.download_calls.append((bucket, object_name))
        destination.write(self.source)

    def convert(self, raw_message):
        self.convert_calls.append(raw_message)
        parsed_subject = (
            "Board packet" if b"Board packet" in raw_message else "Budget"
        )
        payload = b"%PDF-1.7\n" + hashlib.sha256(raw_message).hexdigest().encode()
        return ConvertedVaultRecord(
            content=payload,
            filename=f"{parsed_subject}.pdf",
            content_type="application/pdf",
            ocr_text=parsed_subject,
        )

    def scan(self, storage_key, *, expected_size, expected_sha256, client):
        payload = client.objects[storage_key]
        self.scan_calls.append(storage_key)
        self.assertEqual(len(payload), expected_size)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
        return True, "OK"

    def cleanup_quarantine(self, storage_key, *, client):
        self.quarantine_cleanup_calls.append(storage_key)
        del client.objects[storage_key]

    def ingest(self, **overrides):
        return ingest_completed_export(
            self.task.pk,
            export=overrides.pop("export", self.export),
            _download=overrides.pop("_download", self.download),
            _client=overrides.pop("_client", self.client),
            _convert=overrides.pop("_convert", self.convert),
            _scan=overrides.pop("_scan", self.scan),
            _cleanup_quarantine=overrides.pop(
                "_cleanup_quarantine",
                self.cleanup_quarantine,
            ),
            **overrides,
        )

    @override_settings(
        PRR_VAULT_IMPERSONATION_TARGET=(
            "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
        ),
    )
    @patch("google.cloud.storage.Client")
    @patch("apps.integrations.vault.client.vault_credentials")
    def test_default_download_passes_google_project_to_storage_client(
        self,
        vault_credentials,
        storage_client,
    ):
        credentials = object()
        vault_credentials.return_value = credentials
        destination = io.BytesIO()

        _default_download("vault-bucket", "mail/export.mbox", destination)

        storage_client.assert_called_once_with(
            project="psd401-prr-identity",
            credentials=credentials,
        )
        storage_client.return_value.bucket.assert_called_once_with(
            "vault-bucket",
        )

    def test_retains_export_and_messages_then_completes_review_queue(self):
        run = self.ingest()

        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.COMPLETE)
        self.assertIsNotNone(self.task.completed_at)
        self.assertEqual(run.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(run.files_total, 1)
        self.assertEqual(run.files_processed, 1)
        self.assertEqual(run.messages_seen, 2)
        self.assertEqual(run.items_created, 2)
        self.assertEqual(run.duplicates_skipped, 0)
        source = VaultExportObject.objects.get()
        self.assertTrue(source.storage_key.startswith(
            f"vault-exports/{self.prr.id}/{self.task.pk}/"
        ))
        self.assertEqual(source.sha256, hashlib.sha256(self.source).hexdigest())
        self.assertEqual(self.client.objects[source.storage_key], self.source)
        self.assertEqual(VaultMessageArtifact.objects.count(), 2)
        self.assertEqual(len(self.scan_calls), 2)
        self.assertEqual(len(self.quarantine_cleanup_calls), 2)
        self.assertTrue(all(
            key.startswith(
                f"quarantine/vault-messages/{self.prr.id}/{self.task.pk}/"
            )
            for key in self.scan_calls
        ))
        self.assertTrue(all(
            not key.startswith("quarantine/vault-messages/")
            for key in self.client.objects
        ))
        self.assertTrue(all(
            artifact.raw_storage_key.startswith(
                f"vault-messages/{self.prr.id}/{self.task.pk}/"
            )
            for artifact in VaultMessageArtifact.objects.all()
        ))
        items = RecordItem.objects.order_by("created_at")
        self.assertEqual(items.count(), 2)
        for item in items:
            self.assertEqual(item.request, self.prr)
            self.assertEqual(item.collection_task, self.task)
            self.assertEqual(item.source_system, "google_vault_gmail")
            self.assertEqual(item.custodian, "custodian@psd401.net")
            self.assertEqual(item.review_state, RecordItem.ReviewState.UNREVIEWED)
            self.assertEqual(
                hashlib.sha256(self.client.objects[item.storage_key]).hexdigest(),
                item.sha256,
            )
            self.assertEqual(item.content_length, len(self.client.objects[item.storage_key]))
            self.assertEqual(item.content_type, "application/pdf")
        self.assertTrue(StatusEvent.objects.filter(
            request=self.prr,
            note__contains="Vault collection completed: 2 message(s)",
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action="vault.ingest.completed",
            target=self.prr.number,
            detail__items_created=2,
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action="collection.export_complete",
            target=self.prr.number,
            detail__collection_task_id=self.task.pk,
        ).exists())

    @override_settings(
        PRR_AI_ADVISORY_ENABLED=True,
        PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
    )
    @patch("apps.records.advisory_ai.defer_automatic_record_advisory")
    def test_each_new_review_record_defers_automatic_responsiveness(self, defer):
        self.ingest()

        item_ids = list(
            RecordItem.objects.order_by("created_at").values_list("pk", flat=True)
        )
        self.assertEqual(
            defer.call_args_list,
            [
                call(
                    item_id,
                    kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                )
                for item_id in item_ids
            ],
        )

    def test_infected_raw_message_stays_quarantined_and_never_reaches_review(self):
        def infected_scan(
            storage_key,
            *,
            expected_size,
            expected_sha256,
            client,
        ):
            self.scan_calls.append(storage_key)
            self.assertEqual(len(client.objects[storage_key]), expected_size)
            self.assertEqual(
                hashlib.sha256(client.objects[storage_key]).hexdigest(),
                expected_sha256,
            )
            return False, "Eicar-Test-Signature FOUND"

        with self.assertRaisesMessage(
            VaultIngestPermanentError,
            "failed malware scanning",
        ):
            self.ingest(_scan=infected_scan)

        artifact = VaultMessageArtifact.objects.get()
        self.assertEqual(artifact.outcome, VaultMessageArtifact.Outcome.FAILED)
        self.assertEqual(artifact.last_error_code, "message_malware_detected")
        self.assertEqual(artifact.raw_storage_key, "")
        self.assertEqual(RecordItem.objects.count(), 0)
        self.assertEqual(self.convert_calls, [])
        self.assertEqual(self.quarantine_cleanup_calls, [])
        self.assertEqual(len(self.scan_calls), 1)
        self.assertIn(self.scan_calls[0], self.client.objects)
        self.assertFalse(any(
            key.startswith("vault-messages/")
            for key in self.client.objects
        ))
        run = VaultIngestRun.objects.get()
        self.assertEqual(run.last_error_code, "message_malware_detected")
        self.assertTrue(AuditLog.objects.filter(
            action="vault.message_malware_detected",
            target=self.prr.number,
            detail__collection_task_id=self.task.pk,
        ).exists())

    def test_unavailable_message_scan_fails_closed_in_quarantine(self):
        def unavailable_scan(*_args, **_kwargs):
            raise secure_files.AttachmentScanUnavailable("scanner offline")

        with self.assertRaisesMessage(
            VaultIngestError,
            "could not classify",
        ):
            self.ingest(_scan=unavailable_scan)

        artifact = VaultMessageArtifact.objects.get()
        self.assertEqual(artifact.outcome, VaultMessageArtifact.Outcome.FAILED)
        self.assertEqual(
            artifact.last_error_code,
            "message_malware_scan_unavailable",
        )
        self.assertEqual(artifact.raw_storage_key, "")
        self.assertEqual(RecordItem.objects.count(), 0)
        self.assertEqual(self.convert_calls, [])
        self.assertEqual(self.quarantine_cleanup_calls, [])
        quarantine_keys = [
            key for key in self.client.objects
            if key.startswith("quarantine/vault-messages/")
        ]
        self.assertEqual(len(quarantine_keys), 1)
        run = VaultIngestRun.objects.get()
        self.assertEqual(
            run.last_error_code,
            "message_malware_scan_unavailable",
        )
        self.assertTrue(AuditLog.objects.filter(
            action="vault.message_scan_unavailable",
            target=self.prr.number,
            detail__collection_task_id=self.task.pk,
        ).exists())

    def test_unavailable_message_scan_can_resume_after_scanner_recovers(self):
        def unavailable_scan(*_args, **_kwargs):
            raise secure_files.AttachmentScanUnavailable("scanner offline")

        with self.assertRaises(VaultIngestError):
            self.ingest(_scan=unavailable_scan)

        completed = self.ingest()

        self.assertEqual(completed.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(completed.items_created, 2)
        self.assertEqual(RecordItem.objects.count(), 2)
        self.assertFalse(any(
            key.startswith("quarantine/vault-messages/")
            for key in self.client.objects
        ))
        first_artifact = VaultMessageArtifact.objects.order_by("ordinal").first()
        self.assertEqual(
            first_artifact.outcome,
            VaultMessageArtifact.Outcome.CREATED,
        )
        self.assertEqual(first_artifact.last_error_code, "")

    def test_completed_replay_is_a_noop(self):
        first = self.ingest()
        put_count = len(self.client.put_calls)
        event_count = AuditLog.objects.filter(
            action="vault.ingest.completed",
        ).count()

        second = self.ingest(
            _download=lambda *_args: self.fail("completed replay downloaded GCS"),
            _convert=lambda *_args: self.fail("completed replay converted a message"),
        )

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(len(self.client.put_calls), put_count)
        self.assertEqual(
            AuditLog.objects.filter(action="vault.ingest.completed").count(),
            event_count,
        )

    def test_duplicate_raw_message_creates_one_record_with_two_evidence_rows(self):
        self.source = _mbox(self.messages[0], self.messages[0])
        self.export["cloudStorageSink"]["files"][0]["size"] = str(len(self.source))

        run = self.ingest()

        self.assertEqual(RecordItem.objects.count(), 1)
        self.assertEqual(VaultMessageArtifact.objects.count(), 2)
        self.assertEqual(run.items_created, 1)
        self.assertEqual(run.duplicates_skipped, 1)
        duplicate = VaultMessageArtifact.objects.get(
            outcome=VaultMessageArtifact.Outcome.DUPLICATE,
        )
        self.assertIsNotNone(duplicate.duplicate_of_id)

    def test_failed_conversion_is_durable_and_retry_resumes_retained_source(self):
        attempts = 0

        def fail_second(raw_message):
            nonlocal attempts
            attempts += 1
            if b"Budget" in raw_message:
                raise RuntimeError("renderer temporarily unavailable")
            return self.convert(raw_message)

        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            self.ingest(_convert=fail_second)

        run = VaultIngestRun.objects.get()
        self.assertEqual(run.state, VaultIngestRun.State.FAILED)
        self.assertEqual(run.last_error_code, "message_conversion_failed")
        self.assertEqual(RecordItem.objects.count(), 1)
        self.assertEqual(self.download_calls, [("vault-bucket", "mail/export-1.mbox")])
        first_put_count = len(self.client.put_calls)

        resumed = self.ingest(
            _download=lambda *_args: self.fail("retry must use retained source"),
        )

        self.assertEqual(resumed.state, VaultIngestRun.State.COMPLETE)
        self.assertEqual(RecordItem.objects.count(), 2)
        self.assertGreater(len(self.client.put_calls), first_put_count)
        self.assertEqual(
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.CREATED,
            ).count(),
            2,
        )

    def test_source_size_mismatch_fails_before_any_message_is_created(self):
        self.export["cloudStorageSink"]["files"][0]["size"] = str(
            len(self.source) + 1
        )

        with self.assertRaisesMessage(
            VaultIngestPermanentError,
            "size did not match",
        ):
            self.ingest()

        self.assertEqual(RecordItem.objects.count(), 0)
        self.assertEqual(VaultMessageArtifact.objects.count(), 0)
        run = VaultIngestRun.objects.get()
        self.assertEqual(run.state, VaultIngestRun.State.FAILED)
        self.assertEqual(run.last_error_code, "export_integrity_failed")

    def test_non_completed_export_is_refused_without_gcs_access(self):
        self.export["status"] = "IN_PROGRESS"

        with self.assertRaisesMessage(
            VaultIngestPermanentError,
            "not completed",
        ):
            self.ingest()

        self.assertEqual(self.download_calls, [])
        self.assertEqual(VaultExportObject.objects.count(), 0)

    def test_fresh_worker_lease_returns_progress_without_duplicate_work(self):
        run = VaultIngestRun.objects.create(
            collection_task=self.task,
            state=VaultIngestRun.State.RUNNING,
            heartbeat_at=timezone.now(),
            attempt_count=1,
        )

        returned = self.ingest(
            _download=lambda *_args: self.fail("fresh lease must not download"),
            _convert=lambda *_args: self.fail("fresh lease must not convert"),
        )

        self.assertEqual(returned.pk, run.pk)
        self.assertEqual(VaultExportObject.objects.count(), 0)

    def test_unsafe_zip_member_is_rejected_after_original_is_retained(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../escaped.mbox", self.source)
        self.source = archive.getvalue()
        descriptor = self.export["cloudStorageSink"]["files"][0]
        descriptor["objectName"] = "mail/export-1.zip"
        descriptor["size"] = str(len(self.source))

        with self.assertRaisesMessage(VaultIngestPermanentError, "unsafe member"):
            self.ingest()

        retained = VaultExportObject.objects.get()
        self.assertTrue(retained.storage_key)
        self.assertEqual(self.client.objects[retained.storage_key], self.source)
        self.assertEqual(RecordItem.objects.count(), 0)


class VaultMessageConversionTests(TestCase):
    def test_email_body_and_attachment_inventory_render_to_searchable_pdf(self):
        message = EmailMessage()
        message["From"] = "custodian@psd401.net"
        message["To"] = "requester@example.org"
        message["Subject"] = "Board packet"
        message["Message-ID"] = "<board-packet@example.org>"
        message.set_content("The searchable meeting agenda.")
        message.add_attachment(
            b"attachment bytes",
            maintype="application",
            subtype="octet-stream",
            filename="agenda.bin",
        )

        with patch(
            "apps.records.vault_ingest._ocr_pdf",
            side_effect=lambda pdf: pdf,
        ):
            converted = convert_message_to_searchable_pdf(message.as_bytes())

        import pymupdf as fitz

        document = fitz.open(stream=converted.content, filetype="pdf")
        try:
            rendered_text = "\n".join(page.get_text() for page in document)
        finally:
            document.close()
        self.assertEqual(converted.filename, "Board packet.pdf")
        self.assertEqual(converted.content_type, "application/pdf")
        self.assertIn("searchable meeting agenda", converted.ocr_text)
        self.assertIn("Attachment: agenda.bin", converted.ocr_text)
        self.assertIn("Board packet", rendered_text)
        self.assertIn("searchable meeting agenda", rendered_text)
