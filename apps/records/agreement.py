# ABOUTME: How often the AI's responsiveness suggestion matched the human
# ABOUTME: decision, by label and confidence band, from stored ReviewDecisions.
"""Stored AI-human agreement on record responsiveness (issue #38).

A record's standing suggestion is its latest *complete* responsiveness run.
:func:`latest_suggestion_runs` is that rule, used both by the review list and
by ``review.review_record`` when it stores a decision. What is stored is the
suggestion standing when the decision was written. That is what the reviewer
saw unless a newer run completed after their page loaded; the request does
not name the run it rendered, so that rare case is not distinguished. For
measuring the model against the human answer this is the right quantity
either way.

Agreement compares the suggested state with the human outcome, where a record
withheld in full counts as responsive (it was responsive and then exempt).
``uncertain`` hands the call to a person, so it has no agreement rate; a
``duplicate`` outcome is not a responsiveness call and is left out. Only the
latest decision on each record counts, so a corrected decision replaces the
one it corrected.
"""

from collections import defaultdict
from typing import Any

from django.db import models
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from .models import AdvisoryRun, RecordItem, ReviewDecision

#: Lower bound and label, highest first.
CONFIDENCE_BANDS = (
    (0.9, "90-100%"),
    (0.7, "70-89%"),
    (0.5, "50-69%"),
    (0.0, "below 50%"),
)
_HUMAN_LABEL = {
    RecordItem.ReviewState.RESPONSIVE: "responsive",
    RecordItem.ReviewState.WITHHELD: "responsive",
    RecordItem.ReviewState.NON_RESPONSIVE: "non_responsive",
}


def confidence_band(confidence: float | None) -> str:
    if confidence is None:
        return ""
    return next(label for floor, label in CONFIDENCE_BANDS if confidence >= floor)


def latest_suggestion_runs(record_item):
    """Complete responsiveness runs for ``record_item``, newest first.

    ``record_item`` may be an ``OuterRef`` for use in a subquery.
    """
    return AdvisoryRun.objects.filter(
        record_item=record_item,
        kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
        state=AdvisoryRun.State.COMPLETE,
    ).order_by("-created_at")


def annotate_latest_suggestion(queryset):
    """Annotate ``advisory_suggested_state`` and ``advisory_confidence``.

    Advisory only: this is the model's suggestion and its confidence, shown
    so a reviewer can choose what to look at first. Nothing here changes
    review state, and no filter on it applies a decision.
    """
    latest = latest_suggestion_runs(models.OuterRef("pk"))
    # advisory_ai._validate_proposal refuses to complete a responsiveness
    # run whose confidence is not a finite number in [0, 1], which is what
    # makes this cast safe: Postgres raises on a non-numeric cast, while
    # SQLite would quietly return 0.0.
    return queryset.annotate(
        advisory_suggested_state=models.Subquery(
            latest.annotate(
                _value=KeyTextTransform("suggested_state", "proposal"),
            ).values("_value")[:1],
            output_field=models.CharField(),
        ),
        advisory_confidence=Cast(
            models.Subquery(
                latest.annotate(
                    _value=KeyTextTransform("confidence", "proposal"),
                ).values("_value")[:1],
            ),
            output_field=models.FloatField(),
        ),
    )


def agreement_report(*, request=None) -> dict[str, Any]:
    """Agreement by suggested label and confidence band, district-wide or for
    one case."""
    decisions = ReviewDecision.objects.order_by("created_at", "pk")
    if request is not None:
        decisions = decisions.filter(request=request)
    latest = {}
    for row in decisions.values(
        "record_item_id", "review_state", "advisory_run_id",
        "advisory_label", "advisory_confidence",
    ):
        latest[row["record_item_id"]] = row

    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    without_suggestion = 0
    for row in latest.values():
        human = _HUMAN_LABEL.get(row["review_state"])
        if human is None:
            continue
        if row["advisory_run_id"] is None:
            without_suggestion += 1
            continue
        cell = counts[(row["advisory_label"], confidence_band(row["advisory_confidence"]))]
        cell[0] += 1
        cell[1] += row["advisory_label"] == human

    band_order = [label for _floor, label in CONFIDENCE_BANDS]
    rows = []
    for (label, band), (total, agreed) in sorted(
        counts.items(), key=lambda item: (item[0][0], band_order.index(item[0][1])),
    ):
        scored = label != "uncertain"
        rows.append({
            "label": label,
            "band": band,
            "decisions": total,
            "agreed": agreed if scored else None,
            "agreement": round(agreed / total, 4) if scored else None,
        })
    scored_rows = [row for row in rows if row["agreed"] is not None]
    scored = sum(row["decisions"] for row in scored_rows)
    agreed_total = sum(row["agreed"] for row in scored_rows)
    return {
        # Decisions made while a suggestion stood; ``scored`` leaves out
        # the uncertain ones, which have no right answer to agree with.
        "decisions": sum(row["decisions"] for row in rows),
        "scored": scored,
        "agreed": agreed_total,
        "agreement": round(agreed_total / scored, 4) if scored else None,
        "uncertain": sum(row["decisions"] for row in rows if row["agreed"] is None),
        "without_suggestion": without_suggestion,
        "rows": rows,
    }
