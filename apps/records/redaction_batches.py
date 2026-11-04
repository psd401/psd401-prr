# ABOUTME: Group acceptance of redaction proposals after a sampled check (#40),
# ABOUTME: and one action that builds every ready redacted PDF on a case.
"""Redaction proposals, a group at a time.

A group is the pending proposals on one case with one exemption and one
pattern (``RedactionProposal.Pattern``). With ``PRR_TRUST_REDACTION_MODE`` set
to ``batch`` (an in-app setting: admin-only, with a reason; there is no
automatic mode), a reviewer may accept a group after a random sample of
``PRR_TRUST_BATCH_SAMPLE_SIZE`` proposals: they accept or reject each sampled
proposal, a rejection carrying its reason. Only if every sampled proposal is
accepted are the rest accepted. Every acceptance goes through
``redaction_ai.accept_redaction_proposal`` and therefore ``create_redaction``
with the proposal's own box and exemption citation (rule 3), and the record's
second-review rule follows from that exemption as it always has. One
rejection blocks the group: the sample decisions stand and the rest stay
pending for one-at-a-time review. A proposal that became undecidable since
the batch opened (decided, record no longer responsive, source changed) is
skipped and counted, never forced.

``build_ready_redacted_records`` renders the redacted PDF for every responsive
record on the case whose active redactions are not yet rendered and whose
required second review is done.
"""

import random
from collections import defaultdict
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .models import (
    PublicRecordsRequest,
    RecordItem,
    RedactionBatch,
    RedactionProposal,
    RedactionProposalDecision,
)
from .redaction_ai import accept_redaction_proposal, reject_redaction_proposal
from .review import _require_reviewer, render_redacted_record

MAX_GROUP_PROPOSALS = 2000
_random = random.SystemRandom()


def _pending(request: PublicRecordsRequest):
    return (
        RedactionProposal.objects.filter(
            record_item__request=request,
            record_item__review_state=RecordItem.ReviewState.RESPONSIVE,
        )
        .exclude(
            Exists(RedactionProposalDecision.objects.filter(proposal=OuterRef("pk")))
        )
        .select_related("exemption")
    )


def proposal_groups(request: PublicRecordsRequest) -> list[dict[str, Any]]:
    """Pending proposals by exemption and pattern, largest group first."""
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    records: dict[tuple[int, str], set] = defaultdict(set)
    for proposal in _pending(request).order_by("pk"):
        key = (proposal.exemption_id, proposal.pattern)
        group = groups.setdefault(key, {
            "exemption_id": proposal.exemption_id,
            "exemption_citation": proposal.exemption.citation,
            "exemption_name": proposal.exemption.short_name,
            "requires_second_review": proposal.exemption.requires_second_review,
            "pattern": proposal.pattern,
            "pattern_label": RedactionProposal.Pattern(proposal.pattern).label,
            "pending": 0,
            "records": 0,
        })
        group["pending"] += 1
        records[key].add(proposal.record_item_id)
    for key, group in groups.items():
        group["records"] = len(records[key])
    return sorted(groups.values(), key=lambda g: (-g["pending"], g["exemption_citation"], g["pattern"]))


@transaction.atomic
def create_redaction_batch(request, *, exemption_id, pattern, actor) -> RedactionBatch:
    """Open a group batch and draw its sample. Opening decides nothing."""
    _require_reviewer(actor)
    if runtime.get("PRR_TRUST_REDACTION_MODE") != "batch":
        raise ValidationError("The trust policy does not allow accepting proposals as a group.")
    PublicRecordsRequest.objects.select_for_update().filter(pk=request.pk).first()
    ids = [
        str(pk)
        for pk in _pending(request)
        .filter(exemption_id=exemption_id, pattern=pattern)
        .order_by("pk")
        .values_list("pk", flat=True)
    ]
    if not ids:
        raise ValidationError("No pending proposal is left in this group.")
    if len(ids) > MAX_GROUP_PROPOSALS:
        raise ValidationError(
            f"One group covers at most {MAX_GROUP_PROPOSALS} proposals."
        )
    for open_batch in RedactionBatch.objects.filter(
        request=request, exemption_id=exemption_id, pattern=pattern,
        outcome=RedactionBatch.Outcome.OPEN,
    ):
        if open_batch.proposal_ids == ids:
            return open_batch
    size = runtime.get("PRR_TRUST_BATCH_SAMPLE_SIZE")
    batch = RedactionBatch.objects.create(
        request=request,
        exemption_id=exemption_id,
        pattern=pattern,
        proposal_ids=ids,
        sample_ids=sorted(_random.sample(ids, min(size, len(ids)))),
        created_by=actor,
    )
    audit(
        "redaction.batch_created",
        target=request.number,
        actor=actor,
        batch_id=batch.pk,
        exemption_id=exemption_id,
        pattern=pattern,
        proposal_count=len(ids),
        sample_size=len(batch.sample_ids),
    )
    return batch


def _accept(proposal_id, actor) -> bool:
    """Accept one proposal as proposed; False if it is no longer decidable."""
    proposal = RedactionProposal.objects.get(pk=proposal_id)
    try:
        with transaction.atomic():
            accept_redaction_proposal(
                proposal_id,
                actor=actor,
                page_number=proposal.page_number,
                x0=proposal.x0,
                y0=proposal.y0,
                x1=proposal.x1,
                y1=proposal.y1,
                exemption_id=proposal.exemption_id,
            )
    except ValidationError:
        return False
    return True


@transaction.atomic
def decide_redaction_batch(batch_id, *, actor, judgments: dict) -> RedactionBatch:
    """Record the sampled decisions, then accept the group or block it."""
    _require_reviewer(actor)
    batch = (
        RedactionBatch.objects.select_for_update()
        .select_related("request", "exemption")
        .get(pk=batch_id)
    )
    if batch.outcome != RedactionBatch.Outcome.OPEN:
        raise ValidationError("This batch has already been decided.")
    judgments = {str(pk): value for pk, value in (judgments or {}).items()}
    if set(judgments) != set(batch.sample_ids):
        raise ValidationError("Decide every sampled proposal, and only those.")
    for value in judgments.values():
        if not isinstance(value, dict) or not isinstance(value.get("accept"), bool):
            raise ValidationError("Each sampled proposal is accepted or rejected.")
        if not value["accept"] and len(str(value.get("reason") or "").strip()) < 10:
            raise ValidationError("A substantive reason is required to reject a proposal.")

    for pk in batch.sample_ids:
        if judgments[pk]["accept"]:
            if not _accept(pk, actor):
                raise ValidationError(
                    "A sampled proposal can no longer be accepted; reopen the group."
                )
        else:
            reject_redaction_proposal(pk, actor=actor, reason=judgments[pk]["reason"])

    agreed = all(value["accept"] for value in judgments.values())
    accepted = sum(1 for value in judgments.values() if value["accept"])
    skipped = 0
    if agreed:
        for pk in batch.proposal_ids:
            if pk in judgments:
                continue
            if _accept(pk, actor):
                accepted += 1
            else:
                skipped += 1

    batch.judgments = judgments
    batch.outcome = RedactionBatch.Outcome.APPLIED if agreed else RedactionBatch.Outcome.BLOCKED
    batch.accepted_count = accepted if agreed else 0
    batch.skipped_count = skipped
    batch.decided_by = actor
    batch.decided_at = timezone.now()
    batch.save(update_fields=[
        "judgments", "outcome", "accepted_count", "skipped_count", "decided_by", "decided_at",
    ])
    audit(
        "redaction.batch_decided",
        target=batch.request.number,
        actor=actor,
        batch_id=batch.pk,
        outcome=batch.outcome,
        exemption_id=batch.exemption.pk,
        pattern=batch.pattern,
        sample_size=len(batch.sample_ids),
        rejected_in_sample=sum(1 for value in judgments.values() if not value["accept"]),
        accepted_count=batch.accepted_count,
        skipped_count=skipped,
    )
    return batch


def build_ready_redacted_records(request, *, actor) -> dict[str, int]:
    """Render the redacted PDF for every record on the case that is ready."""
    _require_reviewer(actor)
    built = awaiting = current = 0
    candidates = (
        RecordItem.objects.filter(
            request=request,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            redactions__voided_at__isnull=True,
        )
        .distinct()
        .order_by("pk")
    )
    for item in candidates:
        if item.second_review_required and item.second_reviewed_at is None:
            awaiting += 1
            continue
        before = item.redacted_manifest_sha256
        rendered = render_redacted_record(item.pk, actor=actor)
        if rendered.redacted_manifest_sha256 == before and before:
            current += 1
        else:
            built += 1
    audit(
        "redaction.ready_records_built",
        target=request.number,
        actor=actor,
        built=built,
        awaiting_second_review=awaiting,
        already_current=current,
    )
    return {"built": built, "awaiting_second_review": awaiting, "already_current": current}
