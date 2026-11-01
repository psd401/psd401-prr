"""Each migrated case can answer "did everything come across?" from its record."""

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


class DocumentReconciliationTests(TestCase):
    WITH = {
        "documents_total": 1792, "folderless": 714, "in_vault_folders": 1078,
        "folderless_migrated": 521, "vault_containers_not_migrated": 193,
        "container_types": {"pst": 150, "zip": 43},
    }
    CLEAN = {
        "documents_total": 14, "folderless": 14, "in_vault_folders": 0,
        "folderless_migrated": 14, "vault_containers_not_migrated": 0,
        "container_types": {},
    }

    def setUp(self):
        user = get_user_model().objects.create_user(
            username="ro", email="ro@psd401.net"
        )
        self.batch = LegacyImportBatch.objects.create(
            imported_by=user, source_sha256="0" * 64, mapping_sha256="1" * 64,
            plan_sha256="2" * 64, row_count=2, mapping_snapshot={},
        )
        self.a = self._case("26-19")
        self.b = self._case("26-35")

    def _case(self, number):
        requester = Requester.objects.create(
            name="R", email=f"{number}@example.org"
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester, description="d", received_at=timezone.now()
        )
        LegacyRequestImport.objects.create(
            batch=self.batch, request=request, source_id=number,
            source_number=number, source_status="Open - responded",
            source_row_sha256="3" * 64,
        )
        return request

    def _run(self, payload, **kwargs):
        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        path = Path(directory) / "r.json"
        path.write_text(json.dumps(payload))
        out = io.StringIO()
        call_command(
            "record_document_reconciliation", str(path),
            "--operator", "records-officer", stdout=out, **kwargs,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        out = self._run({"26-19": self.WITH})
        self.assertIn("to record        : 1", out)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_the_note_states_every_count(self):
        self._run({"26-19": self.WITH}, commit=True)
        note = StatusEvent.objects.get(request=self.a).note
        for value in ("1792", "714", "1078", "521", "193"):
            self.assertIn(value, note)

    def test_containers_are_named_and_explained(self):
        self._run({"26-19": self.WITH}, commit=True)
        note = StatusEvent.objects.get(request=self.a).note
        self.assertIn("150 .pst", note)
        self.assertIn("43 .zip", note)
        # "We chose not to move these" has to read as a decision, not a gap.
        self.assertIn("deliberately left behind", note)
        self.assertIn("re-collected from Vault", note)

    def test_a_case_with_nothing_left_behind_says_so(self):
        self._run({"26-35": self.CLEAN}, commit=True)
        note = StatusEvent.objects.get(request=self.b).note
        self.assertIn("every document came across", note)
        self.assertNotIn("deliberately left behind", note)

    def test_incomplete_data_blocks_the_run(self):
        with self.assertRaisesMessage(CommandError, "blocking issue"):
            self._run({"26-19": {"documents_total": 5}}, commit=True)
        self.assertEqual(StatusEvent.objects.count(), 0)

    def test_an_unknown_request_blocks_the_run(self):
        with self.assertRaisesMessage(CommandError, "blocking issue"):
            self._run({"99-99": self.WITH}, commit=True)

    def test_running_twice_does_not_duplicate(self):
        self._run({"26-19": self.WITH}, commit=True)
        out = self._run({"26-19": self.WITH}, commit=True)
        self.assertIn("already recorded : 1", out)
        self.assertEqual(StatusEvent.objects.filter(request=self.a).count(), 1)

    def test_the_counts_are_audited(self):
        self._run({"26-19": self.WITH}, commit=True)
        entry = AuditLog.objects.filter(
            action="request.document_reconciliation_recorded"
        ).get()
        self.assertEqual(entry.detail["vault_containers_not_migrated"], 193)
        self.assertEqual(entry.detail["documents_total"], 1792)

    MIXED = {
        "documents_total": 88, "folderless": 87, "in_vault_folders": 1,
        "folderless_migrated": 87, "vault_containers_not_migrated": 0,
        "container_types": {},
    }

    def test_a_case_with_folder_contents_does_not_claim_everything_came_across(self):
        # Saying "every document came across" while also listing documents that
        # did not is the kind of contradiction a reader stops trusting.
        self._run({"26-19": self.MIXED}, commit=True)
        note = StatusEvent.objects.get(request=self.a).note
        self.assertNotIn("every document came across", note)
        self.assertIn("inside export folders", note)
