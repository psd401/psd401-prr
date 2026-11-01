"""Legacy NextRequest files arrive expanded, deduped, scanned, and reviewable."""

import csv
import hashlib
import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.models import (
    IntakeAttachment,
    LegacyFileImport,
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    StatusEvent,
)
from apps.records.nextrequest_files import (
    MAX_ARCHIVE_DEPTH,
    LegacyFileMigrationError,
    build_plan,
    commit_plan,
)


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    """Minimal object store that refuses to overwrite a retained object."""

    def __init__(self):
        self.objects = {}

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

    def copy_object(self, **kwargs):
        self.objects[kwargs["Key"]] = self.objects[kwargs["CopySource"]["Key"]]

    def get_bucket_versioning(self, **kwargs):
        return {}

    def delete_object(self, *, Bucket, Key, **kwargs):
        self.objects.pop(Key, None)

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def _zip_bytes(members, *, nested=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        for name, inner in (nested or {}).items():
            archive.writestr(name, inner)
    return buffer.getvalue()


class _LegacyFileTestBase(TestCase):
    maxDiff = None

    def setUp(self):
        self.client_stub = _ObjectClient()
        # Exercise the real scan path (which verifies size and digest after
        # the verdict) with a scanner that reports clean.
        scanner = patch(
            "apps.secure_files._clamav_scan",
            side_effect=lambda chunks: ([*chunks] and (True, "OK")) or (True, "OK"),
        )
        scanner.start()
        self.addCleanup(scanner.stop)
        self.root = Path(self.mkdtemp())
        requester = Requester.objects.create(
            name="A Requester", email="requester@example.org"
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Everything about the parking policy.",
            received_at=timezone.now(),
        )
        operator = get_user_model().objects.create_user(
            username="records-officer", email="ro@psd401.net"
        )
        batch = LegacyImportBatch.objects.create(
            imported_by=operator,
            source_sha256="0" * 64,
            mapping_sha256="1" * 64,
            plan_sha256="2" * 64,
            row_count=1,
            mapping_snapshot={},
        )
        self.imported = LegacyRequestImport.objects.create(
            batch=batch,
            request=self.request,
            source_id="9001",
            source_number="26-19",
            source_status="Open - responded",
            source_row_sha256="3" * 64,
            source_attachment_count=1792,
        )

    def mkdtemp(self):
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def _stage(self, rows):
        """Write payloads to disk and produce the manifest describing them."""
        manifest = self.root / "manifest.csv"
        with manifest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "document_id", "request", "classification", "visibility",
                "title", "path", "bytes", "sha256", "status",
            ])
            writer.writeheader()
            for row in rows:
                payload = row.pop("payload")
                relative = f"files/{row['document_id']}__{row['title']}"
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                writer.writerow({
                    "request": "26-19",
                    "visibility": "Staff Only",
                    "classification": "hand_gathered",
                    "status": "ok",
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    **row,
                })
        return manifest

    def _plan(self, manifest, **kwargs):
        return build_plan(manifest, self.root, **kwargs)

    def _commit(self, plan, manifest):
        return commit_plan(
            plan, manifest, self.root,
            operator="records-officer",
            _client=self.client_stub,
        )


class LegacyFileMigrationTests(_LegacyFileTestBase):
    # ---- expansion -------------------------------------------------------

    def test_archive_is_expanded_into_reviewable_members(self):
        payload = _zip_bytes({
            "2018 Resources/policy.pdf": b"%PDF-1.4 policy",
            "2018 Resources/notes.docx": b"docx bytes",
        })
        manifest = self._stage([
            {"document_id": "1", "title": "2018 Resources.zip", "payload": payload},
        ])
        plan = self._plan(manifest)
        self.assertEqual(plan.blockers, [])
        self.assertEqual(
            sorted(item.member_path for item in plan.stored_items),
            ["2018 Resources/notes.docx", "2018 Resources/policy.pdf"],
        )
        # The container itself is never stored: it cannot be previewed.
        self.assertNotIn("", [item.member_path for item in plan.stored_items])
        self._commit(plan, manifest)
        self.assertEqual(
            sorted(RecordItem.objects.values_list("original_filename", flat=True)),
            ["2018 Resources/notes.docx", "2018 Resources/policy.pdf"],
        )
        self.assertFalse(
            RecordItem.objects.filter(original_filename__endswith=".zip").exists()
        )

    def test_nested_archive_expands_through_both_levels(self):
        inner = _zip_bytes({"inner/deep.pdf": b"%PDF deep"})
        payload = _zip_bytes(
            {"outer/top.pdf": b"%PDF top"},
            nested={"outer/bundle.zip": inner},
        )
        manifest = self._stage([
            {"document_id": "1", "title": "Trng.zip", "payload": payload},
        ])
        plan = self._plan(manifest)
        self.assertEqual(plan.blockers, [])
        self.assertEqual(
            sorted(item.member_path for item in plan.stored_items),
            ["outer/bundle.zip/inner/deep.pdf", "outer/top.pdf"],
        )

    def test_archive_nested_beyond_the_depth_limit_is_blocked(self):
        payload = b"%PDF leaf"
        for _ in range(MAX_ARCHIVE_DEPTH + 1):
            payload = _zip_bytes({"level.zip": payload})
        manifest = self._stage([
            {"document_id": "1", "title": "bomb.zip", "payload": payload},
        ])
        plan = self._plan(manifest)
        self.assertTrue(any("nests deeper" in b for b in plan.blockers), plan.blockers)

    def test_zip_slip_member_is_blocked(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../escape.pdf", b"%PDF escape")
        manifest = self._stage([
            {"document_id": "1", "title": "evil.zip", "payload": buffer.getvalue()},
        ])
        plan = self._plan(manifest)
        self.assertTrue(any("unsafe" in b for b in plan.blockers), plan.blockers)

    def test_macos_archive_noise_is_not_stored(self):
        payload = _zip_bytes({
            "Portable Prezi/slide.png": b"\x89PNG real",
            "__MACOSX/._slide.png": b"junk",
            "Portable Prezi/.DS_Store": b"junk",
        })
        manifest = self._stage([
            {"document_id": "1", "title": "Prezi.zip", "payload": payload},
        ])
        plan = self._plan(manifest)
        self.assertEqual(
            [item.member_path for item in plan.stored_items],
            ["Portable Prezi/slide.png"],
        )

    def test_empty_archive_stores_nothing_and_warns(self):
        manifest = self._stage([
            {"document_id": "1", "title": "Peninsula Tort.zip",
             "payload": _zip_bytes({})},
        ])
        plan = self._plan(manifest)
        self.assertEqual(plan.stored_items, [])
        self.assertEqual(plan.counts()["skipped"], 1)
        self.assertTrue(any("empty archive" in w for w in plan.warnings))

    # ---- dedupe ----------------------------------------------------------

    def test_byte_identical_documents_collapse_onto_one_stored_item(self):
        payload = _zip_bytes({"2018 Resources/policy.pdf": b"%PDF-1.4 policy"})
        manifest = self._stage([
            {"document_id": "64930806", "title": "2018 Resources.zip",
             "payload": payload},
            {"document_id": "68812149", "title": "2018 Resources(1).zip",
             "payload": payload},
        ])
        plan = self._plan(manifest)
        counts = plan.counts()
        self.assertEqual(counts["to_store"], 1)
        self.assertEqual(counts["duplicates"], 1)
        duplicate = next(i for i in plan.items if i.duplicate_of_document_id)
        self.assertEqual(duplicate.duplicate_of_document_id, "64930806")

        self._commit(plan, manifest)
        self.assertEqual(RecordItem.objects.count(), 1)
        # The collapsed document keeps its own provenance row, so dedupe is
        # visible rather than looking like a file went missing.
        self.assertEqual(LegacyFileImport.objects.count(), 2)
        self.assertEqual(
            LegacyFileImport.objects.filter(
                outcome=LegacyFileImport.Outcome.DUPLICATE
            ).count(),
            1,
        )

    def test_commit_is_idempotent(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        plan = self._plan(manifest)
        self._commit(plan, manifest)
        self._commit(self._plan(manifest), manifest)
        self.assertEqual(RecordItem.objects.count(), 1)
        self.assertEqual(LegacyFileImport.objects.count(), 1)

    # ---- classification --------------------------------------------------

    def test_ambiguous_row_blocks_until_explicitly_resolved(self):
        manifest = self._stage([
            {"document_id": "1", "title": "bundle.zip", "classification": "ambiguous",
             "payload": _zip_bytes({"a/policy.pdf": b"%PDF a"})},
        ])
        plan = self._plan(manifest)
        self.assertTrue(any("ambiguous" in b for b in plan.blockers), plan.blockers)

        resolved = self._plan(manifest, ambiguous_classification="hand_gathered")
        self.assertEqual(resolved.blockers, [])
        self.assertEqual(resolved.counts()["record_items"], 1)

    def test_requester_upload_never_becomes_a_responsive_record(self):
        manifest = self._stage([
            {"document_id": "1", "title": "receipt.pdf",
             "classification": "requester_upload", "visibility": "Requester",
             "payload": b"%PDF receipt"},
        ])
        plan = self._plan(manifest)
        self.assertEqual(plan.counts()["record_items"], 0)
        self.assertEqual(plan.counts()["intake_attachments"], 1)
        self._commit(plan, manifest)
        self.assertEqual(RecordItem.objects.count(), 0)
        attachment = IntakeAttachment.objects.get()
        self.assertEqual(attachment.scan_status, IntakeAttachment.ScanStatus.CLEAN)
        self.assertTrue(attachment.storage_key.startswith("intake/"))

    def test_released_source_file_still_arrives_unreviewed(self):
        manifest = self._stage([
            {"document_id": "1", "title": "released.pdf", "visibility": "Requester",
             "payload": b"%PDF released"},
        ])
        self._commit(self._plan(manifest), manifest)
        record = RecordItem.objects.get()
        self.assertEqual(record.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertEqual(
            LegacyFileImport.objects.get().source_visibility, "Requester"
        )

    # ---- integrity and evidence -----------------------------------------

    def test_staged_bytes_that_disagree_with_the_manifest_block(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        (self.root / "files" / "1__policy.pdf").write_bytes(b"%PDF tampered")
        plan = self._plan(manifest)
        self.assertTrue(any("do not match" in b for b in plan.blockers), plan.blockers)

    def test_infected_file_is_rejected_and_its_evidence_retained(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        plan = self._plan(manifest)
        with patch(
            "apps.secure_files.scan_object_for_malware",
            return_value=(False, "Eicar-Test-Signature"),
        ):
            results = self._commit(plan, manifest)
        self.assertEqual(results["infected"], 1)
        self.assertEqual(RecordItem.objects.count(), 0)
        row = LegacyFileImport.objects.get()
        self.assertEqual(row.outcome, LegacyFileImport.Outcome.INFECTED)
        self.assertEqual(row.sha256, hashlib.sha256(b"%PDF one").hexdigest())

    def test_commit_records_a_correcting_timeline_entry(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        self._commit(self._plan(manifest), manifest)
        event = StatusEvent.objects.filter(request=self.request).latest("occurred_at")
        self.assertIn("supersedes the earlier note", event.note)
        self.assertFalse(event.visible_to_requester)
        self.assertEqual(event.from_status, event.to_status)

    def test_commit_never_rewrites_the_source_attachment_count(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        self._commit(self._plan(manifest), manifest)
        self.imported.refresh_from_db()
        self.assertEqual(self.imported.source_attachment_count, 1792)

    def test_commit_audits_the_reviewed_plan_digest(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        plan = self._plan(manifest)
        self._commit(plan, manifest)
        entry = AuditLog.objects.filter(action="records.legacy_files_migrated").get()
        self.assertEqual(entry.detail["plan_sha256"], plan.plan_sha256)
        self.assertEqual(entry.detail["manifest_sha256"], plan.manifest_sha256)

    def test_plan_digest_changes_when_expansion_changes(self):
        manifest = self._stage([
            {"document_id": "1", "title": "a.zip",
             "payload": _zip_bytes({"a/policy.pdf": b"%PDF a"})},
        ])
        self.assertNotEqual(
            self._plan(manifest).plan_sha256,
            self._plan(manifest, expand_archives=False).plan_sha256,
        )

    def test_plan_with_blockers_cannot_be_committed(self):
        manifest = self._stage([
            {"document_id": "1", "title": "b.zip", "classification": "ambiguous",
             "payload": _zip_bytes({"a/policy.pdf": b"%PDF a"})},
        ])
        with self.assertRaises(LegacyFileMigrationError):
            self._commit(self._plan(manifest), manifest)

    def test_unimported_source_request_blocks(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "request": "99-99",
             "payload": b"%PDF one"},
        ])
        plan = self._plan(manifest)
        self.assertTrue(any("never imported" in b for b in plan.blockers), plan.blockers)


class LegacyFileCommandTests(_LegacyFileTestBase):
    def test_command_is_dry_run_without_commit(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        out = io.StringIO()
        call_command(
            "nextrequest_import_files", str(manifest), str(self.root),
            "--operator", "records-officer", stdout=out,
        )
        self.assertIn("Dry run only", out.getvalue())
        self.assertEqual(RecordItem.objects.count(), 0)

    def test_command_requires_the_reviewed_plan_digest(self):
        manifest = self._stage([
            {"document_id": "1", "title": "policy.pdf", "payload": b"%PDF one"},
        ])
        with self.assertRaisesMessage(CommandError, "does not match this plan"):
            call_command(
                "nextrequest_import_files", str(manifest), str(self.root),
                "--operator", "records-officer", "--commit",
                "--expected-plan-sha256", "0" * 64, stdout=io.StringIO(),
            )
        self.assertEqual(RecordItem.objects.count(), 0)
