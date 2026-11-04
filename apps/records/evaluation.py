# ABOUTME: Compare advisory models on decisions people already made: label a
# ABOUTME: set from production outcomes, then score each candidate model on it.
"""Model-comparison evaluation (issue #32).

``build_evaluation_set`` labels examples from what staff already decided and
stores references only. ``evaluate_models`` rebuilds each example's input from
the live rows -- the same builder a live advisory uses -- sends it to every
candidate model through ``invoke_advisory_model``, validates the answer the
same way, and stores metrics. Nothing is sent unless advisory AI is on. The
candidates are not checked in the app: the production Bedrock grant
(``infra/cdk/lib/advisory-bedrock.ts``) is the allow-list, so AWS refuses a
model it does not name and every example scores as an error.

Nothing here writes an advisory run, a proposal, a status, or a message. It
only reads decisions and records how well each model would have matched them.

Labels are coarse on purpose, because the decisions they come from are:

* intake -- ``new`` (the case went on as a request), ``existing`` (merged into,
  or its message added to, another case; ``target_case_number`` names it), or
  ``not_request`` (closed as not a records request). An officer's answer to an
  intake suggestion outranks the case outcome. Undecided (``received``) cases
  are left out. Every matter kind is included; candidates never cross kinds.
* responsiveness -- ``responsive`` (reviewed responsive or withheld) or
  ``non_responsive``. Records with no searchable text are left out.

The safety metric is recall on the class whose loss hurts a requester: real
requests (``new`` or ``existing``) for intake, and responsive records for
responsiveness, where ``uncertain`` counts as kept because a person decides it.
A failed call counts as a miss.
"""

import math
import time
from collections import Counter
from typing import Any

from django.core.exceptions import ValidationError

from apps.audit.models import AuditLog, audit

from .advisory_ai import (
    AdvisoryOutputInvalid,
    AdvisoryProviderError,
    _attachment_content_blocks,
    _build_advisory_input,
    _feature_enabled,
    _received_date,
    _validate_proposal,
    invoke_advisory_model,
)
from .models import (
    AdvisoryFeedback,
    AdvisoryRun,
    EvaluationRun,
    EvaluationSet,
    PublicRecordsRequest,
    RecordItem,
    RequestStatus,
)

ERROR = "error"
DEFAULT_LIMIT = 500

_ADVISORY_KIND = {
    EvaluationSet.Kind.INTAKE: AdvisoryRun.Kind.INTAKE_CLASSIFICATION,
    EvaluationSet.Kind.RESPONSIVENESS: AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
}
_INTAKE_COARSE = {
    "new_request": "new",
    "related_to": "new",
    "follow_up": "existing",
    "duplicate_of": "existing",
    "non_request": "not_request",
    "spam": "not_request",
}
_SAFE = {
    EvaluationSet.Kind.INTAKE: ({"new", "existing"}, {"new", "existing"}),
    EvaluationSet.Kind.RESPONSIVENESS: ({"responsive"}, {"responsive", "uncertain"}),
}


def _intake_examples(limit: int) -> list[dict[str, Any]]:
    cases = list(
        PublicRecordsRequest.objects.exclude(status=RequestStatus.RECEIVED)
        .select_related("merged_into")
        .order_by("-received_at", "pk")[:limit]
    )
    answered = {}
    for feedback in (
        AdvisoryFeedback.objects.filter(
            advisory_run__kind=AdvisoryRun.Kind.INTAKE_CLASSIFICATION,
            advisory_run__request__in=cases,
        )
        .select_related("advisory_run")
        .order_by("created_at")
    ):
        run = feedback.advisory_run
        coarse = _INTAKE_COARSE.get(feedback.final_label)
        if coarse is None:
            continue
        target = (
            run.proposal.get("target_case_number")
            if feedback.outcome == AdvisoryFeedback.Outcome.CONFIRMED
            else None
        )
        answered[str(run.request_id)] = (coarse, target if coarse == "existing" else None)

    closed_numbers = [
        case.number for case in cases if case.status == RequestStatus.CLOSED_NON_REQUEST
    ]
    redirected = {
        row.detail.get("source_request"): row.detail.get("destination_request")
        for row in AuditLog.objects.filter(
            action="records.email_intake_redirected",
            detail__source_request__in=closed_numbers,
        )
    }
    examples = []
    for case in cases:
        if str(case.pk) in answered:
            label, target = answered[str(case.pk)]
        elif case.status == RequestStatus.CLOSED_DUPLICATE and case.merged_into:
            label, target = "existing", case.merged_into.number
        elif case.status == RequestStatus.CLOSED_NON_REQUEST:
            target = redirected.get(case.number)
            label = "existing" if target else "not_request"
        else:
            label, target = "new", None
        examples.append({"request_id": str(case.pk), "label": label, "target_case_number": target})
    return examples


def _responsiveness_examples(limit: int) -> list[dict[str, Any]]:
    states = {
        RecordItem.ReviewState.RESPONSIVE: "responsive",
        RecordItem.ReviewState.WITHHELD: "responsive",
        RecordItem.ReviewState.NON_RESPONSIVE: "non_responsive",
    }
    items = (
        RecordItem.objects.filter(review_state__in=states)
        .exclude(ocr_text__regex=r"^\s*$")
        .order_by("-reviewed_at", "pk")
        .values("pk", "request_id", "review_state")[:limit]
    )
    return [
        {
            "request_id": str(row["request_id"]),
            "record_item_id": str(row["pk"]),
            "label": states[row["review_state"]],
        }
        for row in items
    ]


def build_evaluation_set(kind: str, *, actor, limit: int = DEFAULT_LIMIT) -> EvaluationSet:
    if limit < 1:
        raise ValidationError("The evaluation set limit must be at least 1.")
    if kind == EvaluationSet.Kind.INTAKE:
        examples = _intake_examples(limit)
    elif kind == EvaluationSet.Kind.RESPONSIVENESS:
        examples = _responsiveness_examples(limit)
    else:
        raise ValidationError(
            f"No evaluation labels are defined for {kind!r}; "
            f"supported kinds: {', '.join(EvaluationSet.Kind.values)}."
        )
    counts = Counter(row["label"] for row in examples)
    evaluation_set = EvaluationSet.objects.create(
        kind=kind,
        examples=examples,
        label_counts=dict(sorted(counts.items())),
        created_by=actor,
    )
    audit(
        "advisory.evaluation_set_built",
        target=str(evaluation_set.pk),
        actor=actor,
        kind=kind,
        examples=len(examples),
        label_counts=evaluation_set.label_counts,
    )
    return evaluation_set


def _predict(evaluation_set: EvaluationSet, example: dict, model: str, usage: dict) -> tuple[str, str | None]:
    advisory_kind = _ADVISORY_KIND[evaluation_set.kind]
    run = AdvisoryRun(
        kind=advisory_kind,
        request_id=example["request_id"],
        record_item_id=example.get("record_item_id"),
        model_identifier=model,
        input_sha256="",
    )
    advisory_input = _build_advisory_input(run)
    raw = invoke_advisory_model(
        advisory_kind,
        advisory_input.payload,
        attachment_blocks=_attachment_content_blocks(advisory_input.attachments),
        model_identifier=model,
        usage=usage,
    )
    proposal = _validate_proposal(
        advisory_kind,
        raw,
        received_date=_received_date(run.request),
        candidate_numbers={
            row["number"] for row in advisory_input.payload.get("candidate_cases", ())
        },
    )
    if evaluation_set.kind == EvaluationSet.Kind.INTAKE:
        return _INTAKE_COARSE[proposal["label"]], proposal.get("target_case_number")
    return proposal["suggested_state"], None


def _percentile(values: list[float], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)])


def _score(evaluation_set: EvaluationSet, rows: list[dict], price) -> dict[str, Any]:
    gold_safe, kept = _SAFE[evaluation_set.kind]
    confusion: dict[str, Counter] = {}
    for row in rows:
        confusion.setdefault(row["label"], Counter())[row["predicted"]] += 1
    safe_rows = [row for row in rows if row["label"] in gold_safe]
    targeted = [row for row in rows if row["label"] == "existing" and row["gold_target"]]
    latencies = [row["latency_ms"] for row in rows if row["predicted"] != ERROR]
    input_tokens = [row["input_tokens"] for row in rows if row["input_tokens"] is not None]
    output_tokens = [row["output_tokens"] for row in rows if row["output_tokens"] is not None]
    input_mean = sum(input_tokens) / len(input_tokens) if input_tokens else None
    output_mean = sum(output_tokens) / len(output_tokens) if output_tokens else None
    cost = (
        round((input_mean * price[0] + output_mean * price[1]) / 1_000_000, 6)
        if price and input_mean is not None and output_mean is not None
        else None
    )
    return {
        "items": len(rows),
        "errors": sum(row["predicted"] == ERROR for row in rows),
        "accuracy": round(sum(row["predicted"] == row["label"] for row in rows) / len(rows), 4)
        if rows else None,
        "safety_recall": round(sum(row["predicted"] in kept for row in safe_rows) / len(safe_rows), 4)
        if safe_rows else None,
        "target_accuracy": round(
            sum(row["predicted_target"] == row["gold_target"] for row in targeted) / len(targeted), 4,
        ) if targeted else None,
        "confusion": {gold: dict(counts) for gold, counts in sorted(confusion.items())},
        "input_tokens_mean": round(input_mean) if input_mean is not None else None,
        "output_tokens_mean": round(output_mean) if output_mean is not None else None,
        "cost_per_item_usd": cost,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "predictions": [
            {key: row[key] for key in ("ref", "label", "predicted", "predicted_target")}
            for row in rows
        ],
    }


def evaluate_models(
    evaluation_set: EvaluationSet,
    models: list[str],
    *,
    actor,
    prices: dict[str, tuple[float, float]] | None = None,
) -> EvaluationRun:
    """Score every model on every example. ``prices`` is optional USD per
    million input and output tokens per model; without one, cost is None."""
    # Checked before anything is sent: real request and record text reaches
    # the provider only while advisory AI is on, exactly as for a live run.
    _feature_enabled()
    results = {}
    for model in models:
        rows = []
        for example in evaluation_set.examples:
            usage: dict[str, int] = {}
            started = time.monotonic()
            try:
                predicted, predicted_target = _predict(evaluation_set, example, model, usage)
            except (AdvisoryProviderError, AdvisoryOutputInvalid, ValidationError):
                predicted, predicted_target = ERROR, None
            rows.append({
                "ref": example.get("record_item_id") or example["request_id"],
                "label": example["label"],
                "gold_target": example.get("target_case_number"),
                "predicted": predicted,
                "predicted_target": predicted_target,
                "latency_ms": (time.monotonic() - started) * 1000,
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
            })
        results[model] = _score(evaluation_set, rows, (prices or {}).get(model))
    run = EvaluationRun.objects.create(
        evaluation_set=evaluation_set,
        model_identifiers=list(models),
        results=results,
        created_by=actor,
    )
    audit(
        "advisory.evaluation_completed",
        target=str(run.pk),
        actor=actor,
        evaluation_set=str(evaluation_set.pk),
        kind=evaluation_set.kind,
        models=list(models),
        examples=len(evaluation_set.examples),
    )
    return run
