"""Model comparison: score candidate models on decisions people already made.

An evaluation set is labelled from production decisions (a merge, a
non-request closure, a reviewed record) and holds references, never copies of
request or record text. An evaluation run sends every example to every
candidate model through the same gated provider path as a live advisory and
stores only metrics and per-item labels.
"""

import hashlib
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import AdvisoryProviderError
from apps.records.evaluation import build_evaluation_set, evaluate_models
from apps.records.models import (
    AdvisoryFeedback,
    AdvisoryRun,
    EvaluationRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
)

AI_ON = {
    "PRR_AI_ADVISORY_ENABLED": True,
    "PRR_AI_AWS_REGION": "us-west-2",
    "PRR_AI_MODEL": "us.amazon.nova-2-lite-v1:0",
    "PRR_AI_MAX_INPUT_CHARACTERS": 50_000,
}


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


class _Fixture(TestCase):
    def setUp(self):
        self.officer = _user("eval-officer", StaffRole.RECORDS_OFFICER)

    def _case(self, *, status=RequestStatus.ACKNOWLEDGED, description="Synthetic records.", **fields):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email=f"{description[:8].strip()}@example.org"),
            description=description,
        )
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(status=status, **fields)
        prr.refresh_from_db()
        return prr

    def _record(self, prr, state, text="Synthetic memo text."):
        return RecordItem.objects.create(
            request=prr,
            original_filename="memo.txt",
            storage_key=f"records/{prr.pk}/{hashlib.sha256(text.encode()).hexdigest()[:8]}",
            content_length=len(text),
            content_type="text/plain",
            sha256=hashlib.sha256(text.encode() + state.encode()).hexdigest(),
            ocr_text=text,
            review_state=state,
            withholding_exemption=(
                ExemptionCitation.objects.get_or_create(
                    citation="RCW 42.56.230(1)", short_name="Personal information",
                )[0]
                if state == RecordItem.ReviewState.WITHHELD else None
            ),
        )


class IntakeSetTests(_Fixture):
    def test_labels_come_from_what_people_did(self):
        earlier = self._case(description="Bus video from March.")
        merged = self._case(
            status=RequestStatus.CLOSED_DUPLICATE, merged_into=earlier,
            description="Same bus video again.",
        )
        junk = self._case(status=RequestStatus.CLOSED_NON_REQUEST, description="Buy our product.")
        redirected = self._case(
            status=RequestStatus.CLOSED_NON_REQUEST, description="Any update on my request?",
        )
        AuditLog.objects.create(
            action="records.email_intake_redirected",
            target=earlier.number,
            detail={"source_request": redirected.number, "destination_request": earlier.number},
        )
        undecided = self._case(status=RequestStatus.RECEIVED, description="Not yet looked at.")

        evaluation_set = build_evaluation_set("intake", actor=self.officer)

        labels = {row["request_id"]: row for row in evaluation_set.examples}
        self.assertEqual(labels[str(earlier.pk)]["label"], "new")
        self.assertEqual(labels[str(merged.pk)]["label"], "existing")
        self.assertEqual(labels[str(merged.pk)]["target_case_number"], earlier.number)
        self.assertEqual(labels[str(junk.pk)]["label"], "not_request")
        self.assertEqual(labels[str(redirected.pk)]["label"], "existing")
        self.assertEqual(labels[str(redirected.pk)]["target_case_number"], earlier.number)
        self.assertNotIn(str(undecided.pk), labels)
        self.assertEqual(evaluation_set.label_counts, {"existing": 2, "new": 1, "not_request": 1})

    def test_an_officer_answer_outranks_the_case_outcome(self):
        prr = self._case(description="Looks new.")
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.INTAKE_CLASSIFICATION, request=prr,
            model_identifier="m", input_sha256="x", state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer,
            proposal={"label": "spam", "target_case_number": None,
                      "confidence": 0.9, "evidence": "x"},
        )
        AdvisoryFeedback.objects.create(
            advisory_run=run, outcome="dismissed", final_label="non_request", actor=self.officer,
        )
        evaluation_set = build_evaluation_set("intake", actor=self.officer)
        self.assertEqual(evaluation_set.examples[0]["label"], "not_request")

    def test_the_set_holds_references_not_text(self):
        self._case(description="A distinctive synthetic sentence.")
        evaluation_set = build_evaluation_set("intake", actor=self.officer)
        self.assertNotIn("distinctive", str(evaluation_set.examples))
        for row in evaluation_set.examples:
            self.assertEqual(set(row), {"request_id", "label", "target_case_number"})


class ResponsivenessSetTests(_Fixture):
    def test_reviewed_records_with_text_are_labelled(self):
        prr = self._case()
        responsive = self._record(prr, RecordItem.ReviewState.RESPONSIVE, "Responsive memo.")
        withheld = self._record(prr, RecordItem.ReviewState.WITHHELD, "Withheld memo.")
        not_responsive = self._record(prr, RecordItem.ReviewState.NON_RESPONSIVE, "Lunch menu.")
        unreviewed = self._record(prr, RecordItem.ReviewState.UNREVIEWED, "Unreviewed.")
        blank = self._record(prr, RecordItem.ReviewState.RESPONSIVE, " ")

        evaluation_set = build_evaluation_set("responsiveness", actor=self.officer)

        labels = {row["record_item_id"]: row["label"] for row in evaluation_set.examples}
        self.assertEqual(labels, {
            str(responsive.pk): "responsive",
            str(withheld.pk): "responsive",
            str(not_responsive.pk): "non_responsive",
        })
        self.assertNotIn(str(unreviewed.pk), labels)
        self.assertNotIn(str(blank.pk), labels)

    def test_the_limit_bounds_the_set(self):
        prr = self._case()
        for index in range(5):
            self._record(prr, RecordItem.ReviewState.RESPONSIVE, f"Memo {index}.")
        evaluation_set = build_evaluation_set("responsiveness", actor=self.officer, limit=2)
        self.assertEqual(len(evaluation_set.examples), 2)

    def test_a_limit_below_one_is_refused(self):
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                build_evaluation_set("responsiveness", actor=self.officer, limit=limit)

    def test_unsupported_kinds_are_refused(self):
        with self.assertRaises(ValidationError):
            build_evaluation_set("redaction", actor=self.officer)


@override_settings(**AI_ON)
class EvaluateModelsTests(_Fixture):
    def setUp(self):
        super().setUp()
        prr = self._case()
        self._record(prr, RecordItem.ReviewState.RESPONSIVE, "Responsive memo.")
        self._record(prr, RecordItem.ReviewState.RESPONSIVE, "Second responsive memo.")
        self._record(prr, RecordItem.ReviewState.NON_RESPONSIVE, "Lunch menu.")
        self.evaluation_set = build_evaluation_set("responsiveness", actor=self.officer)

    def _fake(self, answers):
        """Answer per model: 'responsive' for every record, or by text."""
        def invoke(kind, payload, *, attachment_blocks=None, model_identifier, usage=None):
            if usage is not None:
                usage.update({"inputTokens": 1000, "outputTokens": 100})
            answer = answers[model_identifier](payload["record_text"])
            if isinstance(answer, Exception):
                raise answer
            return {
                "suggested_state": answer, "confidence": 0.8,
                "matched_terms": [], "rationale": "Synthetic.",
            }
        return invoke

    def test_every_model_is_scored_on_the_same_examples(self):
        answers = {
            "model-a": lambda text: "non_responsive" if "Lunch" in text else "responsive",
            "model-b": lambda text: "non_responsive",
        }
        with patch("apps.records.evaluation.invoke_advisory_model", side_effect=self._fake(answers)):
            run = evaluate_models(
                self.evaluation_set, ["model-a", "model-b"], actor=self.officer,
                prices={"model-a": (3.0, 15.0)},
            )

        a, b = run.results["model-a"], run.results["model-b"]
        self.assertEqual(a["items"], 3)
        self.assertEqual(a["accuracy"], 1.0)
        self.assertEqual(a["safety_recall"], 1.0)
        self.assertEqual(b["accuracy"], round(1 / 3, 4))
        self.assertEqual(b["safety_recall"], 0.0)
        self.assertEqual(b["confusion"]["responsive"]["non_responsive"], 2)
        self.assertEqual(a["input_tokens_mean"], 1000)
        # 1000 in at $3/M plus 100 out at $15/M.
        self.assertEqual(a["cost_per_item_usd"], 0.0045)
        self.assertIsNone(b["cost_per_item_usd"])
        self.assertIn("latency_p95_ms", a)
        self.assertTrue(AuditLog.objects.filter(
            action="advisory.evaluation_completed", target=str(run.pk),
        ).exists())

    def test_uncertain_counts_toward_safety_recall_but_not_accuracy(self):
        answers = {"model-u": lambda text: "uncertain"}
        with patch("apps.records.evaluation.invoke_advisory_model", side_effect=self._fake(answers)):
            run = evaluate_models(self.evaluation_set, ["model-u"], actor=self.officer)
        self.assertEqual(run.results["model-u"]["safety_recall"], 1.0)
        self.assertEqual(run.results["model-u"]["accuracy"], 0.0)

    def test_a_failed_call_is_an_error_and_a_miss(self):
        answers = {"model-x": lambda text: AdvisoryProviderError("provider_rejected")}
        with patch("apps.records.evaluation.invoke_advisory_model", side_effect=self._fake(answers)):
            run = evaluate_models(self.evaluation_set, ["model-x"], actor=self.officer)
        result = run.results["model-x"]
        self.assertEqual(result["errors"], 3)
        self.assertEqual(result["safety_recall"], 0.0)
        self.assertEqual(result["confusion"]["responsive"]["error"], 2)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    def test_nothing_is_sent_while_advisory_ai_is_off(self):
        with patch("apps.records.evaluation.invoke_advisory_model") as invoke:
            with self.assertRaises(ValidationError):
                evaluate_models(self.evaluation_set, ["model-a"], actor=self.officer)
        invoke.assert_not_called()
        self.assertFalse(EvaluationRun.objects.exists())


@override_settings(**AI_ON)
class IntakeEvaluationTests(_Fixture):
    def test_target_accuracy_is_scored_for_cases_that_belong_elsewhere(self):
        earlier = self._case(description="Board minutes.")
        self._case(
            status=RequestStatus.CLOSED_DUPLICATE, merged_into=earlier,
            description="Board minutes please.",
        )
        evaluation_set = build_evaluation_set("intake", actor=self.officer)

        def invoke(kind, payload, *, attachment_blocks=None, model_identifier, usage=None):
            numbers = [row["number"] for row in payload["candidate_cases"]]
            if numbers:
                return {"label": "duplicate_of", "target_case_number": numbers[0],
                        "confidence": 0.9, "evidence": "Same."}
            return {"label": "new_request", "target_case_number": None,
                    "confidence": 0.9, "evidence": "New."}

        with patch("apps.records.evaluation.invoke_advisory_model", side_effect=invoke):
            run = evaluate_models(evaluation_set, ["model-a"], actor=self.officer)

        result = run.results["model-a"]
        self.assertEqual(result["accuracy"], 1.0)
        self.assertEqual(result["target_accuracy"], 1.0)
        self.assertEqual(result["safety_recall"], 1.0)


@override_settings(**AI_ON)
class CommandTests(_Fixture):
    def test_build_then_evaluate(self):
        prr = self._case()
        self._record(prr, RecordItem.ReviewState.RESPONSIVE)
        out = StringIO()
        call_command(
            "build_eval_set", "--kind", "responsiveness",
            "--actor", "eval-officer@psd401.net", stdout=out,
        )
        set_id = out.getvalue().split()[-1]

        def invoke(kind, payload, *, attachment_blocks=None, model_identifier, usage=None):
            return {"suggested_state": "responsive", "confidence": 0.9,
                    "matched_terms": [], "rationale": "x"}

        out = StringIO()
        with patch("apps.records.evaluation.invoke_advisory_model", side_effect=invoke):
            call_command(
                "evaluate_models", "--set", set_id, "--models", "model-a,model-b",
                "--price", "model-a=3/15", "--actor", "eval-officer@psd401.net", stdout=out,
            )
        run = EvaluationRun.objects.get()
        self.assertEqual(sorted(run.results), ["model-a", "model-b"])
        self.assertIn("model-a", out.getvalue())


class EvaluationApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        prr = self._case()
        self._record(prr, RecordItem.ReviewState.RESPONSIVE)
        evaluation_set = build_evaluation_set("responsiveness", actor=self.officer)
        EvaluationRun.objects.create(
            evaluation_set=evaluation_set, model_identifiers=["model-a"],
            results={"model-a": {"items": 1, "accuracy": 1.0}}, created_by=self.officer,
        )
        self.api = APIClient()

    def test_an_officer_reads_the_runs(self):
        self.api.force_authenticate(self.officer)
        response = self.api.get("/api/staff/advisory-evaluations/")
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()[0]
        self.assertEqual(row["kind"], "responsiveness")
        self.assertEqual(row["items"], 1)
        self.assertEqual(row["results"]["model-a"]["accuracy"], 1.0)

    def test_runs_are_listed_only(self):
        # The screen and the registered capability are the list; a detail
        # route would be an ungated second read nobody registered.
        from apps.api.resources.advisory_evaluations import EvaluationRunViewSet
        self.assertFalse(hasattr(EvaluationRunViewSet, "retrieve"))

    def test_a_reviewer_may_not(self):
        self.api.force_authenticate(_user("eval-reviewer", StaffRole.REVIEWER))
        self.assertEqual(self.api.get("/api/staff/advisory-evaluations/").status_code, 403)
