# ABOUTME: Proves requester-supplied files stay quarantined until ClamAV clears
# ABOUTME: them and that only clean attachments receive short-lived downloads.
import struct
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.records.models import IntakeAttachment, PublicRecordsRequest, Requester
from apps.records.tasks import scan_intake_attachment


# TestCase, not SimpleTestCase: the attachment limits are application-managed
# settings now, so validating an upload reads the settings store.
class IntakeAttachmentValidationTests(TestCase):
    @staticmethod
    def _ooxml_manifest(main_part, main_content_type):
        return (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
            "content-types'>"
            f"<Override PartName='/{main_part}' "
            f"ContentType='{main_content_type}'/>"
            "</Types>"
        )

    def test_accepts_supported_content_signatures(self):
        from apps.records.attachments import validate_attachments

        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy office bytes"
        cases = (
            ("context.pdf", "application/pdf", b"%PDF-1.7\ncontext"),
            ("context.rtf", "application/rtf", b"{\\rtf1 context}"),
            ("context.doc", "application/msword", ole),
            ("context.xls", "application/vnd.ms-excel", ole),
            ("context.ppt", "application/vnd.ms-powerpoint", ole),
            ("context.gif", "image/gif", b"GIF89a" + b"\x00" * 20),
            ("context.jpg", "image/jpeg", b"\xff\xd8\xff\xe0jpeg"),
            ("context.png", "image/png", b"\x89PNG\r\n\x1a\ncontent"),
            ("context.tiff", "image/tiff", b"II*\x00content"),
            (
                "context.eml",
                "message/rfc822",
                b"From: requester@example.org\r\nSubject: Context\r\n\r\nMessage",
            ),
            ("context.csv", "text/csv", b"name,value\nexample,1\n"),
            ("context.txt", "text/plain", b"Requester-supplied context\n"),
        )

        for filename, content_type, content in cases:
            with self.subTest(content_type=content_type):
                upload = SimpleUploadedFile(
                    filename, content, content_type=content_type
                )
                self.assertEqual(len(validate_attachments([upload])), 1)

    def test_accepts_each_ooxml_family_when_archive_matches_declared_type(self):
        from apps.records.attachments import validate_attachments

        content_types = {
            (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ): (
                "word/document.xml",
                (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml"
                ),
            ),
            (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ): (
                "xl/workbook.xml",
                (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet.main+xml"
                ),
            ),
            (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ): (
                "ppt/presentation.xml",
                (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation.main+xml"
                ),
            ),
        }
        for content_type, (family_entry, main_content_type) in content_types.items():
            archive_bytes = BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    self._ooxml_manifest(
                        family_entry,
                        main_content_type,
                    ),
                )
                archive.writestr(family_entry, "<document />")
            upload = SimpleUploadedFile(
                "context.zip", archive_bytes.getvalue(), content_type=content_type
            )

            with self.subTest(content_type=content_type):
                self.assertEqual(len(validate_attachments([upload])), 1)

    def test_accepts_ooxml_with_explicit_empty_directory_entries(self):
        from apps.records.attachments import validate_attachments

        archive_bytes = BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("word/", b"")
            archive.writestr(
                "[Content_Types].xml",
                self._ooxml_manifest(
                    "word/document.xml",
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml",
                ),
            )
            archive.writestr("word/document.xml", "<document />")
        upload = SimpleUploadedFile(
            "context.docx",
            archive_bytes.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        self.assertEqual(len(validate_attachments([upload])), 1)

    def test_rejects_ooxml_archive_from_a_different_declared_family(self):
        from apps.records.attachments import (
            AttachmentValidationError,
            validate_attachments,
        )

        archive_bytes = BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/document.xml", "<document />")
        upload = SimpleUploadedFile(
            "disguised.xlsx",
            archive_bytes.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

        with self.assertRaisesRegex(AttachmentValidationError, "does not match"):
            validate_attachments([upload])

    def test_rejects_arbitrary_zip_with_only_an_ooxml_directory_prefix(self):
        from apps.records.attachments import (
            AttachmentValidationError,
            validate_attachments,
        )

        archive_bytes = BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/document.xml", "<not-an-ooxml-package />")
        upload = SimpleUploadedFile(
            "spoofed.docx",
            archive_bytes.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        with self.assertRaisesRegex(AttachmentValidationError, "does not match"):
            validate_attachments([upload])

    def test_zip_entry_count_is_rejected_before_directory_materialization(self):
        from apps.records.attachments import (
            AttachmentValidationError,
            validate_attachments,
        )

        local_file_prefix = b"PK\x03\x04" + (b"\x00" * 26)
        eocd = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            10_001,
            10_001,
            0,
            len(local_file_prefix),
            0,
        )
        upload = SimpleUploadedFile(
            "entry-bomb.docx",
            local_file_prefix + eocd,
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        with (
            patch(
                "apps.records.attachments.zipfile.ZipFile",
                side_effect=AssertionError(
                    "ZIP directory was materialized"
                ),
            ),
            self.assertRaisesRegex(
                AttachmentValidationError,
                "does not match",
            ),
        ):
            validate_attachments([upload])

    def test_rejects_ooxml_with_excessive_expanded_size_or_ratio(self):
        from apps.records.attachments import (
            AttachmentValidationError,
            validate_attachments,
        )

        archive_bytes = BytesIO()
        with zipfile.ZipFile(
            archive_bytes,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "[Content_Types].xml",
                self._ooxml_manifest(
                    "word/document.xml",
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml",
                ),
            )
            archive.writestr("word/document.xml", b"\x00" * (2 * 1024 * 1024))
        upload = SimpleUploadedFile(
            "expanded-bomb.docx",
            archive_bytes.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        with self.assertRaisesRegex(AttachmentValidationError, "does not match"):
            validate_attachments([upload])

    def test_ooxml_expanded_size_accepts_limit_and_rejects_limit_plus_one(self):
        from apps.records.attachments import _ooxml_content_type

        manifest = self._ooxml_manifest(
            "word/document.xml",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml",
        )
        document = b"x" * 512
        archive_bytes = BytesIO()
        with zipfile.ZipFile(
            archive_bytes,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            archive.writestr("[Content_Types].xml", manifest)
            archive.writestr("word/document.xml", document)
        upload = SimpleUploadedFile(
            "bounded.docx",
            archive_bytes.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        exact_total = len(manifest.encode()) + len(document)

        with override_settings(
            PRR_INTAKE_ATTACHMENT_MAX_BYTES=len(document),
            PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=exact_total,
        ):
            self.assertEqual(
                _ooxml_content_type(upload),
                (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
            )
        with override_settings(
            PRR_INTAKE_ATTACHMENT_MAX_BYTES=len(document) - 1,
            PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=exact_total,
        ):
            self.assertIsNone(_ooxml_content_type(upload))
        with override_settings(
            PRR_INTAKE_ATTACHMENT_MAX_BYTES=len(document),
            PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=exact_total - 1,
        ):
            self.assertIsNone(_ooxml_content_type(upload))

    def test_rejects_control_characters_in_requester_filename(self):
        from apps.records.attachments import (
            AttachmentValidationError,
            validate_attachments,
        )

        upload = SimpleUploadedFile(
            "context.pdf", b"%PDF-1.7\ncontext", content_type="application/pdf"
        )
        upload.name = "context\r\nX-Injected-Header.pdf"

        with self.assertRaisesRegex(AttachmentValidationError, "filename"):
            validate_attachments([upload])

    def test_compose_stacks_give_clamav_a_healthcheck(self):
        for filename in ("docker-compose.yml", "docker-compose.deploy.yml"):
            lines = (Path(settings.BASE_DIR) / filename).read_text().splitlines()
            start = lines.index("  clamav:") + 1
            end = next(
                (
                    index
                    for index in range(start, len(lines))
                    if lines[index].startswith("  ")
                    and not lines[index].startswith("    ")
                    and lines[index].strip()
                ),
                len(lines),
            )
            clamav_service = "\n".join(lines[start:end])

            with self.subTest(filename=filename):
                self.assertIn("healthcheck:", clamav_service)
                self.assertIn('test: ["CMD", "clamdcheck.sh"]', clamav_service)


class IntakeAttachmentUploadBoundaryTests(TestCase):
    @override_settings(
        PRR_INTAKE_ATTACHMENT_MAX_BYTES=10,
        PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=20,
        PRR_INTAKE_ATTACHMENT_MAX_COUNT=2,
    )
    def test_exact_per_file_and_total_limits_are_accepted(self):
        from apps.records.upload_handlers import (
            IntakeAttachmentLimitUploadHandler,
        )

        request = type("Request", (), {})()
        handler = IntakeAttachmentLimitUploadHandler(request)
        for filename in ("first.pdf", "second.pdf"):
            handler.new_file(
                "attachments",
                filename,
                "application/pdf",
                10,
            )
            self.assertEqual(handler.receive_data_chunk(b"x" * 10, 0), b"x" * 10)
            handler.file_complete(10)

        self.assertEqual(handler.total_file_bytes, 20)
        self.assertFalse(
            hasattr(request, "_prr_intake_upload_limit_error")
        )

    @override_settings(
        PRR_INTAKE_ATTACHMENT_MAX_BYTES=10,
        PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES=20,
        PRR_INTAKE_ATTACHMENT_MAX_COUNT=2,
    )
    def test_one_byte_over_per_file_limit_is_rejected(self):
        from apps.records.upload_handlers import (
            IntakeAttachmentLimitUploadHandler,
        )

        request = type("Request", (), {})()
        handler = IntakeAttachmentLimitUploadHandler(request)

        with self.assertRaises(StopUpload):
            handler.new_file(
                "attachments",
                "oversized.pdf",
                "application/pdf",
                11,
            )

        self.assertIn(
            "per-file size limit",
            request._prr_intake_upload_limit_error,
        )


@override_settings(
    OBJECT_STORE={
        "endpoint": "http://objects.test",
        "bucket": "prr-records",
        "access_key": "test",
        "secret_key": "test-secret",
        "region": "",
    }
)
class IntakeAttachmentStorageTests(TestCase):
    def setUp(self):
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester, description="Records with supplied context."
        )
        self.attachment = IntakeAttachment.objects.create(
            request=self.prr,
            original_filename="context.pdf",
            content_type="application/pdf",
            size=8,
            sha256="c" * 64,
            storage_key=f"quarantine/intake/{self.prr.id}/generated-id",
        )

    @patch("apps.records.attachments._object_store_client")
    def test_quarantine_upload_forces_octet_stream_and_security_metadata(self, client):
        from apps.records.attachments import store_quarantined_attachment

        upload = SimpleUploadedFile(
            "context.pdf", b"pdfbytes", content_type="application/pdf"
        )

        store_quarantined_attachment(self.attachment, upload)

        stored = client.return_value.put_object.call_args.kwargs
        self.assertEqual(stored["Bucket"], "prr-records")
        self.assertEqual(stored["Key"], self.attachment.storage_key)
        self.assertEqual(stored["ContentLength"], self.attachment.size)
        self.assertEqual(stored["ContentType"], "application/octet-stream")
        self.assertEqual(stored["Metadata"]["sha256"], self.attachment.sha256)
        self.assertEqual(stored["Metadata"]["scan-status"], "pending")
        client.return_value.upload_fileobj.assert_not_called()

    @patch("apps.records.attachments._object_store_client")
    def test_clean_promotion_replaces_metadata_then_deletes_quarantine(self, client):
        from apps.records.attachments import promote_clean_attachment

        client.return_value.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}},
            "HeadObject",
        )
        destination = f"intake/{self.prr.id}/{self.attachment.id}"
        promote_clean_attachment(self.attachment.storage_key, destination)

        client.return_value.copy_object.assert_called_once_with(
            Bucket="prr-records",
            Key=destination,
            CopySource={"Bucket": "prr-records", "Key": self.attachment.storage_key},
            ContentType="application/octet-stream",
            Metadata={"scan-status": "clean"},
            MetadataDirective="REPLACE",
        )
        client.return_value.delete_object.assert_called_once_with(
            Bucket="prr-records", Key=self.attachment.storage_key
        )

    def test_pending_object_cannot_be_opened_even_by_internal_helper(self):
        from apps.records.attachments import open_clean_attachment

        with self.assertRaises(ValueError):
            open_clean_attachment(self.attachment)

    @patch("apps.records.attachments._clamav_scan")
    @patch("apps.records.attachments._object_store_client")
    def test_clean_scan_result_still_fails_closed_on_object_integrity_mismatch(
        self, client, clamav_scan
    ):
        from apps.records.attachments import (
            AttachmentScanUnavailable,
            scan_object_for_malware,
        )

        body = MagicMock()
        body.iter_chunks.return_value = iter([b"tampered bytes"])
        client.return_value.get_object.return_value = {"Body": body}

        def consume(chunks):
            list(chunks)
            return True, "OK"

        clamav_scan.side_effect = consume

        with self.assertRaises(AttachmentScanUnavailable):
            scan_object_for_malware(
                self.attachment.storage_key,
                expected_size=self.attachment.size,
                expected_sha256=self.attachment.sha256,
            )
        body.close.assert_called_once()


class IntakeAttachmentScanTests(TestCase):
    def setUp(self):
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records concerning the transportation plan.",
        )
        self.attachment = IntakeAttachment.objects.create(
            request=self.prr,
            original_filename="context.pdf",
            content_type="application/pdf",
            size=16,
            sha256="a" * 64,
            storage_key=f"quarantine/intake/{self.prr.id}/context.pdf",
        )

    @patch("apps.records.attachments.promote_clean_attachment")
    @patch("apps.records.attachments.scan_object_for_malware", return_value=(True, "OK"))
    def test_clean_scan_promotes_file_and_records_completion(self, scan, promote):
        scan_intake_attachment(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.scan_status, IntakeAttachment.ScanStatus.CLEAN)
        self.assertTrue(self.attachment.scanned_at)
        self.assertEqual(self.attachment.scan_detail, "OK")
        self.assertTrue(self.attachment.storage_key.startswith("intake/"))
        promote.assert_called_once()

    @patch("apps.records.attachments.delete_quarantined_attachment")
    @patch(
        "apps.records.attachments.scan_object_for_malware",
        return_value=(False, "Eicar-Test-Signature FOUND"),
    )
    def test_infected_scan_deletes_bytes_and_keeps_evidentiary_metadata(
        self, scan, delete
    ):
        scan_intake_attachment(str(self.attachment.id))

        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.scan_status, IntakeAttachment.ScanStatus.INFECTED)
        self.assertEqual(self.attachment.scan_detail, "Eicar-Test-Signature FOUND")
        self.assertTrue(self.attachment.scanned_at)
        delete.assert_called_once_with(self.attachment.storage_key)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class IntakeAttachmentStaffApiTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.login(username="officer", password="pw")
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester, description="Records about the capital project."
        )
        self.attachment = IntakeAttachment.objects.create(
            request=self.prr,
            original_filename="context.pdf",
            content_type="application/pdf",
            size=24,
            sha256="b" * 64,
            storage_key=f"quarantine/intake/{self.prr.id}/context.pdf",
        )

    def test_request_detail_lists_attachment_metadata_without_storage_key(self):
        response = self.client.get(f"/api/staff/requests/{self.prr.number}/")

        self.assertEqual(response.status_code, 200)
        [row] = response.json()["intake_attachments"]
        self.assertEqual(row["original_filename"], "context.pdf")
        self.assertEqual(row["scan_status"], IntakeAttachment.ScanStatus.PENDING)
        self.assertNotIn("storage_key", row)
        self.assertNotIn("scan_detail", row)

    def test_pending_attachment_cannot_be_downloaded(self):
        response = self.client.post(
            f"/api/staff/intake-attachments/{self.attachment.id}/download/"
        )

        self.assertEqual(response.status_code, 409)

    def test_reviewer_cannot_read_or_download_intake_attachment(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        detail = self.client.get(f"/api/staff/requests/{self.prr.number}/")
        download = self.client.post(
            f"/api/staff/intake-attachments/{self.attachment.id}/download/"
        )

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["intake_attachments"], [])
        self.assertEqual(download.status_code, 403)

    def test_clean_attachment_gets_short_lived_user_bound_download(self):
        self.attachment.scan_status = IntakeAttachment.ScanStatus.CLEAN
        self.attachment.scanned_at = timezone.now()
        self.attachment.storage_key = f"intake/{self.prr.id}/context.pdf"
        self.attachment.save(update_fields=["scan_status", "scanned_at", "storage_key"])

        response = self.client.post(
            f"/api/staff/intake-attachments/{self.attachment.id}/download/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_in"], 300)
        self.assertIn(
            f"/api/staff/intake-attachments/{self.attachment.id}/content/",
            response.json()["url"],
        )

    @patch("apps.api.resources.intake_attachments.open_clean_attachment")
    def test_user_bound_download_streams_private_object_without_sniffing(self, open_file):
        from io import BytesIO

        self.attachment.scan_status = IntakeAttachment.ScanStatus.CLEAN
        self.attachment.scanned_at = timezone.now()
        self.attachment.storage_key = f"intake/{self.prr.id}/context.pdf"
        self.attachment.save(update_fields=["scan_status", "scanned_at", "storage_key"])
        authorization = self.client.post(
            f"/api/staff/intake-attachments/{self.attachment.id}/download/"
        )
        path = authorization.json()["url"]
        open_file.return_value = BytesIO(b"clean attachment bytes")

        response = self.client.get(path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"clean attachment bytes")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_download_url_cannot_be_used_by_a_different_officer(self):
        self.attachment.scan_status = IntakeAttachment.ScanStatus.CLEAN
        self.attachment.storage_key = f"intake/{self.prr.id}/context.pdf"
        self.attachment.save(update_fields=["scan_status", "storage_key"])
        authorization = self.client.post(
            f"/api/staff/intake-attachments/{self.attachment.id}/download/"
        )
        path = authorization.json()["url"]
        other = get_user_model().objects.create_user(
            "other-officer", "other@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(other, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(other)

        response = self.client.get(path)

        self.assertEqual(response.status_code, 404)
