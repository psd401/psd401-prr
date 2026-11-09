"""The system-drafted scope letter (CIO decision, 2026-11-08).

After a Gmail count finishes and before any records are pulled, the system
drafts the "your request matched N items; you may narrow it" letter in its
informational form, from the AI's reading of the request. It never approves,
queues, or sends it: a person does (rule 6).
"""

import datetime as dt
import hashlib

from django.test import override_settings
from django.utils import timezone

from apps.correspondence.models import Correspondence
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    MatterKind,
    PublicRecordsRequest,
    ScopingSession,
    VaultCountRun,
)
from apps.records.scope_letters import prepare_scope_letters

from .test_interpretation_estimate_letter import _LetterFixture

SUMMARY = "Emails about the route 12 bus schedule change in spring 2026."
OPTIONS = [
    {"terms": ["route 12", "schedule"], "rationale": "Only messages about the route 12 schedule itself."},
]


@override_settings(PRR_AI_ADVISORY_ENABLED=True, PRR_AI_AUTOMATIC_SCOPE_LETTER_ENABLED=True)
class SystemScopeLetterTests(_LetterFixture):
    def _reading(self, prr=None, **proposal):
        prr = prr or self.prr
        return AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=prr,
            model_identifier="synthetic-intent-model",
            input_sha256=hashlib.sha256(f"intent{prr.pk}".encode()).hexdigest(),
            state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer,
            proposal={"summary": SUMMARY, "narrowing_options": OPTIONS, **proposal},
        )

    def _count(self, prr=None, *, state=VaultCountRun.State.COMPLETE, total=4210):
        prr = prr or self.prr
        return VaultCountRun.objects.create(
            request=prr,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
            state=state,
            terms='"route 12" OR "bus schedule"',
            input_sha256=hashlib.sha256(f"count{prr.pk}{state}".encode()).hexdigest(),
            total_count=total if state == VaultCountRun.State.COMPLETE else None,
            completed_at=timezone.now() if state == VaultCountRun.State.COMPLETE else None,
        )

    def test_a_finished_count_gets_one_informational_draft_and_only_one(self):
        self._reading()
        self._count()

        self.assertEqual(prepare_scope_letters(), 1)
        self.assertEqual(prepare_scope_letters(), 0)

        draft = Correspondence.objects.get(kind=Correspondence.Kind.SCOPE_CLARIFICATION)
        self.assertTrue(draft.system_prepared)
        self.assertIsNone(draft.created_by)
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(draft.draft_metadata["form"], "informational")
        self.assertEqual(draft.draft_metadata["match_count"], 4210)
        self.assertEqual(draft.draft_metadata["query_summary"], SUMMARY)
        self.assertEqual(draft.draft_metadata["narrowing_options"], OPTIONS)
        self.assertIn("4,210 items", draft.draft_body)
        self.assertIn("This letter is informational", draft.draft_body)

    def test_the_latest_finished_count_is_the_one_stated(self):
        self._reading()
        self._count(total=9000)
        self._count(total=310)
        prepare_scope_letters()
        draft = Correspondence.objects.get(kind=Correspondence.Kind.SCOPE_CLARIFICATION)
        self.assertEqual(draft.draft_metadata["match_count"], 310)

    def test_nothing_is_drafted_before_a_count_finishes(self):
        self._reading()
        self._count(state=VaultCountRun.State.RUNNING)
        self.assertEqual(prepare_scope_letters(), 0)

    def test_nothing_is_drafted_once_records_are_being_pulled(self):
        # Gmail, Drive, or Chat: any Vault collection means records are coming.
        for kind in CollectionTask.VAULT_KINDS:
            with self.subTest(kind=kind):
                prr = PublicRecordsRequest.objects.create(
                    requester=self.prr.requester,
                    description=f"Synthetic request pulled from {kind}.",
                )
                self._reading(prr)
                self._count(prr)
                CollectionTask.objects.create(
                    request=prr, kind=kind, state=CollectionTask.State.PENDING,
                )
        self.assertEqual(prepare_scope_letters(), 0)

    def test_a_manual_department_task_does_not_hold_the_letter_back(self):
        self._reading()
        self._count()
        CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.PENDING,
        )
        self.assertEqual(prepare_scope_letters(), 1)

    def test_a_deleted_draft_stays_deleted_until_a_new_count_finishes(self):
        self._reading()
        self._count()
        prepare_scope_letters()
        Correspondence.objects.filter(kind=Correspondence.Kind.SCOPE_CLARIFICATION).delete()
        self.assertEqual(prepare_scope_letters(), 0)

        self._count(total=120)  # the requester narrowed; a new count finished
        self.assertEqual(prepare_scope_letters(), 1)
        draft = Correspondence.objects.get(kind=Correspondence.Kind.SCOPE_CLARIFICATION)
        self.assertEqual(draft.draft_metadata["match_count"], 120)

    def test_an_unsent_letter_holds_back_a_second_one(self):
        self._reading()
        self._count()
        prepare_scope_letters()
        self._count(total=120)
        self.assertEqual(prepare_scope_letters(), 0)

    def test_nothing_is_drafted_while_scoping_is_still_counting(self):
        self._reading()
        self._count()
        ScopingSession.objects.create(
            request=self.prr,
            trigger=ScopingSession.Trigger.AUTOMATIC,
            state=ScopingSession.State.RUNNING,
            max_iterations=4,
        )
        self.assertEqual(prepare_scope_letters(), 0)

    def test_other_matter_kinds_get_no_letter(self):
        litigation = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="Synthetic litigation pull.",
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
        )
        self._reading(litigation)
        self._count(litigation)
        self.assertEqual(prepare_scope_letters(), 0)

    def test_unusable_narrowing_options_are_left_out_not_fatal(self):
        self._reading(narrowing_options=[{"terms": [], "rationale": "x"}, *OPTIONS])
        self._count()
        self.assertEqual(prepare_scope_letters(), 1)
        draft = Correspondence.objects.get(kind=Correspondence.Kind.SCOPE_CLARIFICATION)
        self.assertEqual(draft.draft_metadata["narrowing_options"], OPTIONS)

    @override_settings(PRR_AI_AUTOMATIC_SCOPE_LETTER_ENABLED=False)
    def test_off_drafts_nothing(self):
        self._reading()
        self._count()
        self.assertEqual(prepare_scope_letters(), 0)

    def test_a_reading_too_short_to_state_does_not_block_newer_cases(self):
        stuck = []
        for n in range(10):
            older = PublicRecordsRequest.objects.create(
                requester=self.prr.requester,
                description=f"Synthetic older request {n}.",
            )
            self._reading(older, summary="Bus.")
            self._count(older)
            stuck.append(older)
        PublicRecordsRequest.objects.filter(pk__in=[p.pk for p in stuck]).update(
            received_at=timezone.now() - dt.timedelta(days=30),
        )
        self._reading()
        self._count()

        prepare_scope_letters()
        self.assertEqual(prepare_scope_letters(), 1)
        draft = Correspondence.objects.get(kind=Correspondence.Kind.SCOPE_CLARIFICATION)
        self.assertEqual(draft.request_id, self.prr.pk)
