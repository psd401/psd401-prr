"""A Vault collection exports every source its count found: Gmail, Drive, Chat.

One reviewed count starts one collection task per source. A source that
counted exactly zero is not exported; one whose count is unavailable is,
because a missing count is not evidence that nothing is there. Drive and
Chat holds are placed only when a person starts the collection.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    VaultCountRun,
)
from apps.records.vault_collections import (
    queue_vault_collection,
    start_vault_collection,
)


def _counts(mail: int | None = 40, drive: int | None = 7, chat: int | None = 3):
    def entry(value):
        if value is None:
            return {"state": "unavailable", "count": None, "reason": "Google Vault could not count."}
        return {"state": "counted", "count": value}

    return {"mail": entry(mail), "drive": entry(drive), "chat": entry(chat)}


@override_settings(
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
    PRR_VAULT_ADMIN_USER="vault-admin@example.org",
    PRR_VAULT_IMPERSONATION_TARGET="prr-vault@psd401-prr-identity.iam.gserviceaccount.com",
)
class _Fixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="source-collect-officer", email="officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="collect@example.org"),
            description="Records about a bus route.",
        )

    def count(self, source_counts=None, **overrides):
        source_counts = _counts() if source_counts is None else source_counts
        total = sum(entry.get("count") or 0 for entry in source_counts.values())
        data = {
            "request": self.request,
            "requested_by": self.officer,
            "state": VaultCountRun.State.COMPLETE,
            "search_scope": VaultCountRun.SearchScope.ACCOUNTS,
            "terms": '"bus route"',
            "custodian_emails": ["transport@psd401.net"],
            "date_range_start": "2026-01-01",
            "date_range_end": "2026-01-31",
            "input_sha256": "a" * 64,
            "vault_matter_id": "matter-1",
            "total_count": total,
            "completed_at": "2026-02-01T16:00:00Z",
            "sources": ["mail", "drive", "chat"],
            "source_counts": source_counts,
        }
        data.update(overrides)
        return VaultCountRun.objects.create(**data)

    def queue(self, run):
        return queue_vault_collection(
            run.pk, actor=self.officer, expected_input_sha256=run.input_sha256,
        )


@patch("apps.records.tasks.execute_vault_collection.delay")
class QueueTests(_Fixture):
    def test_one_task_per_source(self, delay):
        result = self.queue(self.count())

        self.assertTrue(result.created)
        self.assertEqual(
            [task.kind for task in result.tasks],
            ["vault_mail", "vault_drive", "vault_chat"],
        )
        self.assertEqual(result.task.kind, CollectionTask.Kind.VAULT_MAIL)
        self.assertEqual(delay.call_count, 3)
        drive = result.tasks[1]
        self.assertIn("Drive", drive.instructions)
        self.assertEqual(drive.search_terms, '"bus route"')
        self.assertEqual(drive.custodian_emails, ["transport@psd401.net"])

    def test_a_source_that_counted_zero_is_not_exported(self, delay):
        result = self.queue(self.count(_counts(mail=0, drive=None, chat=0)))
        self.assertEqual([task.kind for task in result.tasks], ["vault_drive"])
        self.assertEqual(result.task.kind, CollectionTask.Kind.VAULT_DRIVE)

    def test_every_source_counting_zero_is_refused(self, delay):
        with self.assertRaisesMessage(ValidationError, "Every source counted zero"):
            self.queue(self.count(_counts(mail=0, drive=0, chat=0)))
        self.assertFalse(CollectionTask.objects.exists())
        delay.assert_not_called()

    def test_district_wide_without_the_org_unit_collects_gmail_only(self, delay):
        result = self.queue(self.count(
            _counts(drive=None, chat=None),
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
        ))
        self.assertEqual([task.kind for task in result.tasks], ["vault_mail"])

    def test_district_wide_with_nothing_searchable_is_refused(self, delay):
        with self.assertRaisesMessage(ValidationError, "staff organizational unit"):
            self.queue(self.count(
                _counts(mail=0, drive=None, chat=None),
                search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
            ))

    def test_queueing_twice_adds_nothing(self, delay):
        run = self.count()
        first = self.queue(run)
        second = self.queue(run)
        self.assertFalse(second.created)
        self.assertEqual([t.pk for t in second.tasks], [t.pk for t in first.tasks])
        self.assertEqual(CollectionTask.objects.count(), 3)
        self.assertEqual(delay.call_count, 3)

    def test_only_the_failed_handoff_is_requeued(self, delay):
        run = self.count()
        first = self.queue(run)
        chat = first.tasks[2]
        chat.state = CollectionTask.State.FAILED
        chat.save(update_fields=["state"])
        delay.reset_mock()

        again = self.queue(run)

        delay.assert_called_once_with(chat.pk)
        chat.refresh_from_db()
        self.assertEqual(chat.state, CollectionTask.State.PENDING)
        self.assertFalse(again.created)

    def test_a_failed_export_on_any_source_refuses_a_requeue(self, delay):
        run = self.count()
        drive = self.queue(run).tasks[1]
        drive.state = CollectionTask.State.FAILED
        drive.vault_export_id = "export-x"
        drive.save(update_fields=["state", "vault_export_id"])
        with self.assertRaisesMessage(ValidationError, "replaced or failed"):
            self.queue(run)

    def test_a_count_from_before_drive_and_chat_collects_gmail(self, delay):
        result = self.queue(self.count({}, sources=[], total_count=12))
        self.assertEqual([task.kind for task in result.tasks], ["vault_mail"])


class StartTests(_Fixture):
    def task(self, kind, run, trigger=CollectionTask.Trigger.MANUAL):
        return CollectionTask.objects.create(
            request=self.request, kind=kind, state=CollectionTask.State.PENDING,
            source_vault_count=run, trigger=trigger, search_terms=run.terms,
            custodian_emails=run.custodian_emails,
        )

    @patch("apps.records.vault_collections.vault.ensure_source_export", return_value="drive-export")
    @patch("apps.records.vault_collections.vault.ensure_source_hold", return_value="drive-hold")
    def test_a_person_started_drive_collection_holds_then_exports(self, hold, export):
        run = self.count()
        task = self.task(CollectionTask.Kind.VAULT_DRIVE, run)

        self.assertTrue(start_vault_collection(task.pk))

        name = f"{self.request.number} — Vault count {run.pk} — Drive"
        hold.assert_called_once_with("drive", "matter-1", ["transport@psd401.net"], name=name)
        export.assert_called_once_with(
            "drive", "matter-1", ["transport@psd401.net"], '"bus route"',
            name=name,
            start_time="2026-01-01T08:00:00Z",
            end_time="2026-02-01T08:00:00Z",
            entire_org=False,
            org_unit_id="",
        )
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.RUNNING)
        self.assertEqual(task.vault_hold_id, "drive-hold")
        self.assertEqual(task.vault_export_id, "drive-export")
        self.assertIn("Drive", task.request.events.order_by("-pk").first().note)

    @patch("apps.records.vault_collections.vault.ensure_source_export", return_value="chat-export")
    @patch("apps.records.vault_collections.vault.ensure_source_hold")
    def test_an_automatic_chat_collection_adds_no_hold(self, hold, export):
        run = self.count()
        task = self.task(CollectionTask.Kind.VAULT_CHAT, run, trigger=CollectionTask.Trigger.AUTOMATIC)

        start_vault_collection(task.pk)

        hold.assert_not_called()
        self.assertEqual(export.call_args.args[0], "chat")
        task.refresh_from_db()
        self.assertEqual(task.vault_hold_id, "")

    @patch("apps.records.vault_collections.vault.ensure_mail_export", return_value="mail-export")
    @patch("apps.records.vault_collections.vault.ensure_mail_hold", return_value="mail-hold")
    def test_an_automatic_gmail_collection_keeps_its_hold(self, hold, export):
        run = self.count()
        task = self.task(CollectionTask.Kind.VAULT_MAIL, run, trigger=CollectionTask.Trigger.AUTOMATIC)
        start_vault_collection(task.pk)
        hold.assert_called_once()

    @patch("apps.records.vault_collections.vault.ensure_source_export", return_value="drive-export")
    @patch("apps.records.vault_collections.vault.ensure_source_hold")
    def test_district_wide_drive_searches_the_staff_org_unit_with_no_hold(self, hold, export):
        run = self.count(
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG, custodian_emails=[],
            org_unit_id="id:03staff",
        )
        start_vault_collection(self.task(CollectionTask.Kind.VAULT_DRIVE, run).pk)
        hold.assert_not_called()
        self.assertTrue(export.call_args.kwargs["entire_org"])
        self.assertEqual(export.call_args.kwargs["org_unit_id"], "id:03staff")

    def test_a_manual_task_is_not_a_vault_collection(self):
        run = self.count()
        task = self.task(CollectionTask.Kind.MANUAL, run)
        with self.assertRaises(ValidationError):
            start_vault_collection(task.pk)
