# ABOUTME: Declarative eval cases for the MCP tools, run over a real session.
# ABOUTME: Each case states the keys a response must carry and must never carry.
"""Eval suite for the PRR MCP tools (psd-dev-standards standards/07-mcp.md §6).

The cases here are data, not tests. ``apps/api/tests/test_mcp_evals.py`` seeds
one request with a collection task, opens a real streamable-HTTP MCP session
against the mounted application, and runs every case through it. Keeping the
cases in the package rather than in the test file is what lets the same list
be read by a reviewer, printed in the README, and -- when the gateway lands --
replayed against a deployed endpoint instead of an in-process one.

An eval case is deliberately not a unit test of a Python method. It asserts
the things a *model* depends on and that a refactor can silently break:

* the tool answers at all, through the wire, with the arguments as documented;
* the response carries the keys an agent is told to read (``required_keys``);
* the response does **not** carry fields the tool is supposed to withhold
  (``forbidden_keys``) -- a field the data owner has not signed off on sending
  to a caller's model provider, for instance; and
* a wrong argument produces an error that *teaches* the right vocabulary
  (``error_contains``) rather than a bare stack trace, which is build
  standard 3's explicit ask.

Write-tool behaviour (that a draft stays a draft, that a proposal applies
nothing) is proved in ``apps/api/tests/test_mcp_write_tools.py`` against real
database rows, which is a stronger claim than an eval can make; the one write
case below exists to prove the *draft path answers over MCP*, not to re-prove
the guardrails.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Placeholder the runner substitutes with the seeded request's public number.
REQUEST_NUMBER = "{request_number}"

#: Placeholder the runner substitutes with the seeded collection task's id.
TASK_ID = "{task_id}"

#: Placeholder the runner substitutes with an ISO date safely in the future.
FUTURE_DATE = "{future_date}"


@dataclass(frozen=True)
class EvalCase:
    """One tool call and what its response has to be true of."""

    #: Short identifier, used as the subtest label.
    name: str
    #: The wire tool name, as ``tools/list`` reports it.
    tool: str
    #: Arguments as an agent would send them. String values equal to one of
    #: the placeholders above are resolved by the runner.
    arguments: dict = field(default_factory=dict)
    #: Why this case is worth running. Read by humans, printed on failure.
    rationale: str = ""
    #: True when the call is expected to come back as an MCP error.
    expect_error: bool = False
    #: JSON object keys that must appear somewhere in the parsed response.
    #: Matched against real keys at any depth, never against the raw text: a
    #: key name that only happens to occur inside a letter body is not a
    #: field, and an eval that accepted one would pass on a broken response.
    required_keys: tuple[str, ...] = ()
    #: JSON object keys that must not appear at any depth. This is the PII
    #: assertion, so it has to be recursive -- a requester address nested one
    #: level down is still disclosed.
    forbidden_keys: tuple[str, ...] = ()
    #: Dotted paths to exact expected values, e.g. ``{"status": "draft"}`` or
    #: ``{"proposal.instructions": "..."}``. This is how a case states that a
    #: draft stayed a draft, which key presence alone cannot say: every
    #: timestamp field is present on a draft and simply null.
    required_values: dict = field(default_factory=dict)
    #: Substrings an expected error must contain, case-insensitively.
    error_contains: tuple[str, ...] = ()


READ_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="requests_list_returns_a_paginated_envelope",
        tool="psd_prr_requests_list",
        rationale=(
            "The queue browser is the entry point for every agent workflow, "
            "and agents are told to page through `results`/`next`."
        ),
        required_keys=("results", "count"),
    ),
    EvalCase(
        name="requests_list_filters_by_an_exact_status_code",
        tool="psd_prr_requests_list",
        arguments={"status": "acknowledged"},
        rationale=(
            "The documented status vocabulary is exact PRA status codes; a "
            "filter that silently ignored an unknown value would make an "
            "agent believe the queue was empty."
        ),
        required_keys=("results",),
    ),
    EvalCase(
        name="request_get_returns_the_full_status_timeline",
        tool="psd_prr_request_get",
        arguments={"number": REQUEST_NUMBER},
        rationale=(
            "`events` is the staff-visible compliance timeline. An agent "
            "reasoning about a deadline reads it, so its absence is a defect "
            "even when the call succeeds."
        ),
        required_keys=("number", "events", "status"),
    ),
    EvalCase(
        name="request_get_rejects_a_path_escape_before_any_http",
        tool="psd_prr_request_get",
        arguments={"number": "../record-items"},
        rationale=(
            "The number is interpolated into a REST path. The guard belongs "
            "in front of the request, and the error has to name what a "
            "request number is."
        ),
        expect_error=True,
        error_contains=("request number",),
    ),
    EvalCase(
        name="overdue_list_reports_its_own_bounds",
        tool="psd_prr_overdue_requests_list",
        rationale=(
            "A statutory-deadline count that cannot say whether it is "
            "complete is a guess. `total` is the number overdue and is never "
            "capped by `limit`; `truncated` and `scan_complete` are what "
            "keep an agent from reporting a bounded page as a total."
        ),
        required_keys=(
            "results",
            "count",
            "total",
            "truncated",
            "scan_complete",
            "limit",
            "pages_scanned",
            "response_format",
            "as_of",
        ),
    ),
    EvalCase(
        name="overdue_list_count_is_not_capped_by_the_row_limit",
        tool="psd_prr_overdue_requests_list",
        arguments={"limit": 1},
        rationale=(
            "The regression this guards is the one that matters most here: "
            "if `limit` also capped `total`, an officer asking how many "
            "requests are overdue would be told the page size. The fixture "
            "has exactly one overdue request, so `total` must be 1 and the "
            "scan must report itself complete with nothing truncated."
        ),
        required_values={
            "total": 1,
            "count": 1,
            "truncated": False,
            "scan_complete": True,
        },
    ),
    EvalCase(
        name="overdue_list_concise_carries_every_promised_field",
        tool="psd_prr_overdue_requests_list",
        arguments={"response_format": "concise"},
        rationale=(
            "The required keys are every field the concise projection "
            "promises, so a field dropped from OVERDUE_CONCISE_FIELDS is "
            "caught here. This checks key presence only; a serializer "
            "rename shows up as a null value, not a failure. "
            "`subject`, `requester_name` and `requester_email` are among "
            "them because the data owner (Hagel, CIO) signed off on "
            "2026-10-19 that MCP tool results may carry them to the "
            "caller's model provider."
        ),
        required_keys=(
            "days_overdue",
            "statutory_response_due",
            "number",
            "subject",
            "status",
            "matter_kind",
            "next_due",
            "next_due_basis",
            "assigned_officer_username",
            "requester_name",
            "requester_email",
        ),
    ),
    EvalCase(
        name="overdue_list_detailed_returns_the_full_summary_row",
        tool="psd_prr_overdue_requests_list",
        arguments={"response_format": "detailed", "limit": 5},
        rationale=(
            "The second documented format has to actually differ from the "
            "first, or the parameter is decoration. `subject_source` is in "
            "the full staff row and not in the concise projection."
        ),
        required_keys=("subject_source", "days_overdue"),
    ),
    EvalCase(
        name="overdue_list_teaches_the_response_format_vocabulary",
        tool="psd_prr_overdue_requests_list",
        arguments={"response_format": "summary"},
        rationale=(
            "Build standard 3 asks for errors that teach. A model that "
            "guessed 'summary' must learn the two real values. Over MCP the "
            "rejection comes from the parameter's two-value Literal, not "
            "from PRRClient's own check, so this asserts that the vocabulary "
            "reaches the caller rather than matching one wording."
        ),
        expect_error=True,
        error_contains=("concise", "detailed"),
    ),
    EvalCase(
        name="overdue_list_teaches_the_limit_range",
        tool="psd_prr_overdue_requests_list",
        arguments={"limit": 5000},
        rationale=(
            "The ceiling exists so one call cannot ask for the whole "
            "backlog; the error has to say what the ceiling is."
        ),
        expect_error=True,
        error_contains=("limit must be",),
    ),
    EvalCase(
        name="collection_tasks_list_scopes_to_one_request",
        tool="psd_prr_collection_tasks_list",
        arguments={"request_number": REQUEST_NUMBER},
        rationale=(
            "Adequate-search reasoning depends on seeing which systems were "
            "assigned for this request and whether the work is complete."
        ),
        required_keys=("results",),
    ),
    EvalCase(
        name="record_systems_list_returns_the_registry",
        tool="psd_prr_record_systems_list",
        rationale=(
            "The registry is what routes collection (rule 8); custodians and "
            "retention windows are the fields that decide a footage hold."
        ),
        required_keys=("results",),
    ),
    EvalCase(
        name="exemptions_list_returns_the_citation_taxonomy",
        tool="psd_prr_exemptions_list",
        rationale=(
            "Rule 3 requires a real citation FK behind every withholding, so "
            "an agent proposing one must be able to look the taxonomy up."
        ),
        required_keys=("results",),
    ),
    EvalCase(
        name="audit_log_search_scopes_to_one_target",
        tool="psd_prr_audit_log_search",
        arguments={"target": REQUEST_NUMBER},
        rationale=(
            "The audit log is append-only (rule 4) and is the answer to 'who "
            "did what'. Scoped search is how an agent cites it."
        ),
        required_keys=("results",),
    ),
    EvalCase(
        name="capabilities_list_returns_the_parity_contract",
        tool="psd_prr_capabilities_list",
        rationale=(
            "Rule 10's contract is how an agent discovers which outcomes are "
            "direct, which need confirmation, and which are human-only. It "
            "is the first call a well-behaved agent makes."
        ),
        required_keys=("capabilities",),
    ),
)

WRITE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="acknowledgment_draft_answers_over_mcp_and_sends_nothing",
        tool="psd_prr_acknowledgment_draft",
        arguments={"request_number": REQUEST_NUMBER},
        rationale=(
            "The statutory RCW 42.56.520 letter, and the backlog an officer "
            "most needs an agent's help with. The response must show an "
            "unapproved, unqueued, unsent draft: `acknowledged` follows a "
            "human-approved delivery, so a draft that reported itself sent "
            "would be a false compliance signal. `automated` must be false "
            "because rule 6 allows exactly two automated requester-facing "
            "paths and this is neither."
        ),
        required_keys=("status", "request_number", "letter_text"),
        # The letter quotes the request description in full, which is
        # unbounded requester text. `forbidden_keys` is the PII assertion and
        # is recursive, so this states that no rendered body reaches the
        # caller's model provider by any nesting.
        forbidden_keys=(
            "draft_body",
            "draft_subject",
            "approved_body",
            "snippet",
        ),
        required_values={
            "status": "draft",
            "automated": False,
            "approved_at": None,
            "queued_at": None,
            "sent_at": None,
            "approved_by": None,
            "queued_by": None,
        },
    ),
    EvalCase(
        name="acknowledgment_draft_teaches_the_date_format",
        tool="psd_prr_acknowledgment_draft",
        arguments={
            "request_number": REQUEST_NUMBER,
            "estimated_completion": "next Friday",
        },
        rationale=(
            "The optional estimate is a date, and a model that guessed prose "
            "has to learn the format rather than see a parser traceback."
        ),
        expect_error=True,
        error_contains=("yyyy-mm-dd",),
    ),
    EvalCase(
        name="clarification_draft_answers_over_mcp_and_applies_nothing",
        tool="psd_prr_clarification_letter_draft",
        arguments={
            "request_number": REQUEST_NUMBER,
            "clarification_questions": (
                "Which school years should the search cover?"
            ),
            "clarification_due": FUTURE_DATE,
        },
        rationale=(
            "The only hosted tool that writes a row. The response must show "
            "an unapproved, unqueued, unsent draft: rule 6 keeps every "
            "requester-facing send human-only, and rule 6 also forbids a "
            "second automated requester path, so `automated` must be false."
        ),
        required_keys=("status", "draft_body", "request_number"),
        required_values={
            "status": "draft",
            "automated": False,
            "approved_at": None,
            "queued_at": None,
            "sent_at": None,
            "approved_by": None,
            "queued_by": None,
        },
    ),
    EvalCase(
        name="collection_task_notes_proposal_says_it_applied_nothing",
        tool="psd_prr_collection_task_notes_propose",
        arguments={
            "task_id": TASK_ID,
            "proposed_instructions": "Also search the archived mailbox.",
        },
        rationale=(
            "The tool is named `_propose` because it changes nothing. "
            "`applied: false` and the named human gate are what an agent "
            "must relay instead of reporting the note as updated."
        ),
        required_keys=("applied", "proposal", "human_gate", "task"),
        required_values={
            "applied": False,
            "proposal.instructions": "Also search the archived mailbox.",
            "task.current_instructions": "Search the original date range.",
            "human_gate.required": True,
            "human_gate.rest_primitive.method": "PATCH",
        },
    ),
)

CASES: tuple[EvalCase, ...] = READ_CASES + WRITE_CASES
