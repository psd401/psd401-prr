"""One Vault count counts Gmail, Drive, and Chat, each with its own method.

Gmail is counted with ``matters.count`` as it always was. Vault rejects
``matters.count`` for Drive and Chat (HTTP 400 "Corpus type ... is not
supported", production probe 2026-11-08), so those two are counted with a
count-only export whose ``stats.totalArtifactCount`` is read once it is
COMPLETED, and the export is then deleted. It is never downloaded.

Each source ends ``counted`` with a number or ``unavailable`` with a plain
reason, and the run completes only when every source has.
"""

import datetime as dt
from typing import cast
from unittest.mock import patch

from celery import Task
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records import tasks as record_tasks
from apps.records.models import PublicRecordsRequest, Requester, VaultCountRun
from apps.records.vault_counts import (
    advance_vault_count,
    fail_vault_count,
    request_vault_count,
    source_count_summary,
    start_vault_count,
)

VAULT = {
    "PRR_VAULT_CREDENTIALS_FILE": "",
    "PRR_VAULT_ADMIN_USER": "vault-admin@example.org",
    "PRR_VAULT_IMPERSONATION_TARGET": "prr-vault@psd401-prr-identity.iam.gserviceaccount.com",
    "PRR_GOOGLE_HOSTED_DOMAIN": "psd401.net",
}

MAIL_DONE = {
    "name": "operations/mail",
    "done": True,
    "response": {
        "totalCount": "40",
        "mailCountResult": {"queriedAccountsCount": "1", "matchingAccountsCount": "1"},
    },
}


@override_settings(**VAULT)
class _Fixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="source-count-officer", email="officer@psd401.net", is_staff=True,
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="sources@example.org"),
            description="Records about a bus route.",
        )

    def make_run(self, **overrides):
        data = {
            "request": self.request,
            "requested_by": self.officer,
            "terms": '"bus route"',
            "custodian_emails": ["transport@psd401.net"],
            "input_sha256": "a" * 64,
            "sources": ["mail", "drive", "chat"],
        }
        data.update(overrides)
        return VaultCountRun.objects.create(**data)


@patch("apps.records.tasks.execute_vault_count.delay")
class RequestRecordsSourcesTests(_Fixture):
    def test_a_count_searches_every_source(self, _delay):
        run = request_vault_count(
            self.request.pk, actor=self.officer, terms="bus",
            custodian_emails=["transport@psd401.net"],
        )
        self.assertEqual(run.sources, ["mail", "drive", "chat"])
        self.assertEqual(run.org_unit_id, "")

    @override_settings(PRR_VAULT_STAFF_ORG_UNIT_ID="id:03staff")
    def test_a_district_wide_count_captures_the_staff_org_unit(self, _delay):
        run = request_vault_count(
            self.request.pk, actor=self.officer, terms="bus",
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
        )
        self.assertEqual(run.org_unit_id, "id:03staff")

    @override_settings(PRR_VAULT_STAFF_ORG_UNIT_ID="id:03staff")
    def test_a_named_account_count_ignores_the_org_unit(self, _delay):
        run = request_vault_count(
            self.request.pk, actor=self.officer, terms="bus",
            custodian_emails=["transport@psd401.net"],
        )
        self.assertEqual(run.org_unit_id, "")

    def test_the_reviewed_hash_covers_the_org_unit(self, _delay):
        first = request_vault_count(
            self.request.pk, actor=self.officer, terms="bus",
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
        )
        first.state = VaultCountRun.State.FAILED
        first.save(update_fields=["state"])
        with override_settings(PRR_VAULT_STAFF_ORG_UNIT_ID="id:03staff"):
            second = request_vault_count(
                self.request.pk, actor=self.officer, terms="bus",
                search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
            )
        self.assertNotEqual(first.input_sha256, second.input_sha256)


@patch("apps.records.vault_counts.vault.ensure_matter", return_value="matter-1")
@patch("apps.records.vault_counts.vault.start_mail_count", return_value=MAIL_DONE)
class StartTests(_Fixture):
    @patch("apps.records.vault_counts.vault.ensure_count_export")
    def test_drive_and_chat_are_counted_by_a_count_only_export(
        self, ensure_export, _mail, _matter,
    ):
        # Vault's matters.count rejects DRIVE and HANGOUTS_CHAT ("Corpus type
        # ... is not supported", production probe 2026-11-08), so these two
        # sources always count with a count-only export.
        ensure_export.side_effect = lambda source, *a, **k: f"count-{source}"
        run = self.make_run(date_range_start="2026-01-01", date_range_end="2026-01-31")

        self.assertFalse(start_vault_count(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.RUNNING)
        self.assertIsNone(run.total_count)
        self.assertEqual(run.source_counts["mail"], {"state": "counted", "count": 40})
        self.assertEqual([c.args[0] for c in ensure_export.call_args_list], ["drive", "chat"])
        args, kwargs = ensure_export.call_args
        self.assertEqual(args, ("chat", "matter-1", ["transport@psd401.net"], '"bus route"'))
        self.assertIn("count-only", kwargs["name"])
        self.assertIn(str(run.pk), kwargs["name"])
        self.assertEqual(kwargs["start_time"], "2026-01-01T08:00:00Z")
        self.assertEqual(kwargs["end_time"], "2026-02-01T08:00:00Z")
        self.assertFalse(kwargs["entire_org"])
        chat = run.source_counts["chat"]
        self.assertEqual((chat["state"], chat["method"], chat["export_id"]),
                         ("counting", "export", "count-chat"))
        summary = {row["source"]: row for row in source_count_summary(run)}
        self.assertEqual(summary["mail"]["count"], 40)
        self.assertEqual(summary["chat"]["state"], "counting")
        self.assertNotIn("export_id", summary["chat"])
        self.assertTrue(AuditLog.objects.filter(action="vault.count.export_started").exists())

    @patch(
        "apps.records.vault_counts.vault.ensure_count_export",
        side_effect=RuntimeError("403 secret permission detail"),
    )
    def test_a_source_vault_will_not_export_is_unavailable_not_a_failed_run(
        self, _export, _mail, _matter,
    ):
        run = self.make_run()
        self.assertTrue(start_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)
        self.assertEqual(run.total_count, 40)
        summary = {row["source"]: row for row in source_count_summary(run)}
        self.assertEqual(summary["drive"]["state"], "unavailable")
        self.assertEqual(summary["drive"]["reason"], "Google Vault could not count Drive files.")
        self.assertEqual(summary["chat"]["reason"], "Google Vault could not count Chat messages.")
        self.assertIsNone(summary["chat"]["count"])
        self.assertNotIn("secret", str(run.source_counts))
        log = AuditLog.objects.get(action="vault.count.completed")
        self.assertEqual(log.detail["source_counts"], {"mail": 40, "drive": None, "chat": None})

    @patch("apps.records.vault_counts.vault.ensure_count_export", return_value="e")
    def test_district_wide_without_the_staff_org_unit_is_unavailable(
        self, ensure_export, _mail, _matter,
    ):
        run = self.make_run(
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
        )
        self.assertTrue(start_vault_count(run.pk))
        ensure_export.assert_not_called()
        run.refresh_from_db()
        summary = {row["source"]: row for row in source_count_summary(run)}
        self.assertEqual(summary["drive"]["reason"], "Staff organizational unit not set.")
        self.assertEqual(summary["chat"]["state"], "unavailable")

    @patch("apps.records.vault_counts.vault.ensure_count_export", return_value="e")
    def test_district_wide_with_the_org_unit_searches_it(self, ensure_export, _mail, _matter):
        run = self.make_run(
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
            org_unit_id="id:03staff",
        )
        start_vault_count(run.pk)
        _, kwargs = ensure_export.call_args
        self.assertTrue(kwargs["entire_org"])
        self.assertEqual(kwargs["org_unit_id"], "id:03staff")

    @patch("apps.records.vault_counts.vault.ensure_count_export")
    def test_a_gmail_only_count_never_touches_drive_or_chat(self, ensure_export, _mail, _matter):
        run = self.make_run(sources=[])
        self.assertTrue(start_vault_count(run.pk))
        ensure_export.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.total_count, 40)
        self.assertEqual([row["source"] for row in source_count_summary(run)], ["mail"])


@patch("apps.records.vault_counts.vault.delete_export")
@patch("apps.records.vault_counts.vault.get_export")
class AdvanceTests(_Fixture):
    def running(self, **sources):
        entries = {
            "drive": {"state": "counted", "count": 7, "method": "export",
                      "export_id": "count-export-0", "export_deleted": True},
            "chat": {
                "state": "counting", "method": "export", "export_id": "count-export-1",
                "started_at": timezone.now().isoformat(),
            },
        }
        entries.update(sources)
        return self.make_run(
            state=VaultCountRun.State.RUNNING,
            vault_matter_id="matter-1",
            operation_name="operations/mail",
            source_counts={"mail": {"state": "counted", "count": 40}, **entries},
        )

    def test_a_finished_count_export_is_read_then_deleted(self, get_export, delete_export):
        get_export.return_value = {
            "id": "count-export-1", "status": "COMPLETED",
            "stats": {"totalArtifactCount": "12"},
            "cloudStorageSink": {"files": [{"bucketName": "b", "objectName": "o", "size": "1"}]},
        }
        run = self.running()

        self.assertTrue(advance_vault_count(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)
        self.assertEqual(run.total_count, 59)
        self.assertEqual(run.source_counts["chat"]["count"], 12)
        self.assertTrue(run.source_counts["chat"]["export_deleted"])
        delete_export.assert_called_once_with("matter-1", "count-export-1")

    def test_an_export_still_running_keeps_the_count_running(self, get_export, delete_export):
        get_export.return_value = {"id": "count-export-1", "status": "IN_PROGRESS"}
        run = self.running()
        self.assertFalse(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.RUNNING)
        delete_export.assert_not_called()

    def test_a_failed_export_is_unavailable_and_still_deleted(self, get_export, delete_export):
        get_export.return_value = {"id": "count-export-1", "status": "FAILED"}
        run = self.running()
        self.assertTrue(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.total_count, 47)
        self.assertEqual(run.source_counts["chat"]["state"], "unavailable")
        self.assertEqual(
            run.source_counts["chat"]["reason"], "Google Vault could not count Chat messages.",
        )
        delete_export.assert_called_once()

    def test_a_completed_export_without_a_total_is_unavailable(self, get_export, delete_export):
        get_export.return_value = {"id": "count-export-1", "status": "COMPLETED"}
        run = self.running()
        self.assertTrue(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.source_counts["chat"]["state"], "unavailable")

    def test_a_slow_export_times_out_as_unavailable(self, get_export, delete_export):
        get_export.return_value = {"id": "count-export-1", "status": "IN_PROGRESS"}
        old = (timezone.now() - dt.timedelta(hours=7)).isoformat()
        run = self.running(chat={
            "state": "counting", "method": "export", "export_id": "count-export-1",
            "started_at": old,
        })
        self.assertTrue(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(
            run.source_counts["chat"]["reason"],
            "Google Vault did not finish counting Chat messages in time.",
        )
        delete_export.assert_called_once()

    def test_a_failed_delete_is_retried_on_the_next_check(self, get_export, delete_export):
        get_export.return_value = {
            "id": "count-export-1", "status": "COMPLETED", "stats": {"totalArtifactCount": "1"},
        }
        delete_export.side_effect = [RuntimeError("down"), None]
        run = self.running()
        self.assertTrue(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertFalse(run.source_counts["chat"]["export_deleted"])
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)

        cast(Task, record_tasks.poll_vault_exports).run()

        run.refresh_from_db()
        self.assertTrue(run.source_counts["chat"]["export_deleted"])
        self.assertEqual(delete_export.call_count, 2)

    def test_a_provider_hiccup_leaves_the_source_counting(self, get_export, delete_export):
        get_export.side_effect = RuntimeError("blip")
        run = self.running()
        self.assertFalse(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.source_counts["chat"]["state"], "counting")

    @patch("apps.records.vault_counts.vault.get_operation")
    def test_gmail_then_others(self, get_operation, get_export, delete_export):
        get_operation.return_value = MAIL_DONE
        get_export.return_value = {"status": "IN_PROGRESS"}
        run = self.running()
        run.source_counts.pop("mail")
        run.save(update_fields=["source_counts"])
        self.assertFalse(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.source_counts["mail"], {"state": "counted", "count": 40})
        self.assertEqual(run.state, VaultCountRun.State.RUNNING)

    def test_the_beat_advances_a_count_waiting_on_an_export(self, get_export, delete_export):
        get_export.return_value = {
            "id": "count-export-1", "status": "COMPLETED", "stats": {"totalArtifactCount": "3"},
        }
        run = self.running()
        cast(Task, record_tasks.poll_vault_exports).run()
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)

    def test_a_failed_run_deletes_its_count_exports(self, get_export, delete_export):
        run = self.running()
        fail_vault_count(run.pk, code="provider_timeout", detail="Timed out.")
        delete_export.assert_called_once_with("matter-1", "count-export-1")
        run.refresh_from_db()
        self.assertTrue(run.source_counts["chat"]["export_deleted"])


class PollTests(_Fixture):
    @patch("apps.records.vault_counts.advance_vault_count", return_value=False)
    def test_the_fast_poll_hands_a_count_export_to_the_beat(self, _advance):
        run = self.make_run(
            state=VaultCountRun.State.RUNNING,
            source_counts={"mail": {"state": "counted", "count": 1}},
        )
        task = cast(Task, record_tasks.poll_vault_count)
        with patch.object(task, "max_retries", 0):
            task.apply(args=[str(run.pk)])
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.RUNNING)

    @patch("apps.records.vault_counts.advance_vault_count", return_value=False)
    def test_the_fast_poll_still_times_out_gmail(self, _advance):
        run = self.make_run(state=VaultCountRun.State.RUNNING)
        task = cast(Task, record_tasks.poll_vault_count)
        with patch.object(task, "max_retries", 0):
            task.apply(args=[str(run.pk)])
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.FAILED)
        self.assertEqual(run.failure_code, "provider_timeout")


class SummaryTests(_Fixture):
    def test_a_count_from_before_drive_and_chat_is_gmail_only(self):
        run = self.make_run(
            sources=[], state=VaultCountRun.State.COMPLETE, total_count=12,
            completed_at=timezone.now(),
        )
        self.assertEqual(source_count_summary(run), [{
            "source": "mail", "label": "Gmail", "state": "counted", "count": 12, "reason": "",
        }])

    def test_a_pending_count_shows_every_source_counting(self):
        run = self.make_run()
        self.assertEqual(
            [(row["source"], row["state"]) for row in source_count_summary(run)],
            [("mail", "counting"), ("drive", "counting"), ("chat", "counting")],
        )

    def test_a_failed_gmail_count_is_unavailable(self):
        run = self.make_run(
            state=VaultCountRun.State.FAILED, failure_detail="Google Vault could not complete.",
        )
        self.assertEqual(source_count_summary(run)[0]["state"], "unavailable")
        self.assertEqual(source_count_summary(run)[0]["reason"], "Google Vault could not complete.")
