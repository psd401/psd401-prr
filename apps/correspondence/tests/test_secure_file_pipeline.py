# ABOUTME: Red tests for one reusable object-store and ClamAV security boundary.
# ABOUTME: Correspondence files remain quarantined on infection or scan outage.
import datetime as dt
import hashlib
import importlib
from io import BytesIO
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceAttachmentQuarantineCleanup,
)
from apps.records.models import IntakeAttachment, PublicRecordsRequest, Requester

OBJECT_STORE = {
    "endpoint": "http://objects.test",
    "bucket": "prr-records",
    "access_key": "test",
    "secret_key": "test-secret",
    "region": "",
}


def _secure_files_module():
    return importlib.import_module("apps.secure_files")


@override_settings(OBJECT_STORE=OBJECT_STORE)
# TestCase, not SimpleTestCase: the object-store timeouts and retry ceiling
# are application-managed settings now, so building a client reads the store.
class GenericSecureFileTests(TestCase):
    @override_settings(
        PRR_OBJECT_STORE_CONNECT_TIMEOUT_SECONDS=7,
        PRR_OBJECT_STORE_READ_TIMEOUT_SECONDS=47,
        PRR_OBJECT_STORE_MAX_ATTEMPTS=4,
    )
    @patch("apps.secure_files.boto3.client")
    def test_object_store_client_has_bounded_timeouts_and_standard_retries(
        self,
        boto3_client,
    ):
        secure_files = _secure_files_module()

        secure_files._object_store_client()

        client_kwargs = boto3_client.call_args.kwargs
        client_config = client_kwargs["config"]
        self.assertEqual(client_config.connect_timeout, 7)
        self.assertEqual(client_config.read_timeout, 47)
        self.assertEqual(
            client_config.retries,
            {"mode": "standard", "total_max_attempts": 4},
        )
        self.assertEqual(client_kwargs["endpoint_url"], "http://objects.test")
        self.assertEqual(client_kwargs["aws_access_key_id"], "test")
        self.assertEqual(client_kwargs["aws_secret_access_key"], "test-secret")

    @override_settings(
        OBJECT_STORE={
            "endpoint": "",
            "bucket": "aws-records",
            "access_key": "",
            "secret_key": "",
            "region": "us-west-2",
        }
    )
    @patch("apps.secure_files.boto3.client")
    def test_object_store_client_uses_aws_default_credential_chain(
        self,
        boto3_client,
    ):
        secure_files = _secure_files_module()

        secure_files._object_store_client()

        client_kwargs = boto3_client.call_args.kwargs
        self.assertEqual(client_kwargs["service_name"], "s3")
        self.assertEqual(client_kwargs["region_name"], "us-west-2")
        self.assertNotIn("endpoint_url", client_kwargs)
        self.assertNotIn("aws_access_key_id", client_kwargs)
        self.assertNotIn("aws_secret_access_key", client_kwargs)

    @override_settings(
        OBJECT_STORE={
            "endpoint": "",
            "bucket": "aws-records",
            "access_key": "",
            "secret_key": "",
            "region": "us-west-2",
        }
    )
    @patch("apps.secure_files.boto3.client")
    def test_object_storage_probe_uses_aws_default_credential_chain(
        self,
        boto3_client,
    ):
        secure_files = _secure_files_module()
        client = boto3_client.return_value

        self.assertTrue(secure_files.probe_object_storage())

        client_kwargs = boto3_client.call_args.kwargs
        self.assertEqual(client_kwargs["service_name"], "s3")
        self.assertEqual(client_kwargs["region_name"], "us-west-2")
        self.assertNotIn("endpoint_url", client_kwargs)
        self.assertNotIn("aws_access_key_id", client_kwargs)
        self.assertNotIn("aws_secret_access_key", client_kwargs)
        client.get_bucket_versioning.assert_called_once_with(Bucket="aws-records")

    @patch("apps.secure_files.boto3.client")
    def test_object_store_clients_reject_custom_endpoint_without_complete_keys(
        self,
        boto3_client,
    ):
        secure_files = _secure_files_module()
        invalid_configs = (
            {
                "endpoint": "http://objects.test",
                "bucket": "prr-records",
                "access_key": "",
                "secret_key": "",
                "region": "",
            },
            {
                "endpoint": "",
                "bucket": "prr-records",
                "access_key": "only-access",
                "secret_key": "",
                "region": "",
            },
            {
                "endpoint": "",
                "bucket": "prr-records",
                "access_key": "",
                "secret_key": "only-secret",
                "region": "",
            },
        )

        for config in invalid_configs:
            for factory in (
                secure_files._object_store_client,
                secure_files.probe_object_storage,
            ):
                with self.subTest(config=config, factory=factory.__name__):
                    with override_settings(OBJECT_STORE=config):
                        with self.assertRaises(ImproperlyConfigured):
                            factory()

        boto3_client.assert_not_called()

    def test_quarantine_store_forces_octet_stream_and_never_uses_display_filename(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        source = BytesIO(b"hostile until scanned")
        source.seek(7)
        digest = hashlib.sha256(source.getvalue()).hexdigest()
        storage_key = "quarantine/correspondence/attachments/generated-id"

        with patch.object(secure_files, "_object_store_client", return_value=client):
            secure_files.store_quarantined_object(
                storage_key,
                source,
                content_length=len(source.getvalue()),
                sha256=digest,
            )

        stored = client.put_object.call_args.kwargs
        self.assertEqual(stored["Bucket"], "prr-records")
        self.assertEqual(stored["Key"], storage_key)
        self.assertIs(stored["Body"], source)
        self.assertEqual(stored["ContentLength"], len(source.getvalue()))
        self.assertEqual(stored["ContentType"], "application/octet-stream")
        self.assertEqual(stored["Metadata"]["sha256"], digest)
        self.assertEqual(stored["Metadata"]["scan-status"], "pending")
        self.assertNotIn("filename", str(stored).lower())
        self.assertEqual(source.tell(), 7)
        client.upload_fileobj.assert_not_called()

    def test_quarantine_store_uses_one_visible_put_for_a_zero_byte_object(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        source = BytesIO(b"")

        secure_files.store_quarantined_object(
            "quarantine/correspondence/attachments/empty",
            source,
            content_length=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            _client=client,
        )

        self.assertEqual(client.put_object.call_args.kwargs["ContentLength"], 0)
        client.upload_fileobj.assert_not_called()

    def test_quarantine_single_put_restores_cursor_when_storage_fails(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        source = BytesIO(b"bounded source")
        source.seek(4)

        def consume_then_fail(**kwargs):
            self.assertEqual(kwargs["Body"].tell(), 0)
            kwargs["Body"].read(3)
            raise RuntimeError("storage unavailable")

        client.put_object.side_effect = consume_then_fail
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            secure_files.store_quarantined_object(
                "quarantine/correspondence/attachments/failing",
                source,
                content_length=len(source.getvalue()),
                sha256=hashlib.sha256(source.getvalue()).hexdigest(),
                _client=client,
            )

        self.assertEqual(source.tell(), 4)
        client.upload_fileobj.assert_not_called()

    def test_scan_stream_has_a_caller_supplied_limit_and_verifies_size_and_digest(self):
        secure_files = _secure_files_module()
        payload = b"exact quarantined bytes"
        digest = hashlib.sha256(payload).hexdigest()
        body = MagicMock()
        body.iter_chunks.return_value = iter([payload[:5], payload[5:]])
        client = MagicMock()
        client.get_object.return_value = {"Body": body}

        def clean_scan(chunks):
            self.assertEqual(b"".join(chunks), payload)
            return True, "OK"

        with (
            patch.object(secure_files, "_object_store_client", return_value=client),
            patch.object(secure_files, "_clamav_scan", side_effect=clean_scan),
        ):
            verdict = secure_files.scan_object_for_malware(
                "quarantine/generated-id",
                expected_size=len(payload),
                expected_sha256=digest,
                max_bytes=len(payload),
            )

        self.assertEqual(verdict, (True, "OK"))
        body.close.assert_called_once()

    def test_scan_fails_closed_before_streaming_past_the_callers_limit(self):
        secure_files = _secure_files_module()
        body = MagicMock()
        body.iter_chunks.return_value = iter([b"12345", b"67890"])
        client = MagicMock()
        client.get_object.return_value = {"Body": body}

        def consume(chunks):
            list(chunks)
            return True, "OK"

        with (
            patch.object(secure_files, "_object_store_client", return_value=client),
            patch.object(secure_files, "_clamav_scan", side_effect=consume),
            self.assertRaises(secure_files.AttachmentScanUnavailable),
        ):
            secure_files.scan_object_for_malware(
                "quarantine/generated-id",
                expected_size=10,
                expected_sha256=hashlib.sha256(b"1234567890").hexdigest(),
                max_bytes=8,
            )

        body.close.assert_called_once()

    def test_integrity_mismatch_cannot_be_promoted_after_a_clean_clamav_result(self):
        secure_files = _secure_files_module()
        body = MagicMock()
        body.iter_chunks.return_value = iter([b"tampered"])
        client = MagicMock()
        client.get_object.return_value = {"Body": body}

        def clean_scan(chunks):
            list(chunks)
            return True, "OK"

        with (
            patch.object(secure_files, "_object_store_client", return_value=client),
            patch.object(secure_files, "_clamav_scan", side_effect=clean_scan),
            self.assertRaises(secure_files.AttachmentScanUnavailable),
        ):
            secure_files.scan_object_for_malware(
                "quarantine/generated-id",
                expected_size=len(b"expected"),
                expected_sha256=hashlib.sha256(b"expected").hexdigest(),
                max_bytes=1024,
            )

        body.close.assert_called_once()

    def test_clean_promotion_replaces_untrusted_metadata_then_deletes_quarantine(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        client.get_bucket_versioning.return_value = {}
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        with patch.object(secure_files, "_object_store_client", return_value=client):
            secure_files.promote_clean_object(
                "quarantine/correspondence/attachments/source",
                "correspondence/attachments/destination",
            )

        client.copy_object.assert_called_once_with(
            Bucket="prr-records",
            Key="correspondence/attachments/destination",
            CopySource={
                "Bucket": "prr-records",
                "Key": "quarantine/correspondence/attachments/source",
            },
            ContentType="application/octet-stream",
            Metadata={"scan-status": "clean"},
            MetadataDirective="REPLACE",
        )
        client.delete_object.assert_called_once_with(
            Bucket="prr-records",
            Key="quarantine/correspondence/attachments/source",
        )

    def test_clean_copy_replaces_untrusted_metadata_but_leaves_quarantine(self):
        secure_files = _secure_files_module()
        client = MagicMock()

        with patch.object(secure_files, "_object_store_client", return_value=client):
            secure_files.copy_clean_object(
                "quarantine/correspondence/attachments/source",
                "correspondence/attachments/destination",
            )

        client.copy_object.assert_called_once_with(
            Bucket="prr-records",
            Key="correspondence/attachments/destination",
            CopySource={
                "Bucket": "prr-records",
                "Key": "quarantine/correspondence/attachments/source",
            },
            ContentType="application/octet-stream",
            Metadata={"scan-status": "clean"},
            MetadataDirective="REPLACE",
        )
        client.delete_object.assert_not_called()

    def test_versioned_quarantine_delete_removes_versions_and_markers(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        paginator = MagicMock()
        paginator.paginate.side_effect = [
            iter(
                [
                    {
                        "Versions": [
                            {
                                "Key": (
                                    "quarantine/correspondence/attachments/source"
                                ),
                                "VersionId": "version-1",
                            }
                        ],
                        "DeleteMarkers": [
                            {
                                "Key": (
                                    "quarantine/correspondence/attachments/source"
                                ),
                                "VersionId": "marker-1",
                            }
                        ],
                    }
                ]
            ),
            iter([{"Versions": [], "DeleteMarkers": []}]),
        ]
        client.get_paginator.return_value = paginator

        secure_files.delete_quarantined_object(
            "quarantine/correspondence/attachments/source",
            _client=client,
        )

        self.assertCountEqual(
            client.delete_object.call_args_list,
            [
                (
                    (),
                    {
                        "Bucket": "prr-records",
                        "Key": (
                            "quarantine/correspondence/attachments/source"
                        ),
                        "VersionId": "version-1",
                    },
                ),
                (
                    (),
                    {
                        "Bucket": "prr-records",
                        "Key": (
                            "quarantine/correspondence/attachments/source"
                        ),
                        "VersionId": "marker-1",
                    },
                ),
            ],
        )
        self.assertEqual(paginator.paginate.call_count, 2)

    def test_unversioned_quarantine_delete_verifies_absence(self):
        secure_files = _secure_files_module()
        client = MagicMock()
        client.get_bucket_versioning.return_value = {}
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        secure_files.delete_quarantined_object(
            "quarantine/correspondence/attachments/source",
            _client=client,
        )

        client.delete_object.assert_called_once_with(
            Bucket="prr-records",
            Key="quarantine/correspondence/attachments/source",
        )
        client.head_object.assert_called_once()

    def test_quarantine_delete_rejects_keys_outside_generated_namespaces(self):
        secure_files = _secure_files_module()

        with self.assertRaises(ValueError):
            secure_files.delete_quarantined_object(
                "responsive-records/do-not-delete",
                _client=MagicMock(),
            )


@override_settings(OBJECT_STORE=OBJECT_STORE)
class IntakeAttachmentCompatibilityTests(TestCase):
    def test_clean_intake_attachment_without_quarantined_field_can_be_opened(self):
        requester = Requester.objects.create(email="requester@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records with a clean requester-supplied attachment.",
        )
        attachment = IntakeAttachment.objects.create(
            request=prr,
            original_filename="context.pdf",
            content_type="application/pdf",
            size=8,
            sha256="c" * 64,
            storage_key=f"intake/{prr.id}/generated-id",
            scan_status=IntakeAttachment.ScanStatus.CLEAN,
        )
        body = object()
        client = MagicMock()
        client.get_object.return_value = {"Body": body}

        with patch(
            "apps.records.attachments._object_store_client",
            return_value=client,
        ):
            from apps.records.attachments import open_clean_attachment

            opened = open_clean_attachment(attachment)

        self.assertFalse(hasattr(attachment, "quarantined"))
        self.assertIs(opened, body)


@override_settings(OBJECT_STORE=OBJECT_STORE)
class CorrespondenceAttachmentScanTaskTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            "inbound-transport",
            password=None,
        )
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records concerning the transportation plan.",
        )
        self.correspondence = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Attachment scan",
            draft_body="Body",
            created_by=self.actor,
        )
        payload = b"attachment evidence"
        self.attachment = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="evidence.pdf",
            content_type="application/pdf",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            uploaded_by=self.actor,
        )

    def _task(self):
        return importlib.import_module(
            "apps.correspondence.tasks"
        ).scan_correspondence_attachment

    def test_scan_task_is_redelivered_after_worker_process_loss(self):
        task = self._task()

        self.assertTrue(task.acks_late)
        self.assertTrue(task.reject_on_worker_lost)
        self.assertFalse(task.acks_on_failure_or_timeout)
        self.assertIsNone(task.max_retries)

    def test_delayed_scanner_cannot_reverse_explicit_requarantine(self):
        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )
        attachment = CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=source_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=promoted_key,
        )
        security_reason = (
            "Clean release was revoked after a later security review."
        )
        attachment = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).requarantine_locked(
            quarantine_reason=security_reason,
            quarantined_at=timezone.now(),
        )

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ) as scan,
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
        ):
            result = self._task().run(str(attachment.pk))

        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        scan.assert_not_called()
        copy.assert_not_called()
        attachment.refresh_from_db()
        self.assertEqual(
            attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.assertTrue(attachment.quarantined)
        self.assertEqual(attachment.storage_key, promoted_key)
        self.assertEqual(attachment.quarantine_reason, security_reason)

    def test_positive_duplicate_scan_revokes_competing_clean_release(self):
        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )

        def competing_clean_then_positive(*_args, **_kwargs):
            CorrespondenceAttachment.objects.filter(
                pk=self.attachment.pk
            ).finalize_locked_scan(
                expected_storage_key=source_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=promoted_key,
            )
            CorrespondenceAttachmentQuarantineCleanup.objects.create(
                attachment_id=self.attachment.pk,
                source_storage_key=source_key,
            )
            return False, "Eicar-Test-Signature FOUND"

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=competing_clean_then_positive,
            ),
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay"
            ),
        ):
            result = self._task().run(str(self.attachment.pk))

        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.attachment.refresh_from_db()
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertTrue(self.attachment.quarantined)
        self.assertFalse(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.quarantine_reason,
            "Malware detected.",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

    def test_positive_verdict_survives_audit_failure_and_fresh_retry(self):
        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )

        def competing_clean_then_positive(*_args, **_kwargs):
            CorrespondenceAttachment.objects.filter(
                pk=self.attachment.pk
            ).finalize_locked_scan(
                expected_storage_key=source_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=promoted_key,
            )
            CorrespondenceAttachmentQuarantineCleanup.objects.create(
                attachment_id=self.attachment.pk,
                source_storage_key=source_key,
            )
            return False, "Eicar-Test-Signature FOUND"

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=competing_clean_then_positive,
            ),
            patch(
                "apps.correspondence.tasks._audit_scan",
                side_effect=RuntimeError("audit temporarily unavailable"),
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(str(self.attachment.pk))

        retry.assert_called_once()
        self.assertTrue(
            retry.call_args.kwargs["kwargs"]["confirmed_positive"]
        )
        self.attachment.refresh_from_db()
        self.assertFalse(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )
        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.get(
            attachment=self.attachment
        )
        self.assertEqual(cleanup.source_storage_key, source_key)

        result = task.run(
            str(self.attachment.pk),
            confirmed_positive=True,
        )

        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )
        self.assertEqual(
            CorrespondenceAttachmentQuarantineCleanup.objects.filter(
                attachment=self.attachment
            ).count(),
            1,
        )

    def test_worker_loss_before_positive_barrier_rescans_competing_clean(self):
        class SimulatedWorkerLoss(BaseException):
            pass

        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )

        def competing_clean_then_positive(*_args, **_kwargs):
            CorrespondenceAttachment.objects.filter(
                pk=self.attachment.pk
            ).finalize_locked_scan(
                expected_storage_key=source_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=promoted_key,
            )
            CorrespondenceAttachmentQuarantineCleanup.objects.create(
                attachment_id=self.attachment.pk,
                source_storage_key=source_key,
            )
            return False, "Eicar-Test-Signature FOUND"

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=competing_clean_then_positive,
            ),
            patch(
                "apps.correspondence.tasks._persist_positive_safety_barrier",
                side_effect=SimulatedWorkerLoss,
            ),
            self.assertRaises(SimulatedWorkerLoss),
        ):
            task.run(str(self.attachment.pk))

        self.attachment.refresh_from_db()
        self.assertTrue(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.CLEAN,
        )

        with patch(
            "apps.correspondence.tasks.secure_files.scan_object_for_malware",
            return_value=(False, "Eicar-Test-Signature FOUND"),
        ) as rescan:
            result = task.run(str(self.attachment.pk))

        rescan.assert_called_once()
        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.attachment.refresh_from_db()
        self.assertFalse(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.get(
            attachment=self.attachment
        )
        self.assertEqual(cleanup.source_storage_key, source_key)

    def test_worker_loss_after_positive_barrier_repairs_missing_audit(self):
        class SimulatedWorkerLoss(BaseException):
            pass

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(False, "Eicar-Test-Signature FOUND"),
            ),
            patch(
                "apps.correspondence.tasks._finalize_infected_with_audit",
                side_effect=SimulatedWorkerLoss,
            ),
            self.assertRaises(SimulatedWorkerLoss),
        ):
            task.run(str(self.attachment.pk))

        self.attachment.refresh_from_db()
        self.assertFalse(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

        with patch(
            "apps.correspondence.tasks.secure_files.scan_object_for_malware"
        ) as scan:
            result = task.run(str(self.attachment.pk))

        scan.assert_not_called()
        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

    def test_released_clean_revalidation_outage_revokes_release_with_audit(self):
        secure_files = _secure_files_module()
        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )
        CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=source_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=promoted_key,
        )
        CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment_id=self.attachment.pk,
            source_storage_key=source_key,
        )

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=secure_files.AttachmentScanUnavailable(
                    "revalidation scanner unavailable"
                ),
            ),
            patch.object(task, "retry") as retry,
        ):
            result = task.run(str(self.attachment.pk))

        retry.assert_not_called()
        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.attachment.refresh_from_db()
        self.assertFalse(self.attachment.is_downloadable)
        self.assertTrue(self.attachment.quarantined)
        self.assertEqual(
            self.attachment.quarantine_reason,
            "Clean attachment revalidation failed; release was revoked.",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action=(
                    "correspondence_attachment."
                    "clean_revalidation_failed"
                ),
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

    def test_confirmed_positive_lookup_failure_persists_barrier_and_retries(self):
        source_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.pk}/"
            f"{self.attachment.pk}"
        )
        CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=source_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=promoted_key,
        )
        CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment_id=self.attachment.pk,
            source_storage_key=source_key,
        )

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks._positive_verdict_source_key",
                side_effect=DatabaseError("cleanup lookup unavailable"),
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(
                str(self.attachment.pk),
                confirmed_positive=True,
            )

        retry.assert_called_once()
        self.assertTrue(
            retry.call_args.kwargs["kwargs"]["confirmed_positive"]
        )
        self.attachment.refresh_from_db()
        self.assertFalse(self.attachment.is_downloadable)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )

    def test_infected_audit_lookup_failure_retries_durable_reconciliation(self):
        self.attachment = CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_infected_verdict_locked(
            expected_size=self.attachment.size,
            expected_sha256=self.attachment.sha256,
            scanned_at=timezone.now(),
            quarantine_reason="Malware detected.",
        )
        CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=self.attachment,
            source_storage_key=self.attachment.storage_key,
        )

        task = self._task()
        with (
            patch(
                "apps.correspondence.tasks._infected_audit_exists",
                side_effect=DatabaseError("audit lookup unavailable"),
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(str(self.attachment.pk))

        retry.assert_called_once()
        self.assertTrue(
            retry.call_args.kwargs["kwargs"]["confirmed_positive"]
        )
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

        result = task.run(str(self.attachment.pk))

        self.assertEqual(
            result,
            CorrespondenceAttachment.ScanStatus.INFECTED,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence_attachment.scan_infected",
                detail__attachment_id=str(self.attachment.pk),
            ).exists()
        )

    def test_eicar_verdict_durably_records_cleanup_before_enqueue(self):
        task = self._task()
        original_key = self.attachment.storage_key

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(False, "Eicar-Test-Signature FOUND"),
            ),
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay"
            ) as enqueue_cleanup,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                task.run(str(self.attachment.id))

            self.attachment.refresh_from_db()
            self.assertEqual(
                self.attachment.scan_status,
                CorrespondenceAttachment.ScanStatus.INFECTED,
            )
            self.assertTrue(self.attachment.quarantined)
            self.assertTrue(self.attachment.scanned_at)
            self.assertEqual(self.attachment.quarantine_reason, "Malware detected.")
            self.assertNotIn(
                "Eicar-Test-Signature",
                self.attachment.quarantine_reason,
            )
            self.assertTrue(
                AuditLog.objects.filter(
                    action="correspondence_attachment.scan_infected",
                    detail__attachment_id=str(self.attachment.id),
                ).exists()
            )
            cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.get(
                attachment=self.attachment
            )
            self.assertEqual(cleanup.source_storage_key, original_key)
            self.assertIsNone(cleanup.completed_at)
            self.assertEqual(cleanup.attempt_count, 0)
            enqueue_cleanup.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            cleanup.refresh_from_db()
            self.assertIsNotNone(cleanup.lease_token)
            self.assertIsNotNone(cleanup.lease_expires_at)
            enqueue_cleanup.assert_called_once_with(
                str(cleanup.id),
                str(cleanup.lease_token),
            )

    def test_scan_outage_stays_quarantined_records_only_a_sanitized_error_and_retries(self):
        secure_files = _secure_files_module()
        task = self._task()
        original_key = self.attachment.storage_key

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=secure_files.AttachmentScanUnavailable(
                    "private scanner diagnostic"
                ),
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.assertTrue(self.attachment.quarantined)
        self.assertEqual(self.attachment.storage_key, original_key)
        self.assertEqual(
            self.attachment.quarantine_reason,
            "Malware scanning is temporarily unavailable.",
        )
        self.assertNotIn(
            "private scanner diagnostic",
            self.attachment.quarantine_reason,
        )
        retry.assert_called_once()

    def test_clean_scan_promotes_to_a_private_nonquarantine_key_before_release(self):
        task = self._task()
        original_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.id}/"
            f"{self.attachment.id}"
        )

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ),
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay"
            ) as enqueue_cleanup,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                task.run(str(self.attachment.id))

            self.attachment.refresh_from_db()
            self.assertEqual(
                self.attachment.scan_status,
                CorrespondenceAttachment.ScanStatus.CLEAN,
            )
            self.assertFalse(self.attachment.quarantined)
            self.assertTrue(self.attachment.scanned_at)
            self.assertEqual(self.attachment.storage_key, promoted_key)
            self.assertTrue(
                AuditLog.objects.filter(
                    action="correspondence_attachment.scan_clean",
                    detail__attachment_id=str(self.attachment.id),
                ).exists()
            )
            copy.assert_called_once_with(original_key, promoted_key)
            cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.get(
                attachment=self.attachment
            )
            self.assertEqual(cleanup.source_storage_key, original_key)
            self.assertIsNone(cleanup.completed_at)
            enqueue_cleanup.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            cleanup.refresh_from_db()
            enqueue_cleanup.assert_called_once_with(
                str(cleanup.id),
                str(cleanup.lease_token),
            )

    def test_cleanup_failure_is_durable_and_a_restart_completes_it(self):
        scan_task = self._task()
        original_key = self.attachment.storage_key

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(False, "Eicar-Test-Signature FOUND"),
            ),
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay"
            ) as enqueue_cleanup,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                scan_task.run(str(self.attachment.id))

        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.get(
            attachment=self.attachment
        )
        first_lease_token = enqueue_cleanup.call_args.args[1]
        cleanup_task = importlib.import_module(
            "apps.correspondence.tasks"
        ).cleanup_correspondence_attachment_quarantine
        secure_files = _secure_files_module()

        with (
            patch(
                "apps.correspondence.tasks.secure_files.delete_quarantined_object",
                side_effect=RuntimeError("object store unavailable"),
            ) as delete,
            self.assertRaisesRegex(
                secure_files.AttachmentScanUnavailable,
                "Quarantine cleanup is temporarily unavailable",
            ),
        ):
            cleanup_task.run(str(cleanup.id), first_lease_token)

        cleanup.refresh_from_db()
        self.assertEqual(cleanup.attempt_count, 1)
        self.assertIsNotNone(cleanup.last_attempt_at)
        self.assertIsNone(cleanup.completed_at)
        self.assertIsNone(cleanup.lease_token)
        self.assertIsNone(cleanup.lease_expires_at)
        self.assertGreater(cleanup.next_attempt_at, cleanup.last_attempt_at)

        restart_at = cleanup.next_attempt_at + dt.timedelta(seconds=1)
        with (
            patch(
                "apps.correspondence.tasks.timezone.now",
                return_value=restart_at,
            ),
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay"
            ) as enqueue_after_restart,
        ):
            queued = importlib.import_module(
                "apps.correspondence.tasks"
            ).requeue_pending_correspondence_attachment_cleanups(limit=1)
        self.assertEqual(queued, 1)
        second_lease_token = enqueue_after_restart.call_args.args[1]

        with patch(
            "apps.correspondence.tasks.secure_files.delete_quarantined_object"
        ) as delete_after_restart:
            result = cleanup_task.run(str(cleanup.id), second_lease_token)

        cleanup.refresh_from_db()
        self.assertEqual(result, "deleted")
        self.assertEqual(cleanup.attempt_count, 2)
        self.assertIsNotNone(cleanup.completed_at)
        delete.assert_called_once_with(original_key)
        delete_after_restart.assert_called_once_with(original_key)
        cleanup_audit = AuditLog.objects.get(
            action="correspondence_attachment.quarantine_deleted",
            detail__attachment_id=str(self.attachment.id),
        )
        self.assertNotIn(original_key, str(cleanup_audit.detail))

    @patch(
        "apps.correspondence.tasks."
        "cleanup_correspondence_attachment_quarantine.delay"
    )
    def test_pending_cleanup_recovery_claims_work_without_storm_or_starvation(
        self,
        cleanup_delay,
    ):
        tasks = importlib.import_module("apps.correspondence.tasks")
        first = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=self.attachment,
            source_storage_key=self.attachment.storage_key,
        )
        second_attachment = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="second.pdf",
            content_type="application/pdf",
            size=1,
            sha256="d" * 64,
            uploaded_by=self.actor,
        )
        second = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=second_attachment,
            source_storage_key=second_attachment.storage_key,
        )
        third_attachment = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="third.pdf",
            content_type="application/pdf",
            size=1,
            sha256="f" * 64,
            uploaded_by=self.actor,
        )
        third = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=third_attachment,
            source_storage_key=third_attachment.storage_key,
        )
        completed_attachment = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="completed.pdf",
            content_type="application/pdf",
            size=1,
            sha256="e" * 64,
            uploaded_by=self.actor,
        )
        completed = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=completed_attachment,
            source_storage_key=completed_attachment.storage_key,
        )
        claimed_at = timezone.now()
        completed_claim = (
            CorrespondenceAttachmentQuarantineCleanup.objects.claim_due(
                cleanup_id=completed.pk,
                now=claimed_at,
                lease_expires_at=claimed_at + dt.timedelta(minutes=15),
            )
        )
        CorrespondenceAttachmentQuarantineCleanup.objects.complete_locked(
            cleanup_id=completed.pk,
            lease_token=completed_claim.lease_token,
            completed_at=claimed_at,
        )

        queued = tasks.requeue_pending_correspondence_attachment_cleanups(limit=2)

        self.assertEqual(queued, 2)
        self.assertEqual(cleanup_delay.call_count, 2)
        queued_ids = {call.args[0] for call in cleanup_delay.call_args_list}
        self.assertEqual(queued_ids, {str(first.id), str(second.id)})
        self.assertNotIn(str(completed.id), queued_ids)
        self.assertEqual(
            tasks.requeue_pending_correspondence_attachment_cleanups(limit=2),
            1,
        )
        self.assertEqual(cleanup_delay.call_args.args[0], str(third.id))
        self.assertEqual(
            tasks.requeue_pending_correspondence_attachment_cleanups(limit=2),
            0,
        )

    def test_cleanup_ledger_protects_attachment_and_rejects_ordinary_mutation(
        self,
    ):
        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=self.attachment,
            source_storage_key=self.attachment.storage_key,
        )

        with self.assertRaises(ProtectedError):
            self.attachment.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachmentQuarantineCleanup.objects.filter(
                pk=cleanup.pk
            ).update(
                source_storage_key="quarantine/correspondence/attachments/retargeted"
            )
        with self.assertRaises(ValidationError):
            CorrespondenceAttachmentQuarantineCleanup.objects.filter(
                pk=cleanup.pk
            ).delete()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachmentQuarantineCleanup.objects.bulk_create([])

    def test_cleanup_key_namespace_is_enforced_by_the_database(self):
        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=self.attachment,
            source_storage_key=self.attachment.storage_key,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuerySet.update(
                CorrespondenceAttachmentQuarantineCleanup.objects.filter(
                    pk=cleanup.pk
                ),
                source_storage_key="responsive-records/do-not-delete",
            )

    def test_broker_outage_releases_claim_with_due_time_backoff(self):
        tasks = importlib.import_module("apps.correspondence.tasks")
        cleanup = CorrespondenceAttachmentQuarantineCleanup.objects.create(
            attachment=self.attachment,
            source_storage_key=self.attachment.storage_key,
        )
        claimed_at = timezone.now() + dt.timedelta(seconds=1)
        failed_at = claimed_at + dt.timedelta(seconds=90)

        with (
            patch(
                "apps.correspondence.tasks.timezone.now",
                side_effect=[claimed_at, failed_at, failed_at],
            ),
            patch(
                "apps.correspondence.tasks."
                "cleanup_correspondence_attachment_quarantine.delay",
                side_effect=RuntimeError("broker unavailable"),
            ),
        ):
            first = (
                tasks.requeue_pending_correspondence_attachment_cleanups(
                    limit=1
                )
            )
            second = (
                tasks.requeue_pending_correspondence_attachment_cleanups(
                    limit=1
                )
            )

        cleanup.refresh_from_db()
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIsNone(cleanup.lease_token)
        self.assertIsNone(cleanup.lease_expires_at)
        self.assertEqual(
            cleanup.next_attempt_at,
            failed_at + dt.timedelta(seconds=60),
        )

    def test_a_retry_can_promote_an_attachment_after_a_sanitized_error(self):
        task = self._task()
        original_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.id}/"
            f"{self.attachment.id}"
        )
        self.attachment = CorrespondenceAttachment.objects.filter(
            pk=self.attachment.id
        ).finalize_locked_scan(
            expected_storage_key=original_key,
            scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
            scanned_at=timezone.now(),
            quarantine_reason=(
                "Malware scanning is temporarily unavailable."
            ),
        )

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ),
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
        ):
            result = task.run(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(result, CorrespondenceAttachment.ScanStatus.CLEAN)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.CLEAN,
        )
        self.assertEqual(self.attachment.storage_key, promoted_key)
        copy.assert_called_once_with(original_key, promoted_key)

    def test_released_clean_outcome_is_revalidated_without_recopying(self):
        task = self._task()
        original_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.id}/"
            f"{self.attachment.id}"
        )
        CorrespondenceAttachment.objects.filter(
            pk=self.attachment.id
        ).finalize_locked_scan(
            expected_storage_key=original_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=promoted_key,
        )

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ) as scan,
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
        ):
            result = task.run(str(self.attachment.id))

        self.assertEqual(result, CorrespondenceAttachment.ScanStatus.CLEAN)
        scan.assert_called_once()
        copy.assert_not_called()

    def test_stale_failed_worker_cannot_overwrite_another_workers_final_verdict(self):
        secure_files = _secure_files_module()
        task = self._task()
        original_key = self.attachment.storage_key
        promoted_key = (
            f"correspondence/attachments/{self.correspondence.id}/"
            f"{self.attachment.id}"
        )

        def concurrent_clean_verdict(*_args, **_kwargs):
            CorrespondenceAttachment.objects.filter(
                pk=self.attachment.id
            ).finalize_locked_scan(
                expected_storage_key=original_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=promoted_key,
            )
            raise secure_files.AttachmentScanUnavailable(
                "stale worker lost its scanner connection"
            )

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                side_effect=concurrent_clean_verdict,
            ),
            patch.object(task, "retry") as retry,
        ):
            result = task.run(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(result, CorrespondenceAttachment.ScanStatus.CLEAN)
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.CLEAN,
        )
        self.assertFalse(self.attachment.quarantined)
        self.assertEqual(self.attachment.storage_key, promoted_key)
        retry.assert_not_called()

    def test_clean_audit_failure_rolls_back_release_and_keeps_quarantine_source(self):
        task = self._task()
        original_key = self.attachment.storage_key

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ),
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
            patch(
                "apps.correspondence.tasks.secure_files.delete_quarantined_object"
            ) as delete,
            patch(
                "apps.correspondence.tasks._audit_scan",
                side_effect=[DatabaseError("audit unavailable"), None],
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ),
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.assertTrue(self.attachment.quarantined)
        self.assertEqual(self.attachment.storage_key, original_key)
        copy.assert_called_once()
        delete.assert_not_called()

    def test_clean_database_failure_keeps_quarantine_source_retryable(self):
        task = self._task()
        original_key = self.attachment.storage_key

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ),
            patch(
                "apps.correspondence.tasks.secure_files.copy_clean_object"
            ) as copy,
            patch(
                "apps.correspondence.tasks.secure_files.delete_quarantined_object"
            ) as delete,
            patch(
                "apps.correspondence.models."
                "CorrespondenceAttachmentQuerySet.finalize_locked_scan",
                side_effect=DatabaseError("database unavailable"),
            ),
            patch.object(
                task,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ),
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            task.run(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(
            self.attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.PENDING,
        )
        self.assertTrue(self.attachment.quarantined)
        self.assertEqual(self.attachment.storage_key, original_key)
        copy.assert_called_once()
        delete.assert_not_called()

    @override_settings(PRR_CORRESPONDENCE_ATTACHMENT_SCAN_STALE_SECONDS=60)
    @patch("apps.correspondence.tasks.scan_correspondence_attachment.delay")
    def test_stale_pending_and_retryable_error_recovery_is_bounded(
        self,
        scan_delay,
    ):
        tasks = importlib.import_module("apps.correspondence.tasks")
        retryable_error = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="retryable.pdf",
            content_type="application/pdf",
            size=1,
            sha256="a" * 64,
            uploaded_by=self.actor,
        )
        CorrespondenceAttachment.objects.filter(
            pk=retryable_error.id
        ).update(
            created_at=timezone.now() - dt.timedelta(minutes=3),
        )
        retryable_error.refresh_from_db()
        retryable_error = CorrespondenceAttachment.objects.filter(
            pk=retryable_error.id
        ).finalize_locked_scan(
            expected_storage_key=retryable_error.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
            scanned_at=timezone.now() - dt.timedelta(minutes=2),
            quarantine_reason="Malware scanning is temporarily unavailable.",
        )
        fresh_pending = CorrespondenceAttachment.objects.create(
            correspondence=self.correspondence,
            original_filename="fresh.pdf",
            content_type="application/pdf",
            size=1,
            sha256="b" * 64,
            uploaded_by=self.actor,
        )
        CorrespondenceAttachment.objects.filter(pk=self.attachment.id).update(
            created_at=timezone.now() - dt.timedelta(minutes=2)
        )

        queued = tasks.requeue_stale_correspondence_attachment_scans(limit=2)

        self.assertEqual(queued, 2)
        self.assertEqual(scan_delay.call_count, 2)
        queued_ids = {call.args[0] for call in scan_delay.call_args_list}
        self.assertEqual(
            queued_ids,
            {str(self.attachment.id), str(retryable_error.id)},
        )
        self.assertNotIn(str(fresh_pending.id), queued_ids)

    @patch("apps.correspondence.tasks.scan_correspondence_attachment.delay")
    def test_stale_recovery_queues_terminal_rows_only_while_audit_is_missing(
        self,
        scan_delay,
    ):
        tasks = importlib.import_module("apps.correspondence.tasks")

        def new_attachment(filename):
            return CorrespondenceAttachment.objects.create(
                correspondence=self.correspondence,
                original_filename=filename,
                content_type="application/pdf",
                size=1,
                sha256=hashlib.sha256(filename.encode()).hexdigest(),
                uploaded_by=self.actor,
            )

        infected_missing = CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_infected_verdict_locked(
            expected_size=self.attachment.size,
            expected_sha256=self.attachment.sha256,
            scanned_at=timezone.now(),
            quarantine_reason="Malware detected.",
        )
        infected_audited = new_attachment("infected-audited.pdf")
        infected_audited = CorrespondenceAttachment.objects.filter(
            pk=infected_audited.pk
        ).finalize_infected_verdict_locked(
            expected_size=infected_audited.size,
            expected_sha256=infected_audited.sha256,
            scanned_at=timezone.now(),
            quarantine_reason="Malware detected.",
        )
        AuditLog.objects.create(
            action="correspondence_attachment.scan_infected",
            detail={"attachment_id": str(infected_audited.pk)},
        )

        revalidation_missing = new_attachment("revalidation-missing.pdf")
        revalidation_source = revalidation_missing.storage_key
        revalidation_missing = CorrespondenceAttachment.objects.filter(
            pk=revalidation_missing.pk
        ).finalize_locked_scan(
            expected_storage_key=revalidation_source,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{self.correspondence.pk}/"
                f"{revalidation_missing.pk}"
            ),
        )
        revalidation_missing = CorrespondenceAttachment.objects.filter(
            pk=revalidation_missing.pk
        ).requarantine_locked(
            quarantine_reason=(
                "Clean attachment revalidation failed; release was revoked."
            ),
            quarantined_at=timezone.now(),
        )
        revalidation_audited = new_attachment("revalidation-audited.pdf")
        revalidation_audited_source = revalidation_audited.storage_key
        revalidation_audited = CorrespondenceAttachment.objects.filter(
            pk=revalidation_audited.pk
        ).finalize_locked_scan(
            expected_storage_key=revalidation_audited_source,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{self.correspondence.pk}/"
                f"{revalidation_audited.pk}"
            ),
        )
        revalidation_audited = CorrespondenceAttachment.objects.filter(
            pk=revalidation_audited.pk
        ).requarantine_locked(
            quarantine_reason=(
                "Clean attachment revalidation failed; release was revoked."
            ),
            quarantined_at=timezone.now(),
        )
        AuditLog.objects.create(
            action=(
                "correspondence_attachment.clean_revalidation_failed"
            ),
            detail={"attachment_id": str(revalidation_audited.pk)},
        )

        queued = tasks.requeue_stale_correspondence_attachment_scans(
            limit=100
        )

        self.assertEqual(queued, 2)
        self.assertEqual(
            {call.args[0] for call in scan_delay.call_args_list},
            {
                str(infected_missing.pk),
                str(revalidation_missing.pk),
            },
        )

    def test_celery_beat_runs_stale_attachment_scan_recovery(self):
        from config.celery import app

        entry = app.conf.beat_schedule[
            "recover-stale-correspondence-attachment-scans"
        ]
        self.assertEqual(
            entry["task"],
            "apps.correspondence.tasks."
            "requeue_stale_correspondence_attachment_scans",
        )

    def test_celery_beat_runs_pending_quarantine_cleanup_recovery(self):
        from config.celery import app

        entry = app.conf.beat_schedule[
            "recover-pending-correspondence-attachment-cleanups"
        ]
        self.assertEqual(
            entry["task"],
            "apps.correspondence.tasks."
            "requeue_pending_correspondence_attachment_cleanups",
        )
