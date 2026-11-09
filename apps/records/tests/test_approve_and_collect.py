"""Collection starts from the approved scope (issue #37, rule 11 amendment).

A Records Officer approves the proposed scope and collects in one action.
With the scope trust policy in ``auto``, a finished automatic scoping session
on a request with no Vault collection starts the export itself, recorded as
automatic. Re-scoping supersedes the old collection's unreviewed records and
collects the new scope, with the officer's reason.
"""

import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    CollectionSupersession,
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    ScopeIteration,
    ScopingSession,
    VaultCountRun,
)
from apps.records.scoping import continue_scoping, current_collection, rescope
from apps.records.vault_collections import queue_vault_collection

VAULT = {
    "PRR_VAULT_CREDENTIALS_FILE": "",
    "PRR_VAULT_ADMIN_USER": "vault-admin@example.org",
    "PRR_VAULT_IMPERSONATION_TARGET": "prr-vault@psd401-prr-identity.iam.gserviceaccount.com",
    "PRR_GOOGLE_HOSTED_DOMAIN": "psd401.net",
    "PRR_INTENT_MAX_COUNT_ITERATIONS": 1,
}


class _Fixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="collect-officer", email="collect-officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="collect@example.org"),
            description="Route 12 emails.",
        )
        self._n = 0

    def _session(self, *, trigger=ScopingSession.Trigger.AUTOMATIC, total=40, done=True):
        self._n += 1
        session = ScopingSession.objects.create(
            request=self.request, trigger=trigger, max_iterations=1,
            actor=self.officer if trigger == ScopingSession.Trigger.MANUAL else None,
        )
        run = VaultCountRun.objects.create(
            request=self.request,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
            state=VaultCountRun.State.COMPLETE if done else VaultCountRun.State.RUNNING,
            search_scope=VaultCountRun.SearchScope.ACCOUNTS,
            terms=f'"route 12" #{self._n}',
            custodian_emails=["transportation@psd401.net"],
            input_sha256=hashlib.sha256(f"run{self._n}".encode()).hexdigest(),
            vault_matter_id="matter-1",
            total_count=total,
            completed_at=timezone.now() if done else None,
        )
        ScopeIteration.objects.create(session=session, number=1, count_run=run, reason="Proposal.")
        return session, run


@override_settings(**VAULT)
@patch("apps.records.tasks.execute_vault_collection.delay")
class AutomaticCollectionTests(_Fixture):
    @override_settings(PRR_TRUST_SCOPE_MODE="auto")
    def test_a_finished_automatic_session_collects_its_final_scope(self, delay):
        session, run = self._session()

        with self.captureOnCommitCallbacks(execute=True):

            continue_scoping(session.pk)

        task = CollectionTask.objects.get()
        self.assertEqual(task.trigger, CollectionTask.Trigger.AUTOMATIC)
        self.assertEqual(task.source_vault_count, run)
        self.assertEqual(task.custodian_emails, ["transportation@psd401.net"])
        row = AuditLog.objects.get(action="vault.collection.queued")
        self.assertIsNone(row.actor)
        self.assertEqual(row.detail["trigger"], "automatic")
        delay.assert_called_once_with(task.pk)

    def test_suggest_mode_collects_nothing(self, delay):
        session, _run = self._session()
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(session.pk)
        self.assertFalse(CollectionTask.objects.exists())
        delay.assert_not_called()

    @override_settings(PRR_TRUST_SCOPE_MODE="auto")
    def test_auto_mode_never_collects_from_an_officers_rerun(self, delay):
        session, _run = self._session(trigger=ScopingSession.Trigger.MANUAL)
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(session.pk)
        self.assertFalse(CollectionTask.objects.exists())

    @override_settings(PRR_TRUST_SCOPE_MODE="auto")
    def test_auto_mode_never_adds_a_second_collection(self, delay):
        first, _first_run = self._session()
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(first.pk)
        second, _ = self._session()
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(second.pk)
        self.assertEqual(CollectionTask.objects.count(), 1)

    @override_settings(PRR_TRUST_SCOPE_MODE="auto")
    def test_auto_mode_skips_a_zero_count(self, delay):
        session, _run = self._session(total=0)
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(session.pk)
        self.assertFalse(CollectionTask.objects.exists())

    def test_the_automatic_path_refuses_a_run_outside_a_finished_automatic_session(self, delay):
        from apps.records.vault_collections import queue_automatic_vault_collection

        _session, run = self._session(trigger=ScopingSession.Trigger.MANUAL)
        with self.assertRaises(ValidationError):
            queue_automatic_vault_collection(run.pk)


@override_settings(**VAULT)
@patch("apps.records.tasks.execute_vault_collection.delay")
class RescopeTests(_Fixture):
    def _collected(self):
        from apps.records.vault_collections import queue_vault_collection

        _session, run = self._session()
        task = queue_vault_collection(run.pk, actor=self.officer, expected_input_sha256=run.input_sha256).task
        CollectionTask.objects.filter(pk=task.pk).update(state=CollectionTask.State.COMPLETE)
        for index in range(2):
            RecordItem.objects.create(
                request=self.request, collection_task=task, original_filename=f"old{index}.eml",
                storage_key=f"records/{self.request.pk}/old{index}", content_length=1,
                content_type="message/rfc822",
                sha256=hashlib.sha256(f"old{index}".encode()).hexdigest(),
            )
        return task

    def test_rescope_supersedes_the_old_collection_and_collects_the_new_scope(self, delay):
        old = self._collected()
        _session, new_run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)

        new = rescope(
            self.request, count_run_id=new_run.pk, expected_input_sha256=new_run.input_sha256,
            actor=self.officer, reason="The new scope leaves out the newsletter.",
        )

        self.assertEqual(new.source_vault_count, new_run)
        self.assertEqual(new.trigger, CollectionTask.Trigger.MANUAL)
        self.assertEqual(
            RecordItem.objects.filter(collection_task=old, review_state=RecordItem.ReviewState.SUPERSEDED).count(), 2,
        )

    def _two_source_collection(self, *, drive_state=CollectionTask.State.COMPLETE):
        _session, run = self._session()
        run.sources = ["mail", "drive", "chat"]
        run.source_counts = {
            "mail": {"state": "counted", "count": 30},
            "drive": {"state": "counted", "count": 10},
            "chat": {"state": "counted", "count": 0},
        }
        run.save(update_fields=["sources", "source_counts"])
        tasks = queue_vault_collection(
            run.pk, actor=self.officer, expected_input_sha256=run.input_sha256,
        ).tasks
        self.assertEqual([task.kind for task in tasks], ["vault_mail", "vault_drive"])
        CollectionTask.objects.filter(pk=tasks[0].pk).update(state=CollectionTask.State.COMPLETE)
        CollectionTask.objects.filter(pk=tasks[1].pk).update(state=drive_state)
        for task in tasks:
            RecordItem.objects.create(
                request=self.request, collection_task=task, original_filename=f"{task.kind}.bin",
                storage_key=f"records/{self.request.pk}/{task.kind}", content_length=1,
                content_type="application/octet-stream",
                sha256=hashlib.sha256(task.kind.encode()).hexdigest(),
            )
        return tasks

    def test_rescope_supersedes_every_source_of_the_old_collection(self, delay):
        mail, drive = self._two_source_collection()
        _session, new_run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)

        rescope(
            self.request, count_run_id=new_run.pk, expected_input_sha256=new_run.input_sha256,
            actor=self.officer, reason="The new scope leaves out the newsletter.",
        )

        for task in (mail, drive):
            self.assertTrue(CollectionSupersession.objects.filter(collection_task=task).exists())
            self.assertEqual(
                RecordItem.objects.get(collection_task=task).review_state,
                RecordItem.ReviewState.SUPERSEDED,
            )

    def test_rescope_waits_for_every_source_to_finish(self, delay):
        self._two_source_collection(drive_state=CollectionTask.State.INGESTING)
        _session, new_run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)
        with self.assertRaises(ValidationError):
            rescope(
                self.request, count_run_id=new_run.pk, expected_input_sha256=new_run.input_sha256,
                actor=self.officer, reason="The new scope leaves out the newsletter.",
            )

    @override_settings(PRR_TRUST_SCOPE_MODE="auto")
    def test_auto_mode_counts_a_drive_task_as_an_existing_collection(self, delay):
        self._two_source_collection()
        session, _run = self._session()
        with self.captureOnCommitCallbacks(execute=True):
            continue_scoping(session.pk)
        self.assertEqual(CollectionTask.objects.count(), 2)

    def test_rescope_needs_a_reason_and_a_finished_collection(self, delay):
        _session, run = self._session()
        with self.assertRaises(ValidationError):
            rescope(self.request, count_run_id=run.pk, expected_input_sha256=run.input_sha256,
                    actor=self.officer, reason="Because the new scope is narrower.")
        self._collected()
        _s2, run2 = self._session(trigger=ScopingSession.Trigger.MANUAL)
        with self.assertRaises(ValidationError):
            rescope(self.request, count_run_id=run2.pk, expected_input_sha256=run2.input_sha256,
                    actor=self.officer, reason="short")
        self.assertEqual(CollectionTask.objects.count(), 1)

    def test_a_failed_collection_is_not_the_current_one(self, delay):
        # A failed export never finishes, so it must not lock the card into a
        # re-scope that waits forever: the officer collects again instead.
        old = self._collected()
        _session, run = self._session(trigger=ScopingSession.Trigger.MANUAL)
        failed = queue_vault_collection(run.pk, actor=self.officer, expected_input_sha256=run.input_sha256).task
        CollectionTask.objects.filter(pk=failed.pk).update(state=CollectionTask.State.FAILED)
        current = current_collection(self.request)
        assert current is not None
        self.assertEqual(current["task_id"], old.pk)

        CollectionTask.objects.filter(pk=old.pk).update(state=CollectionTask.State.FAILED)
        self.assertIsNone(current_collection(self.request))
        with self.assertRaises(ValidationError):
            rescope(self.request, count_run_id=run.pk, expected_input_sha256=run.input_sha256,
                    actor=self.officer, reason="The new scope leaves out the newsletter.")

    def test_a_superseded_collection_is_not_the_current_one(self, delay):
        # Re-scope A to B, then B fails: A was replaced, so nothing is the
        # current collection and the officer approves and collects again.
        self._collected()
        _session, run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)
        new = rescope(self.request, count_run_id=run.pk, expected_input_sha256=run.input_sha256,
                      actor=self.officer, reason="The new scope leaves out the newsletter.")
        CollectionTask.objects.filter(pk=new.pk).update(state=CollectionTask.State.FAILED)

        self.assertIsNone(current_collection(self.request))

    def test_a_rescoped_collection_with_nothing_unreviewed_is_still_replaced(self, delay):
        # supersede_collection writes no row when no record is unreviewed;
        # the re-scope still replaced the collection and must say so.
        old = self._collected()
        RecordItem.objects.filter(collection_task=old).update(review_state=RecordItem.ReviewState.RESPONSIVE)
        _session, run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)
        new = rescope(self.request, count_run_id=run.pk, expected_input_sha256=run.input_sha256,
                      actor=self.officer, reason="The new scope leaves out the newsletter.")
        row = CollectionSupersession.objects.get(collection_task=old)
        self.assertEqual(row.record_count, 0)
        self.assertEqual(row.actor, self.officer)
        CollectionTask.objects.filter(pk=new.pk).update(state=CollectionTask.State.FAILED)

        self.assertIsNone(current_collection(self.request))

    def test_a_count_whose_collection_was_replaced_cannot_be_collected_again(self, delay):
        # Collecting it would hand back the old task with its records still
        # superseded: a silent no-op. The officer runs a new count instead.
        old = self._collected()
        _session, run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)
        new = rescope(self.request, count_run_id=run.pk, expected_input_sha256=run.input_sha256,
                      actor=self.officer, reason="The new scope leaves out the newsletter.")
        CollectionTask.objects.filter(pk=new.pk).update(state=CollectionTask.State.COMPLETE)
        old_run = old.source_vault_count
        assert old_run is not None

        with self.assertRaisesMessage(ValidationError, "run scoping again"):
            rescope(self.request, count_run_id=old_run.pk, expected_input_sha256=old_run.input_sha256,
                    actor=self.officer, reason="Going back to the first scope after all.")
        self.assertFalse(CollectionSupersession.objects.filter(collection_task=new).exists())
        with self.assertRaisesMessage(ValidationError, "run scoping again"):
            queue_vault_collection(old_run.pk, actor=self.officer, expected_input_sha256=old_run.input_sha256)

    def test_a_count_whose_export_failed_in_vault_cannot_be_collected_again(self, delay):
        _session, run = self._session()
        task = queue_vault_collection(run.pk, actor=self.officer, expected_input_sha256=run.input_sha256).task
        CollectionTask.objects.filter(pk=task.pk).update(
            state=CollectionTask.State.FAILED, vault_export_id="export-1",
        )
        with self.assertRaisesMessage(ValidationError, "run scoping again"):
            queue_vault_collection(run.pk, actor=self.officer, expected_input_sha256=run.input_sha256)

    def test_the_api_shows_the_diff_and_rescopes(self, delay):
        old = self._collected()
        _session, new_run = self._session(trigger=ScopingSession.Trigger.MANUAL, total=12)
        api = APIClient()
        api.force_authenticate(self.officer)
        body = api.get(f"/api/staff/requests/{self.request.number}/scoping/").json()
        self.assertEqual(body["session"]["final"]["count_run_id"], str(new_run.pk))
        self.assertEqual(body["current_collection"]["task_id"], old.pk)
        self.assertEqual(body["current_collection"]["total"], 40)

        response = api.post(
            f"/api/staff/requests/{self.request.number}/rescope/",
            {"count_run_id": str(new_run.pk), "expected_input_sha256": new_run.input_sha256,
             "reason": "The new scope leaves out the newsletter."},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(CollectionTask.objects.count(), 2)
