"""Coordinate-grounded AI redaction proposals and named human decisions."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import pymupdf as fitz
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import audit
from apps.configuration import runtime

from .advisory_ai import (
    AdvisoryOutputInvalid,
    AdvisoryUnsearchableRecord,
    _canonical_json,
    _require_actor,
    invoke_advisory_model,
)
from .models import (
    AdvisoryRun,
    ExemptionCitation,
    RecordItem,
    RedactionProposal,
    RedactionProposalDecision,
)
from .preview import _open_verified_pdf
from .review import create_redaction


@dataclass(frozen=True)
class _GroundedSpan:
    id: str
    page_number: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


def _normalized(value: float, origin: float, length: float) -> float:
    return round(max(0.0, min(1.0, (value - origin) / length)), 6)


def _page_spans(page, page_number: int) -> list[_GroundedSpan]:
    """Extract provider-addressable lines while retaining coordinates locally."""
    bounds = page.rect
    spans = []
    lines: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words", sort=True):
        if len(word) < 8 or not str(word[4]).strip():
            continue
        lines.setdefault((int(word[5]), int(word[6])), []).append(word)
    for ordinal, words in enumerate(lines.values(), start=1):
        text = " ".join(str(word[4]).strip() for word in words)
        display_rectangles = [
            fitz.Rect(word[:4]) * page.rotation_matrix
            for word in words
        ]
        # A small local padding covers glyph edges without asking the model to
        # manufacture geometry.
        x0 = min(rectangle.x0 for rectangle in display_rectangles) - 1.5
        y0 = min(rectangle.y0 for rectangle in display_rectangles) - 1.5
        x1 = max(rectangle.x1 for rectangle in display_rectangles) + 1.5
        y1 = max(rectangle.y1 for rectangle in display_rectangles) + 1.5
        normalized = (
            _normalized(x0, bounds.x0, bounds.width),
            _normalized(y0, bounds.y0, bounds.height),
            _normalized(x1, bounds.x0, bounds.width),
            _normalized(y1, bounds.y0, bounds.height),
        )
        if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
            continue
        spans.append(
            _GroundedSpan(
                id=f"p{page_number}s{ordinal}",
                page_number=page_number,
                text=text,
                x0=normalized[0],
                y0=normalized[1],
                x1=normalized[2],
                y1=normalized[3],
            )
        )
    return spans


def _payload_size(payload: dict[str, Any]) -> int:
    return len(_canonical_json(payload))


def _span_chunks(
    base_payload: dict[str, Any],
    spans: list[_GroundedSpan],
) -> list[list[_GroundedSpan]]:
    maximum = runtime.get("PRR_AI_MAX_INPUT_CHARACTERS")
    chunks: list[list[_GroundedSpan]] = []
    current: list[_GroundedSpan] = []
    for span in spans:
        candidate = [*current, span]
        payload = {
            **base_payload,
            "text_spans": [{"id": row.id, "text": row.text} for row in candidate],
        }
        if _payload_size(payload) <= maximum:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = []
        single_payload = {
            **base_payload,
            "text_spans": [{"id": span.id, "text": span.text}],
        }
        if _payload_size(single_payload) > maximum:
            raise ValueError("A PDF text span exceeds the bounded model input.")
        current = [span]
    if current:
        chunks.append(current)
    return chunks


def _bounded_text(value, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AdvisoryOutputInvalid(f"{field} must be text.")
    value = value.strip()
    if not value or len(value) > maximum:
        raise AdvisoryOutputInvalid(f"{field} must be bounded text.")
    return value


def _validate_candidates(
    raw: Any,
    *,
    span_map: dict[str, _GroundedSpan],
    exemption_map: dict[int, ExemptionCitation],
) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"candidates"}:
        raise AdvisoryOutputInvalid("The redaction output has unexpected fields.")
    candidates = raw["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 100:
        raise AdvisoryOutputInvalid("The redaction output must be a bounded candidate list.")
    validated = []
    used_span_ids: set[str] = set()
    for candidate in candidates:
        required = {"span_ids", "exemption_id", "confidence", "rationale"}
        if not isinstance(candidate, dict) or set(candidate) != required:
            raise AdvisoryOutputInvalid("A redaction candidate has unexpected fields.")
        span_ids = candidate["span_ids"]
        if (
            not isinstance(span_ids, list)
            or not span_ids
            or len(span_ids) > 50
            or any(not isinstance(value, str) for value in span_ids)
        ):
            raise AdvisoryOutputInvalid("Candidate span IDs must be a bounded non-empty list.")
        span_ids = list(dict.fromkeys(span_ids))
        if any(span_id not in span_map for span_id in span_ids):
            raise AdvisoryOutputInvalid("The model proposed an unknown source span.")
        if used_span_ids.intersection(span_ids):
            raise AdvisoryOutputInvalid("A source span cannot appear in multiple candidates.")
        exemption_id = candidate["exemption_id"]
        if (
            isinstance(exemption_id, bool)
            or not isinstance(exemption_id, int)
            or exemption_id not in exemption_map
        ):
            raise AdvisoryOutputInvalid("The model proposed an unknown exemption.")
        confidence = candidate["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise AdvisoryOutputInvalid("Candidate confidence must be between zero and one.")
        selected = [span_map[span_id] for span_id in span_ids]
        if len({span.page_number for span in selected}) != 1:
            raise AdvisoryOutputInvalid("A candidate cannot cross PDF pages.")
        used_span_ids.update(span_ids)
        validated.append(
            {
                "span_ids": span_ids,
                "spans": selected,
                "exemption": exemption_map[exemption_id],
                "confidence": round(float(confidence), 4),
                "rationale": _bounded_text(
                    candidate["rationale"],
                    field="rationale",
                    maximum=1000,
                ),
            }
        )
    return validated


def generate_redaction_proposals(
    run: AdvisoryRun,
    advisory_payload: dict[str, Any],
) -> tuple[dict[str, int], list[RedactionProposal]]:
    """Analyze verified PDF spans, then atomically retain grounded candidates."""
    if run.kind != AdvisoryRun.Kind.RECORD_REDACTIONS or run.record_item_id is None:
        raise ValueError("A redaction advisory requires a record target.")
    item = run.record_item
    if item.review_state != RecordItem.ReviewState.RESPONSIVE:
        raise ValidationError(
            "AI redaction review requires a human-confirmed responsive record."
        )
    exemptions = list(ExemptionCitation.objects.filter(active=True).order_by("pk"))
    exemption_map = {exemption.pk: exemption for exemption in exemptions}
    expected_ids = {
        row["id"] for row in advisory_payload.get("available_exemptions", [])
    }
    if expected_ids != set(exemption_map):
        raise ValueError("The exemption taxonomy changed during analysis.")

    document = _open_verified_pdf(item)
    grounded = []
    searchable_span_count = 0
    try:
        page_count = document.page_count
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            spans = _page_spans(page, page_number)
            searchable_span_count += len(spans)
            if not spans:
                continue
            page_base = {
                "kind": run.kind,
                "request_description": advisory_payload["request_description"],
                "page_number": page_number,
                "available_exemptions": advisory_payload["available_exemptions"],
            }
            for chunk in _span_chunks(page_base, spans):
                span_map = {span.id: span for span in chunk}
                raw = invoke_advisory_model(
                    run.kind,
                    {
                        **page_base,
                        "text_spans": [
                            {"id": span.id, "text": span.text} for span in chunk
                        ],
                    },
                    model_identifier=run.model_identifier,
                )
                grounded.extend(
                    _validate_candidates(
                        raw,
                        span_map=span_map,
                        exemption_map=exemption_map,
                    )
                )
                if len(grounded) > 1000:
                    raise AdvisoryOutputInvalid(
                        "The model returned too many redaction candidates."
                    )
    finally:
        document.close()
    if searchable_span_count == 0:
        raise AdvisoryUnsearchableRecord

    proposal_rows = []
    for candidate in grounded:
        spans = candidate["spans"]
        span_evidence = [
            {"id": span.id, "text": span.text}
            for span in spans
        ]
        proposal = RedactionProposal(
            advisory_run=run,
            record_item=item,
            page_number=spans[0].page_number,
            x0=min(span.x0 for span in spans),
            y0=min(span.y0 for span in spans),
            x1=max(span.x1 for span in spans),
            y1=max(span.y1 for span in spans),
            exemption=candidate["exemption"],
            confidence=candidate["confidence"],
            rationale=candidate["rationale"],
            source_span_ids=candidate["span_ids"],
            source_text_sha256=hashlib.sha256(
                _canonical_json(span_evidence)
            ).hexdigest(),
        )
        proposal.full_clean()
        proposal_rows.append(proposal)
    return (
        {"candidate_count": len(proposal_rows), "page_count": page_count},
        proposal_rows,
    )


def _locked_undecided_proposal(proposal_id) -> RedactionProposal:
    proposal = (
        RedactionProposal.objects.select_for_update()
        .select_related("advisory_run", "record_item__request", "exemption")
        .get(pk=proposal_id)
    )
    if RedactionProposalDecision.objects.filter(proposal=proposal).exists():
        raise ValidationError("This redaction proposal has already been decided.")
    if proposal.record_item.review_state != RecordItem.ReviewState.RESPONSIVE:
        raise ValidationError("The record is no longer human-confirmed responsive.")
    from .advisory_ai import advisory_input_sha256

    if advisory_input_sha256(proposal.advisory_run) != proposal.advisory_run.input_sha256:
        raise ValidationError(
            "The record or exemption taxonomy changed; run a new redaction advisory."
        )
    return proposal


@transaction.atomic
def accept_redaction_proposal(
    proposal_id,
    *,
    actor,
    page_number,
    x0,
    y0,
    x1,
    y1,
    exemption_id,
    explanation="",
) -> RedactionProposalDecision:
    """Apply a human-edited candidate through the normal legal service."""
    _require_actor(actor, records_officer_only=False)
    proposal = _locked_undecided_proposal(proposal_id)
    if int(page_number) != proposal.page_number:
        raise ValidationError("A proposal can be accepted only on its grounded PDF page.")
    redaction = create_redaction(
        proposal.record_item_id,
        actor=actor,
        page_number=page_number,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        exemption_id=exemption_id,
        explanation=explanation,
    )
    decision = RedactionProposalDecision(
        proposal=proposal,
        outcome=RedactionProposalDecision.Outcome.ACCEPTED,
        decided_by=actor,
        reason="",
        redaction=redaction,
    )
    decision.full_clean()
    decision.save()
    audit(
        "advisory.redaction_proposal.accepted",
        target=str(proposal.pk),
        actor=actor,
        request_number=proposal.record_item.request.number,
        advisory_run_id=str(proposal.advisory_run_id),
        redaction_id=redaction.pk,
        page_number=redaction.page_number,
        coordinates=[redaction.x0, redaction.y0, redaction.x1, redaction.y1],
        exemption_id=redaction.exemption_id,
    )
    return decision


@transaction.atomic
def reject_redaction_proposal(
    proposal_id,
    *,
    actor,
    reason,
) -> RedactionProposalDecision:
    """Retain a substantive named rejection without creating a redaction."""
    _require_actor(actor, records_officer_only=False)
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("A substantive reason is required to reject a proposal.")
    proposal = _locked_undecided_proposal(proposal_id)
    decision = RedactionProposalDecision(
        proposal=proposal,
        outcome=RedactionProposalDecision.Outcome.REJECTED,
        decided_by=actor,
        reason=reason[:500],
        redaction=None,
    )
    decision.full_clean()
    decision.save()
    audit(
        "advisory.redaction_proposal.rejected",
        target=str(proposal.pk),
        actor=actor,
        request_number=proposal.record_item.request.number,
        advisory_run_id=str(proposal.advisory_run_id),
        reason=decision.reason,
    )
    return decision
