# ABOUTME: Staff record uploads: a Records Officer adds files to a case as
# ABOUTME: candidate records through quarantine, ClamAV, and immutable storage.
import hashlib
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.capabilities import CAPABILITIES
from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    RecordSystem,
    Requester,
    RequestStatus,
)
from apps.secure_files import AttachmentScanUnavailable

PDF_BYTES = b"%PDF-1.4\n% a scanned board packet\n%%EOF\n"


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    """Minimal object store that refuses to overwrite a retained object."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, **kwargs):
        source = kwargs["Body"]
        position = source.tell()
        payload = source.read()
        source.seek(position)
        key = kwargs["Key"]
        if key in self.objects and not key.startswith("quarantine/"):
            raise AssertionError(f"immutable object {key} was overwritten")
        self.objects[key] = payload

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}

    def get_bucket_versioning(self, **kwargs):
        return {}

    def delete_object(self, *, Bucket, Key, **kwargs):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def _url(number):
    return f"/api/staff/requests/{number}/record-uploads/"


class RecordUploadApiTests(TestCase):
    def setUp(self):
        self.store = _ObjectClient()
        store_patch = patch(
            "apps.secure_files._object_store_client", return_value=self.store
        )
        store_patch.start()
        self.addCleanup(store_patch.stop)
        # The real scan path runs (it re-reads the quarantined object and
        # verifies size and digest after the verdict); only the ClamAV socket
        # is replaced, so a verdict here is the one production would act on.
        self.scanner = patch(
            "apps.secure_files._clamav_scan",
            side_effect=lambda chunks: ([*chunks], (True, "OK"))[1],
        )
        self.scan = self.scanner.start()
        self.addCleanup(self.scanner.stop)
        requester = Requester.objects.create(
            name="A Requester", email="requester@example.org"
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board packets for March.",
            received_at=timezone.now(),
            status=RequestStatus.GATHERING,
        )
        self.officer = self.make_staff("officer", StaffRole.RECORDS_OFFICER)

    def make_staff(self, username, role):
        user = get_user_model().objects.create_user(
            username, f"{username}@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(user, [role])
        return user

    def post(self, files, *, user=None, number=None):
        self.client.force_login(user or self.officer)
        return self.client.post(
            _url(number or self.prr.number),
            {"files": files},
        )

    def quarantine_keys(self):
        return [key for key in self.store.objects if key.startswith("quarantine/")]

    # ---- the clean path -------------------------------------------------

    def test_clean_upload_creates_a_candidate_record_with_server_facts(self):
        response = self.post([SimpleUploadedFile(
            "board-packet.pdf", PDF_BYTES, content_type="text/plain",
        )])

        self.assertEqual(response.status_code, 200, response.content)
        record = RecordItem.objects.get()
        self.assertEqual(record.request, self.prr)
        self.assertEqual(record.source_system, "upload")
        self.assertEqual(record.original_filename, "board-packet.pdf")
        self.assertEqual(record.sha256, hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertEqual(record.content_length, len(PDF_BYTES))
        # The declared type is the browser's claim; the record's type is
        # decided the way every other RecordItem source decides it.
        self.assertEqual(record.content_type, "application/pdf")
        self.assertEqual(record.custodian, "")
        self.assertIsNone(record.collection_task)
        self.assertEqual(record.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertTrue(record.storage_key.startswith(f"records/{self.prr.id}/"))
        self.assertTrue(record.storage_key.endswith(".pdf"))
        self.assertNotIn("board-packet", record.storage_key)
        self.assertEqual(self.store.objects[record.storage_key], PDF_BYTES)
        self.assertEqual(self.quarantine_keys(), [])
        self.assertTrue(any(
            key.startswith(f"quarantine/record-uploads/{self.prr.id}/")
            for key in self.store.deleted
        ))
        body = response.json()
        self.assertEqual(body["added_count"], 1)
        self.assertEqual(body["results"], [{
            "filename": "board-packet.pdf",
            "outcome": "added",
            "detail": "",
            "record_item_id": str(record.pk),
            "sha256": record.sha256,
            "size": len(PDF_BYTES),
        }])

    def test_each_added_record_is_audited_with_its_uploader(self):
        self.post([
            SimpleUploadedFile("one.pdf", PDF_BYTES),
            SimpleUploadedFile("two.pdf", PDF_BYTES + b"two"),
        ])

        self.assertEqual(RecordItem.objects.count(), 2)
        entries = AuditLog.objects.filter(action="records.record_uploaded")
        self.assertEqual(entries.count(), 2)
        for record in RecordItem.objects.all():
            entry = entries.get(target=str(record.pk))
            self.assertEqual(entry.actor, self.officer)
            self.assertEqual(entry.detail["request_number"], self.prr.number)
            self.assertEqual(entry.detail["filename"], record.original_filename)
            self.assertEqual(entry.detail["sha256"], record.sha256)
            self.assertEqual(entry.detail["size"], record.content_length)

    def test_a_format_needing_a_rendition_queues_its_derivative(self):
        with patch("apps.records.tasks.build_record_derivative.delay") as queued:
            self.post([SimpleUploadedFile(
                "memo.docx", b"PK\x03\x04 not really a docx",
            )])

        record = RecordItem.objects.get()
        self.assertEqual(record.derivative_state, RecordItem.DerivativeState.PENDING)
        queued.assert_called_once_with(str(record.pk))

    # ---- fail closed ----------------------------------------------------

    def test_infected_file_creates_nothing_and_leaves_no_bytes(self):
        self.scan.side_effect = lambda chunks: (
            [*chunks], (False, "Eicar-Test-Signature")
        )[1]

        response = self.post([SimpleUploadedFile("evil.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(self.store.objects, {})
        result = response.json()["results"][0]
        self.assertEqual(result["outcome"], "infected")
        self.assertIn("Eicar-Test-Signature", result["detail"])
        self.assertIsNone(result["record_item_id"])
        entry = AuditLog.objects.get(action="records.record_upload_rejected")
        self.assertEqual(entry.actor, self.officer)
        self.assertEqual(entry.detail["reason"], "infected")
        self.assertEqual(entry.detail["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())

    def test_scanner_unavailable_refuses_the_file_instead_of_accepting_it(self):
        self.scan.side_effect = AttachmentScanUnavailable("ClamAV could not be reached.")

        response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(self.store.objects, {})
        result = response.json()["results"][0]
        self.assertEqual(result["outcome"], "scanner_unavailable")
        self.assertEqual(response.json()["added_count"], 0)
        self.assertTrue(AuditLog.objects.filter(
            action="records.record_upload_rejected",
            detail__reason="scanner_unavailable",
        ).exists())

    @override_settings(CLAMAV_HOST="")
    def test_an_unconfigured_scanner_is_unavailable_not_clean(self):
        self.scanner.stop()
        self.addCleanup(self.scanner.start)

        response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(
            response.json()["results"][0]["outcome"], "scanner_unavailable"
        )

    def test_empty_file_is_refused(self):
        response = self.post([SimpleUploadedFile("blank.pdf", b"")])

        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(response.json()["results"][0]["outcome"], "empty")

    def test_byte_identical_file_already_on_the_case_is_not_added_twice(self):
        self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])
        first = RecordItem.objects.get()

        response = self.post([
            SimpleUploadedFile("board-again.pdf", PDF_BYTES),
            SimpleUploadedFile("board-third.pdf", PDF_BYTES),
        ])

        self.assertEqual(RecordItem.objects.count(), 1)
        results = response.json()["results"]
        self.assertEqual([r["outcome"] for r in results], ["duplicate", "duplicate"])
        self.assertEqual(results[0]["record_item_id"], str(first.pk))

    # ---- who and where --------------------------------------------------

    def test_only_a_records_officer_may_upload(self):
        for role in (
            StaffRole.REVIEWER,
            StaffRole.LIAISON,
            StaffRole.ADMIN,
            StaffRole.INTAKE_CLERK,
        ):
            with self.subTest(role=role):
                user = self.make_staff(f"user-{role}", role)
                response = self.post(
                    [SimpleUploadedFile("board.pdf", PDF_BYTES)], user=user
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(self.store.objects, {})

    def test_anonymous_is_refused(self):
        response = self.client.post(
            _url(self.prr.number),
            {"files": [SimpleUploadedFile("board.pdf", PDF_BYTES)]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(RecordItem.objects.exists())

    def test_liaison_assigned_to_the_case_still_cannot_upload(self):
        liaison = self.make_staff("liaison", StaffRole.LIAISON)
        system = RecordSystem.objects.create(name="Board files", slug="board-files")
        CollectionTask.objects.create(
            request=self.prr,
            system=system,
            kind=CollectionTask.Kind.MANUAL,
            assigned_to=liaison,
        )

        response = self.post(
            [SimpleUploadedFile("board.pdf", PDF_BYTES)], user=liaison
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(RecordItem.objects.exists())

    def test_unknown_case_is_not_found(self):
        response = self.post(
            [SimpleUploadedFile("board.pdf", PDF_BYTES)], number="99-9999"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.objects, {})

    def test_closed_case_refuses_new_records_until_reopened(self):
        self.prr.status = RequestStatus.FULFILLED
        self.prr.save(update_fields=["status"])

        response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "closed_request")
        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(self.store.objects, {})

    # ---- bounds ---------------------------------------------------------

    def test_no_files_is_a_validation_error(self):
        self.client.force_login(self.officer)
        response = self.client.post(_url(self.prr.number), {})
        self.assertEqual(response.status_code, 400)

    @override_settings(PRR_RECORD_UPLOAD_MAX_BYTES=16)
    def test_file_over_the_size_limit_is_refused_before_storage(self):
        response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "multipart_upload_limit_exceeded")
        self.assertFalse(RecordItem.objects.exists())
        self.assertEqual(self.store.objects, {})

    @override_settings(PRR_RECORD_UPLOAD_MAX_COUNT=1)
    def test_more_files_than_the_count_limit_is_refused(self):
        response = self.post([
            SimpleUploadedFile("one.pdf", PDF_BYTES),
            SimpleUploadedFile("two.pdf", PDF_BYTES + b"2"),
        ])

        self.assertEqual(response.status_code, 413)
        self.assertFalse(RecordItem.objects.exists())

    @override_settings(PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES=len(PDF_BYTES) + 1)
    def test_files_over_the_combined_limit_are_refused(self):
        response = self.post([
            SimpleUploadedFile("one.pdf", PDF_BYTES),
            SimpleUploadedFile("two.pdf", PDF_BYTES + b"2"),
        ])

        self.assertEqual(response.status_code, 413)
        self.assertFalse(RecordItem.objects.exists())

    # ---- races and mid-batch changes ------------------------------------

    def _retain_then(self, action, *, on_call=1):
        """Wrap the real scan-and-retain so ``action`` runs as it returns.

        That is the window a concurrent request lands in: the bytes are
        already retained under records/ and the row is not yet written.
        """
        from apps.records import record_uploads

        real = record_uploads.retain_scanned_record_bytes
        calls = {"n": 0}

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == on_call:
                action()
            return result

        return patch.object(
            record_uploads, "retain_scanned_record_bytes", side_effect=wrapper
        )

    def _after_first_record(self, action):
        """Run ``action`` right after the first record row is written."""
        from apps.records import record_uploads

        real = record_uploads.create_scanned_record_item
        calls = {"n": 0}

        def wrapper(*args, **kwargs):
            record = real(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                action()
            return record

        return patch.object(
            record_uploads, "create_scanned_record_item", side_effect=wrapper
        )

    def _close_case(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED
        )

    def test_a_concurrent_identical_upload_loses_the_race_as_a_duplicate(self):
        sha256 = hashlib.sha256(PDF_BYTES).hexdigest()

        def competitor():
            RecordItem.objects.create(
                request=self.prr,
                source_system="upload",
                original_filename="competitor.pdf",
                storage_key=f"records/{self.prr.id}/competitor.pdf",
                sha256=sha256,
                content_length=len(PDF_BYTES),
                content_type="application/pdf",
            )

        with self._retain_then(competitor):
            response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecordItem.objects.filter(sha256=sha256).count(), 1)
        result = response.json()["results"][0]
        self.assertEqual(result["outcome"], "duplicate")
        self.assertEqual(
            result["record_item_id"],
            str(RecordItem.objects.get(sha256=sha256).pk),
        )
        # The losing copy was already retained and the runtime role cannot
        # delete under records/, so it is left and recorded where it is.
        orphan = AuditLog.objects.get(action="records.record_upload_orphaned")
        self.assertEqual(orphan.detail["reason"], "duplicate")
        self.assertIn(orphan.detail["storage_key"], self.store.objects)
        self.assertEqual(self.quarantine_keys(), [])

    def test_a_case_closed_mid_batch_refuses_the_rest_and_reports_the_added(self):
        with self._after_first_record(self._close_case):
            response = self.post([
                SimpleUploadedFile("one.pdf", PDF_BYTES),
                SimpleUploadedFile("two.pdf", PDF_BYTES + b"two"),
            ])

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [r["outcome"] for r in body["results"]], ["added", "case_closed"]
        )
        self.assertEqual(body["added_count"], 1)
        self.assertEqual(RecordItem.objects.count(), 1)
        # Refused before its bytes were retained: nothing orphaned.
        retained = [k for k in self.store.objects if k.startswith("records/")]
        self.assertEqual(retained, [RecordItem.objects.get().storage_key])
        self.assertFalse(
            AuditLog.objects.filter(action="records.record_upload_orphaned").exists()
        )

    def test_a_role_lost_mid_batch_refuses_the_rest_and_reports_the_added(self):
        with self._after_first_record(
            lambda: apply_staff_roles(self.officer, [StaffRole.REVIEWER])
        ):
            response = self.post([
                SimpleUploadedFile("one.pdf", PDF_BYTES),
                SimpleUploadedFile("two.pdf", PDF_BYTES + b"two"),
            ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [r["outcome"] for r in response.json()["results"]],
            ["added", "not_authorized"],
        )
        self.assertEqual(RecordItem.objects.count(), 1)

    def test_a_case_closed_during_the_scan_leaves_an_audited_orphan(self):
        with self._retain_then(self._close_case):
            response = self.post([SimpleUploadedFile("board.pdf", PDF_BYTES)])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["outcome"], "case_closed")
        self.assertFalse(RecordItem.objects.exists())
        orphan = AuditLog.objects.get(action="records.record_upload_orphaned")
        self.assertEqual(orphan.detail["reason"], "case_closed")
        self.assertEqual(orphan.actor, self.officer)
        self.assertIn(orphan.detail["storage_key"], self.store.objects)

    # ---- hostile names --------------------------------------------------

    def test_hostile_filenames_never_reach_a_key_or_keep_control_characters(self):
        import re

        key_shape = re.compile(
            rf"^records/{self.prr.id}/"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
            r"\.[a-z0-9]{1,10}$"
        )
        names = [
            ("../../../etc/passwd\x00.pdf", b"one"),
            ("re\x07port\x1b[31m.pdf", b"two"),
            ("no-extension", b"three"),
        ]
        for name, payload in names:
            # The name travels in the multipart header exactly as given; what
            # this pins is the end-to-end result, whichever layer (Django's
            # multipart parser or _display_name) does the stripping.
            upload = SimpleUploadedFile(name, PDF_BYTES + payload)
            with self.subTest(name=name):
                response = self.post([upload])
                self.assertEqual(response.status_code, 200)
                result = response.json()["results"][0]
                self.assertEqual(result["outcome"], "added", result)
                record = RecordItem.objects.get(pk=result["record_item_id"])
                self.assertRegex(record.storage_key, key_shape)
                entry = AuditLog.objects.get(
                    action="records.record_uploaded", target=str(record.pk)
                )
                for shown in (record.original_filename, entry.detail["filename"],
                              result["filename"]):
                    self.assertFalse(
                        any(ord(c) < 32 or ord(c) == 127 for c in shown), shown
                    )
                    self.assertNotIn("/", shown)
                    self.assertNotIn("\\", shown)
                    self.assertNotIn("..", shown)
        no_extension = RecordItem.objects.get(original_filename="no-extension")
        self.assertTrue(no_extension.storage_key.endswith(".bin"))

    # ---- agent parity ---------------------------------------------------

    def test_capability_is_registered_for_agents(self):
        capability = next(
            item for item in CAPABILITIES
            if item["id"] == "requests.record_uploads.create"
        )
        self.assertEqual(capability["method"], "POST")
        self.assertEqual(
            capability["path"], "/api/staff/requests/{number}/record-uploads/"
        )
        for constraint in (
            "records_officer_authority",
            "malware_scanned",
            "append_only_audit",
            "accountable_human_actor",
        ):
            self.assertIn(constraint, capability["constraints"])


class RecordUploadServiceTests(TestCase):
    """The service refuses on its own, whoever calls it."""

    def test_service_requires_a_current_records_officer(self):
        from django.core.exceptions import PermissionDenied

        from apps.records.record_uploads import upload_records

        requester = Requester.objects.create(name="R", email="r@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester, description="x", received_at=timezone.now()
        )
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])

        with self.assertRaises(PermissionDenied):
            upload_records(
                prr, [SimpleUploadedFile("a.pdf", PDF_BYTES)], actor=reviewer
            )
        self.assertFalse(RecordItem.objects.exists())


class DisplayNameTests(TestCase):
    """The display name is safe even when no multipart parser stripped it."""

    def test_path_segments_and_control_characters_are_removed(self):
        from types import SimpleNamespace

        from apps.records.record_uploads import _display_name

        cases = {
            "../../../etc/passwd\x00.pdf": "passwd.pdf",
            "..\\..\\windows\\system32\\evil.docx": "evil.docx",
            "re\x07port\x1b[31m.pdf": "report[31m.pdf",
            "no-extension": "no-extension",
            "../..": "upload",
            "\x00\x01": "upload",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    _display_name(SimpleNamespace(name=raw)), expected
                )


class ScannerLimitTests(TestCase):
    """The upload ceilings and the scanner's own limits move together.

    clamd skips a file larger than ``MaxFileSize`` and still answers OK, so the
    scanner must reject an oversized stream (``StreamMaxLength``) before that
    can happen, and must accept the largest file staff may upload.
    """

    COMPOSE_FILES = ("docker-compose.yml", "docker-compose.deploy.yml")

    @staticmethod
    def _clamd_limits(filename):
        from pathlib import Path

        from django.conf import settings

        lines = (Path(settings.BASE_DIR) / filename).read_text().splitlines()
        start = lines.index("  clamav:") + 1
        limits = {}
        for line in lines[start:]:
            if line.strip() and line.startswith("  ") and not line.startswith("    "):
                break
            key, sep, value = line.strip().partition(":")
            if sep and key.startswith("CLAMD_CONF_"):
                number = value.strip().strip("\"'")
                assert number.endswith("M"), f"{key} must be in M: {number}"
                limits[key.removeprefix("CLAMD_CONF_")] = int(number[:-1]) * 1024 * 1024
        return limits

    def test_defaults_are_500_mib_per_file_and_2_gib_per_upload(self):
        from apps.configuration.registry import spec_for

        self.assertEqual(
            spec_for("PRR_RECORD_UPLOAD_MAX_BYTES").default, 500 * 1024 * 1024
        )
        self.assertEqual(
            spec_for("PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES").default, 2 * 1024**3
        )

    def test_every_stack_scans_the_largest_permitted_file_whole(self):
        from apps.configuration.registry import spec_for

        largest_file = spec_for("PRR_RECORD_UPLOAD_MAX_BYTES").maximum
        largest_upload = spec_for("PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES").maximum
        for filename in self.COMPOSE_FILES:
            limits = self._clamd_limits(filename)
            with self.subTest(filename=filename):
                self.assertGreaterEqual(limits.get("StreamMaxLength", 0), largest_file)
                # A stream clamd accepts is never larger than what it scans.
                self.assertGreaterEqual(
                    limits.get("MaxFileSize", 0), limits["StreamMaxLength"]
                )
                self.assertGreaterEqual(limits.get("MaxScanSize", 0), largest_upload)
