"""Iterative scoping (issue #36): propose, count, read the breakdown, tighten.

Every step is a count. Nothing here holds, exports, collects, or changes a
request's status (rule 11 unchanged).
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import AdvisoryOutputInvalid
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    KnowledgeNoiseSender,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
    RequestStatus,
    ScopeIteration,
    ScopingSession,
    VaultCountRun,
)
from apps.records.scoping import continue_scoping, rerun_scoping, start_scoping
from apps.records.vault_counts import _apply_operation, vault_query_from_intent

VAULT = {
    "PRR_VAULT_CREDENTIALS_FILE": "",
    "PRR_VAULT_ADMIN_USER": "vault-admin@example.org",
    "PRR_VAULT_IMPERSONATION_TARGET": "prr-vault@psd401-prr-identity.iam.gserviceaccount.com",
    "PRR_GOOGLE_HOSTED_DOMAIN": "psd401.net",
    "PRR_AI_ADVISORY_ENABLED": True,
    "PRR_AI_AWS_REGION": "us-west-2",
    "PRR_AI_MODEL": "district-approved-model-v1",
    "PRR_INTENT_MAX_COUNT_ITERATIONS": 3,
}

PROPOSAL = {
    "summary": "Bus video and emails about route 12.",
    "required_search_terms": ["route 12"],
    "search_terms": ["bus", "transportation"],
    "record_system_slugs": ["google-workspace"],
    "date_range": {"start": "2026-01-01", "end": "2026-03-01"},
    "custodians": ["transportation@psd401.net", "someone@gmail.com"],
    "exclude_senders": ["news@psd401.net"],
    "scope_rationale": "Transportation holds route mail.",
}


def _complete(run, total, accounts=()):
    _apply_operation(run, {
        "done": True,
        "response": {
            "totalCount": str(total),
            "mailCountResult": {
                "queriedAccountsCount": "1",
                "matchingAccountsCount": str(len(accounts)),
                "accountCounts": [{"account": {"email": email}, "count": str(count)} for email, count in accounts],
            },
        },
    })


class _ScopingFixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="scope-officer", email="scope-officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="scope@example.org"),
            description="Route 12 bus records.",
        )
        RecordSystem.objects.get_or_create(
            slug="google-workspace",
            defaults={"name": "Google Workspace", "category": "Email & documents",
                      "custodian_title": "CIO", "integration": RecordSystem.Integration.VAULT},
        )
        KnowledgeNoiseSender.objects.create(kind="address", value="news@psd401.net")


@override_settings(**VAULT)
@patch("apps.records.tasks.execute_vault_count.delay")
class ScopingTests(_ScopingFixture):

    def test_senders_alone_are_never_a_query(self, _delay):
        self.assertEqual(vault_query_from_intent([], [], exclude_senders=["noise.example"]), "")

    def test_the_query_names_senders_and_leaves_out_noise(self, _delay):
        self.assertEqual(
            vault_query_from_intent(
                ["route 12"], ["bus"],
                sender_domains=["vendor.example"], exclude_senders=["news@psd401.net", "promo.example"],
            ),
            '"route 12" AND "bus" AND from:vendor.example AND -from:news@psd401.net AND -from:promo.example',
        )
        self.assertEqual(
            vault_query_from_intent(["a"], [], sender_domains=["x.org", "y.org"]),
            '"a" AND (from:x.org OR from:y.org)',
        )

    def test_the_first_count_searches_the_named_district_custodians(self, _delay):
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)

        iteration = ScopeIteration.objects.filter(session=session).get()
        run = iteration.count_run
        self.assertEqual(run.search_scope, VaultCountRun.SearchScope.ACCOUNTS)
        # A mailbox outside the district is not a Vault custodian.
        self.assertEqual(run.custodian_emails, ["transportation@psd401.net"])
        self.assertIn("-from:news@psd401.net", run.terms)
        self.assertEqual(iteration.reason, "Transportation holds route mail.")
        self.assertEqual(session.max_iterations, 3)

    @patch("apps.records.scoping.invoke_advisory_model")
    def test_the_agent_reads_the_breakdown_tightens_then_stops(self, invoke, _delay):
        invoke.side_effect = [
            {"decision": "refine", "reason": "Most hits are the district newsletter.",
             "required_search_terms": ["route 12"], "search_terms": ["bus"],
             "exclude_senders": ["news@psd401.net", "district-news.example"]},
            {"decision": "stop", "reason": "The scope is now specific."},
        ]
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 900, [("transportation@psd401.net", 900)])

        continue_scoping(session.pk)

        payload = invoke.call_args.args[1]
        self.assertEqual(payload["iterations"][0]["total"], 900)
        self.assertEqual(payload["iterations"][0]["top_accounts"][0]["email"], "transportation@psd401.net")
        self.assertIn({"kind": "address", "value": "news@psd401.net"}, payload["noise_senders"])
        second = ScopeIteration.objects.filter(session=session).get(number=2)
        self.assertEqual(second.reason, "Most hits are the district newsletter.")
        self.assertIn("-from:district-news.example", second.count_run.terms)

        _complete(second.count_run, 120, [("transportation@psd401.net", 120)])
        continue_scoping(session.pk)

        session.refresh_from_db()
        self.assertEqual(session.state, ScopingSession.State.COMPLETE)
        self.assertEqual(session.stop_reason, "The scope is now specific.")
        self.assertTrue(AuditLog.objects.filter(action="scoping.session_finished").exists())

    @override_settings(PRR_INTENT_MAX_COUNT_ITERATIONS=1)
    @patch("apps.records.scoping.invoke_advisory_model")
    def test_the_iteration_limit_stops_without_asking_the_model(self, invoke, _delay):
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 50)
        continue_scoping(session.pk)
        invoke.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.state, ScopingSession.State.COMPLETE)
        self.assertIn("iteration limit", session.stop_reason)

    @patch("apps.records.scoping.invoke_advisory_model")
    def test_a_refinement_with_no_subject_terms_is_unusable(self, invoke, _delay):
        invoke.return_value = {"decision": "refine", "reason": "Only senders.",
                               "required_search_terms": [], "search_terms": [],
                               "exclude_senders": ["noise.example"]}
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.get(session=session).count_run, 50)
        continue_scoping(session.pk)
        session.refresh_from_db()
        self.assertEqual(ScopeIteration.objects.filter(session=session).count(), 1)
        self.assertIn("last count stands", session.stop_reason)

    def test_a_session_whose_first_count_cannot_queue_is_not_left_running(self, delay):
        from apps.records.vault_counts import VaultCountAlreadyRunning, request_vault_count

        request_vault_count(
            self.request.pk, actor=self.officer, terms='"bus"', custodian_emails=[],
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
        )
        with self.assertRaises(VaultCountAlreadyRunning):
            start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        session = ScopingSession.objects.get()
        self.assertEqual(session.state, ScopingSession.State.FAILED)
        self.assertIn("could not start", session.stop_reason)

    @patch("apps.records.scoping.invoke_advisory_model", side_effect=AdvisoryOutputInvalid("bad"))
    def test_a_provider_failure_keeps_the_last_count(self, _invoke, _delay):
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 50)
        continue_scoping(session.pk)
        session.refresh_from_db()
        self.assertEqual(session.state, ScopingSession.State.COMPLETE)
        self.assertIn("last count stands", session.stop_reason)
        self.assertEqual(ScopeIteration.objects.filter(session=session).count(), 1)

    @patch("apps.records.scoping.invoke_advisory_model")
    def test_a_refinement_that_repeats_a_scope_stops(self, invoke, _delay):
        invoke.return_value = {"decision": "refine", "reason": "Same again.",
                               **{k: PROPOSAL[k] for k in ("required_search_terms", "search_terms",
                                                          "custodians", "exclude_senders")}}
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 50)
        continue_scoping(session.pk)
        session.refresh_from_db()
        self.assertEqual(session.state, ScopingSession.State.COMPLETE)
        self.assertEqual(ScopeIteration.objects.filter(session=session).count(), 1)

    def test_a_failed_count_fails_the_session(self, _delay):
        from apps.records.vault_counts import fail_vault_count

        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        fail_vault_count(ScopeIteration.objects.filter(session=session).get().count_run.pk, code="provider_timeout", detail="Timed out.")
        continue_scoping(session.pk)
        session.refresh_from_db()
        self.assertEqual(session.state, ScopingSession.State.FAILED)

    def test_rerun_needs_a_reason_and_runs_even_when_nothing_changed(self, _delay):
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT, request=self.request,
            model_identifier="m", input_sha256="x", state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer, proposal=PROPOSAL,
        )
        with self.assertRaises(ValidationError):
            rerun_scoping(self.request, actor=self.officer, reason="again")
        first = rerun_scoping(self.request, actor=self.officer, reason="Check the narrower custodians.")
        _complete(ScopeIteration.objects.filter(session=first).get().count_run, 10)
        second = rerun_scoping(self.request, actor=self.officer, reason="Re-check after the new hire.")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.trigger, ScopingSession.Trigger.MANUAL)
        self.assertEqual(second.actor, self.officer)
        self.assertEqual(second.reason, "Re-check after the new hire.")

    @patch("apps.records.scoping.invoke_advisory_model")
    def test_scoping_only_counts(self, invoke, _delay):
        invoke.return_value = {"decision": "stop", "reason": "Fine."}
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 5)
        continue_scoping(session.pk)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.RECEIVED)
        self.assertFalse(CollectionTask.objects.exists())

    def test_account_counts_keep_the_busiest_accounts_first(self, _delay):
        session = start_scoping(self.request, PROPOSAL, trigger=ScopingSession.Trigger.AUTOMATIC)
        run = ScopeIteration.objects.filter(session=session).get().count_run
        _complete(run, 30, [("a@psd401.net", 5), ("b@psd401.net", 25)])
        run.refresh_from_db()
        self.assertEqual(run.account_counts, [
            {"email": "b@psd401.net", "count": 25}, {"email": "a@psd401.net", "count": 5},
        ])


@override_settings(**VAULT)
@patch("apps.records.tasks.execute_vault_count.delay")
class ScopingApiTests(_ScopingFixture):
    def test_an_officer_reads_the_trail_and_reruns_with_a_reason(self, _delay):
        from rest_framework.test import APIClient

        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT, request=self.request,
            model_identifier="m", input_sha256="x", state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer, proposal=PROPOSAL,
        )
        api = APIClient()
        api.force_authenticate(self.officer)
        url = f"/api/staff/requests/{self.request.number}/scoping/"
        self.assertEqual(api.get(url).json(), {"session": None})

        self.assertEqual(api.post(url, {"reason": "short"}, format="json").status_code, 400)
        started = api.post(url, {"reason": "The custodian list changed."}, format="json")
        self.assertEqual(started.status_code, 202, started.content)
        session = ScopingSession.objects.get()
        _complete(ScopeIteration.objects.filter(session=session).get().count_run, 77, [("transportation@psd401.net", 77)])

        body = api.get(url).json()["session"]
        self.assertEqual(body["trigger"], "manual")
        self.assertEqual(body["reason"], "The custodian list changed.")
        self.assertEqual(body["final"]["total"], 77)
        self.assertEqual(body["final"]["top_accounts"][0]["email"], "transportation@psd401.net")

    def test_a_reviewer_may_not(self, _delay):
        from rest_framework.test import APIClient

        reviewer = get_user_model().objects.create(
            username="scope-reviewer", email="scope-reviewer@psd401.net", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        api = APIClient()
        api.force_authenticate(reviewer)
        self.assertEqual(api.get(f"/api/staff/requests/{self.request.number}/scoping/").status_code, 403)
