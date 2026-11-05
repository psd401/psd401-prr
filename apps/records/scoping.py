# ABOUTME: Iterative scoping (#36): count the intent agent's proposed query,
# ABOUTME: let it read the total and custodian breakdown, tighten, count again.
"""Iterative scoping for a request, within rule 11's count-only permission.

``start_scoping`` counts the request-intent proposal: its required and
alternative terms, its ``sender_domains`` and ``exclude_senders`` in Vault
Gmail syntax, and -- when it names district custodians -- only those
mailboxes (the ``accounts`` scope); otherwise the whole district. When a count
finishes, ``continue_scoping`` (queued by vault_counts) shows the agent every
iteration so far -- query, total, and the busiest custodian accounts (Vault
counts give no sender breakdown) -- with the district's noise senders, and the
agent either stops or proposes a tighter query with its reason, which is
counted next. It stops at ``PRR_INTENT_MAX_COUNT_ITERATIONS`` (in-app,
default 4), when the agent says stop, when a refinement is unusable or
repeats an earlier scope, or when a count fails; the last completed count is
the proposed scope.

Counts only. Nothing here holds, exports, collects, writes a status, or
reaches a requester. ``rerun_scoping`` lets an officer start again with a
reason even when nothing changed.
"""

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .advisory_ai import (
    SCOPE_REFINEMENT,
    AdvisoryOutputInvalid,
    AdvisoryProviderError,
    _feature_enabled,
    invoke_advisory_model,
    model_for_kind,
    validate_scope_refinement,
)
from .models import (
    AdvisoryRun,
    KnowledgeNoiseSender,
    PublicRecordsRequest,
    ScopeIteration,
    ScopingSession,
    VaultCountRun,
)
from .vault_counts import (
    VaultCountAlreadyRunning,
    proposal_date_range,
    request_vault_count,
    vault_count_enabled,
    vault_query_from_intent,
)

logger = logging.getLogger(__name__)
_SCOPE_KEYS = (
    "required_search_terms", "search_terms", "custodians", "sender_domains", "exclude_senders",
)


def _district_custodians(custodians) -> list[str]:
    domain = str(runtime.get("PRR_GOOGLE_HOSTED_DOMAIN") or "").lower()
    return [
        email for email in dict.fromkeys(str(c).strip().lower() for c in custodians or ())
        if domain and email.endswith(f"@{domain}")
    ][:100]


def _queue_iteration(session, *, number, scope, reason, dates, actor, trigger) -> ScopeIteration:
    custodians = _district_custodians(scope.get("custodians"))
    terms = vault_query_from_intent(
        scope.get("required_search_terms"),
        scope.get("search_terms"),
        sender_domains=scope.get("sender_domains") or (),
        exclude_senders=scope.get("exclude_senders") or (),
    )
    if not terms:
        raise ValidationError("The proposed scope has no search terms to count.")
    start, end = dates
    run = request_vault_count(
        session.request_id,
        actor=actor,
        terms=terms[:1000],
        custodian_emails=custodians,
        search_scope=(
            VaultCountRun.SearchScope.ACCOUNTS if custodians else VaultCountRun.SearchScope.ENTIRE_ORG
        ),
        date_range_start=start,
        date_range_end=end,
        trigger=trigger,
    )
    iteration = ScopeIteration.objects.create(
        session=session,
        number=number,
        count_run=run,
        reason=str(reason or "")[:1000],
        required_search_terms=list(scope.get("required_search_terms") or []),
        search_terms=list(scope.get("search_terms") or []),
        custodians=custodians,
        sender_domains=list(scope.get("sender_domains") or []),
        exclude_senders=list(scope.get("exclude_senders") or []),
    )
    audit(
        "scoping.iteration_queued",
        target=session.request.number,
        actor=actor,
        session_id=session.pk,
        iteration=number,
        count_run_id=str(run.pk),
        search_scope=run.search_scope,
        custodian_count=len(custodians),
    )
    return iteration


def start_scoping(request: PublicRecordsRequest, proposal: dict, *, trigger, actor=None, reason=""):
    """Open a scoping session and count the proposal (iteration 1)."""
    if not vault_count_enabled():
        raise ValidationError("Google Vault counting is not configured.")
    session = ScopingSession.objects.create(
        request=request,
        trigger=trigger,
        actor=actor,
        reason=str(reason or "")[:500],
        max_iterations=runtime.get("PRR_INTENT_MAX_COUNT_ITERATIONS"),
    )
    audit(
        "scoping.session_started",
        target=request.number,
        actor=actor,
        session_id=session.pk,
        trigger=trigger,
        reason=session.reason,
    )
    try:
        _queue_iteration(
            session,
            number=1,
            scope=proposal,
            reason=proposal.get("scope_rationale") or "The intent proposal's scope.",
            dates=proposal_date_range(request, proposal),
            actor=actor,
            trigger=(
                VaultCountRun.Trigger.MANUAL if actor is not None else VaultCountRun.Trigger.AUTOMATIC
            ),
        )
    except (ValidationError, VaultCountAlreadyRunning) as exc:
        # Never leave a session "running" with nothing counting. (Not one
        # transaction: the count is queued as it is created, so the worker
        # must be able to read it straight away.)
        _finish(session, ScopingSession.State.FAILED, f"The first count could not start: {exc}")
        raise
    return session


def rerun_scoping(request: PublicRecordsRequest, *, actor, reason: str):
    """An officer's fresh scoping run from the latest intent proposal, even
    when nothing about the request or proposal changed."""
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("Say why scoping is being run again (at least 10 characters).")
    intent = (
        AdvisoryRun.objects.filter(
            request=request,
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
        )
        .order_by("-created_at")
        .first()
    )
    if intent is None:
        raise ValidationError("Scoping starts from a completed intent proposal; run one first.")
    ScopingSession.objects.filter(
        request=request, state=ScopingSession.State.RUNNING,
    ).update(
        state=ScopingSession.State.COMPLETE,
        stop_reason="Replaced by a re-run.",
        completed_at=timezone.now(),
    )
    return start_scoping(
        request, intent.proposal, trigger=ScopingSession.Trigger.MANUAL,
        actor=actor, reason=reason,
    )


def _finish(session, state, stop_reason):
    session.state = state
    session.stop_reason = stop_reason[:500]
    session.completed_at = timezone.now()
    session.save(update_fields=["state", "stop_reason", "completed_at"])
    audit(
        "scoping.session_finished",
        target=session.request.number,
        session_id=session.pk,
        state=state,
        iterations=ScopeIteration.objects.filter(session=session).count(),
        stop_reason=session.stop_reason,
    )


def _scope_key(scope: dict) -> tuple:
    """Compare scopes as they would be counted (custodians outside the
    district are never counted, so they never make a scope different)."""
    values = {key: scope.get(key) or () for key in _SCOPE_KEYS}
    values["custodians"] = _district_custodians(values["custodians"])
    return tuple(tuple(sorted(str(v).lower() for v in values[key])) for key in _SCOPE_KEYS)


def _refinement_payload(session, iterations) -> dict[str, Any]:
    return {
        "request": {
            "number": session.request.number,
            "description": session.request.description[:4000],
        },
        "district_domain": runtime.get("PRR_GOOGLE_HOSTED_DOMAIN"),
        "noise_senders": [
            {"kind": row.kind, "value": row.value}
            for row in KnowledgeNoiseSender.objects.filter(active=True).order_by("kind", "value")[:200]
        ],
        "iterations": [
            {
                "number": iteration.number,
                "query": iteration.count_run.terms,
                "scope": iteration.count_run.search_scope,
                "custodians": iteration.custodians,
                "total": iteration.count_run.total_count,
                "top_accounts": iteration.count_run.account_counts[:10],
                "reason": iteration.reason,
            }
            for iteration in iterations
        ],
    }


@transaction.atomic
def continue_scoping(session_id) -> None:
    """Take the next step after a count finishes. Idempotent."""
    session = (
        ScopingSession.objects.select_for_update().select_related("request").get(pk=session_id)
    )
    if session.state != ScopingSession.State.RUNNING:
        return
    iterations = list(
        ScopeIteration.objects.filter(session=session).select_related("count_run").order_by("number")
    )
    last = iterations[-1]
    run = last.count_run
    if run.state in (VaultCountRun.State.PENDING, VaultCountRun.State.RUNNING):
        return
    if run.state == VaultCountRun.State.FAILED:
        _finish(session, ScopingSession.State.FAILED, f"The count failed ({run.failure_code}).")
        return
    if last.number >= session.max_iterations:
        _finish(
            session, ScopingSession.State.COMPLETE,
            f"Reached the iteration limit of {session.max_iterations}; the last count stands.",
        )
        return
    try:
        _feature_enabled()
        refinement = validate_scope_refinement(
            invoke_advisory_model(
                SCOPE_REFINEMENT,
                _refinement_payload(session, iterations),
                model_identifier=model_for_kind(AdvisoryRun.Kind.REQUEST_INTENT),
            )
        )
    except (ValidationError, AdvisoryProviderError, AdvisoryOutputInvalid):
        _finish(
            session, ScopingSession.State.COMPLETE,
            "Refinement was unavailable; the last count stands.",
        )
        return
    if refinement["decision"] == "stop":
        _finish(session, ScopingSession.State.COMPLETE, refinement["reason"])
        return
    tried = {
        _scope_key({key: getattr(i, key) for key in _SCOPE_KEYS}) for i in iterations
    }
    if _scope_key(refinement) in tried:
        _finish(
            session, ScopingSession.State.COMPLETE,
            "The refinement repeated an earlier scope; the last count stands.",
        )
        return
    try:
        _queue_iteration(
            session,
            number=last.number + 1,
            scope=refinement,
            reason=refinement["reason"],
            dates=(run.date_range_start, run.date_range_end),
            actor=None,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
        )
    except (ValidationError, VaultCountAlreadyRunning) as exc:
        _finish(session, ScopingSession.State.COMPLETE, f"No further count: {exc}")
