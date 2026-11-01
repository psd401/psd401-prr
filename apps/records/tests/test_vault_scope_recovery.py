"""Scope recovered from a retired system lands on the case, honestly labelled."""

import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    StatusEvent,
)


class VaultScopeRecoveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ro", email="ro@psd401.net"
        )
        self.batch = LegacyImportBatch.objects.create(
            imported_by=self.user,
            source_sha256="0" * 64, mapping_sha256="1" * 64,
            plan_sha256="2" * 64, row_count=1, mapping_snapshot={},
        )
        self.request = self._case("26-90")

    def _case(self, number):
        requester = Requester.objects.create(
            name="R", email=f"{number}@example.org"
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester, description="d", received_at=timezone.now()
        )
        LegacyRequestImport.objects.create(
            batch=self.batch, request=request, source_id=number,
            source_number=number, source_status="Open - awaiting response",
            source_row_sha256="3" * 64,
        )
        return request

    def _run(self, scope, **kwargs):
        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        path = Path(directory) / "scope.json"
        path.write_text(json.dumps(scope))
        out = io.StringIO()
        call_command(
            "record_vault_scope", str(path),
            "--operator", "records-officer", stdout=out, **kwargs,
        )
        return out.getvalue()

    SCOPE = {
        "26-90": {
            "unmigrated": 120,
            "searches": ["Sample_Search"],
            "custodians": ["taylor@psd401.net", "dana@psd401.net"],
        }
    }

    def test_dry_run_writes_nothing(self):
        output = self._run(self.SCOPE)
        self.assertIn("to record            : 1", output)
        self.assertIn("Dry run only", output)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_scope_is_written_to_the_case_timeline(self):
        self._run(self.SCOPE, commit=True)
        note = StatusEvent.objects.get(request=self.request).note
        self.assertIn("Sample_Search", note)
        self.assertIn("dana@psd401.net", note)
        self.assertIn("taylor@psd401.net", note)
        self.assertIn("120 file(s) that were NOT migrated", note)

    def test_the_note_says_the_scope_is_partial(self):
        # A scope that looks complete is worse than one honestly partial: a
        # reviewer might re-run it believing nothing more was needed.
        self._run(self.SCOPE, commit=True)
        note = StatusEvent.objects.get(request=self.request).note
        self.assertIn("never the query terms or the date", note)
        self.assertIn("Do not treat this as a complete", note)

    def test_custodians_are_listed_in_a_stable_order(self):
        self._run(self.SCOPE, commit=True)
        note = StatusEvent.objects.get(request=self.request).note
        self.assertLess(note.index("dana@"), note.index("taylor@"))

    def test_a_request_that_was_never_imported_blocks_the_run(self):
        with self.assertRaisesMessage(CommandError, "blocking issue"):
            self._run({"99-99": self.SCOPE["26-90"]}, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_an_entry_missing_custodians_blocks_the_run(self):
        with self.assertRaisesMessage(CommandError, "blocking issue"):
            self._run({"26-90": {"searches": ["X"], "custodians": []}}, commit=True)

    def test_one_bad_entry_blocks_the_whole_run(self):
        # All-or-nothing: a partially recorded scope is a scope nobody can
        # trust the completeness of.
        self._case("26-31")
        scope = dict(self.SCOPE)
        scope["99-99"] = self.SCOPE["26-90"]
        with self.assertRaises(CommandError):
            self._run(scope, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_running_twice_does_not_duplicate(self):
        self._run(self.SCOPE, commit=True)
        output = self._run(self.SCOPE, commit=True)
        self.assertIn("already recorded     : 1", output)
        self.assertEqual(StatusEvent.objects.filter(request=self.request).count(), 1)

    def test_recording_is_audited_with_the_counts(self):
        self._run(self.SCOPE, commit=True)
        entry = AuditLog.objects.filter(
            action="request.vault_scope_recovered"
        ).get()
        self.assertEqual(entry.detail["custodian_count"], 2)
        self.assertEqual(entry.detail["search_count"], 1)
        self.assertEqual(entry.detail["unmigrated_files"], 120)

    def test_the_retired_system_is_named(self):
        self._run(self.SCOPE, commit=True, source_system="NextRequest (psd)")
        self.assertIn(
            "NextRequest (psd)", StatusEvent.objects.get(request=self.request).note
        )

    def test_a_malformed_scope_file_is_refused(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        path = Path(directory) / "bad.json"
        path.write_text("{not json")
        with self.assertRaisesMessage(CommandError, "not valid JSON"):
            call_command(
                "record_vault_scope", str(path),
                "--operator", "records-officer", stdout=io.StringIO(),
            )
