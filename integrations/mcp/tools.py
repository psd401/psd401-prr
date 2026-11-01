# ABOUTME: FastMCP registry with shared reads and hosted-only gated write tools.
# ABOUTME: Transport-specific code supplies a PRRClient for each tool request.
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .client import CONCISE, PRRClient

#: psd-dev-standards standards/07-mcp.md build standard 3 requires
#: ``psd_<system>_<resource>_<verb>`` names. ``prr`` is this system's slug, so
#: every tool on either transport starts here. The prefix is asserted in
#: apps/api/tests/test_mcp_tool_contract.py rather than merely conventional:
#: an unnamespaced tool collides with the nineteen other district servers once
#: they sit behind one gateway.
TOOL_NAMESPACE = "psd_prr_"

#: Build standard 3's ceiling. The count is a gate, not a guideline: a server
#: past it stops being a workflow surface and becomes an API mirror, and the
#: tool list itself starts crowding a model's context.
MAX_TOOLS = 15

# One symbol per tool, used by both the registration and the dispatch below.
# Naming the wire string once is what makes the two impossible to desynchronize:
# a tool registered under one name cannot route to another tool's REST method,
# because there is only one place the name is written.
REQUESTS_LIST = "psd_prr_requests_list"
REQUEST_GET = "psd_prr_request_get"
OVERDUE_REQUESTS_LIST = "psd_prr_overdue_requests_list"
COLLECTION_TASKS_LIST = "psd_prr_collection_tasks_list"
RECORD_SYSTEMS_LIST = "psd_prr_record_systems_list"
EXEMPTIONS_LIST = "psd_prr_exemptions_list"
AUDIT_LOG_SEARCH = "psd_prr_audit_log_search"
CAPABILITIES_LIST = "psd_prr_capabilities_list"
ACKNOWLEDGMENT_DRAFT = "psd_prr_acknowledgment_draft"
CLARIFICATION_LETTER_DRAFT = "psd_prr_clarification_letter_draft"
COLLECTION_TASK_NOTES_PROPOSE = "psd_prr_collection_task_notes_propose"
EXEMPTION_CITATIONS_SUGGEST = "psd_prr_exemption_citations_suggest"

READ_TOOL_NAMES = (
    REQUESTS_LIST,
    REQUEST_GET,
    OVERDUE_REQUESTS_LIST,
    COLLECTION_TASKS_LIST,
    RECORD_SYSTEMS_LIST,
    EXEMPTIONS_LIST,
    AUDIT_LOG_SEARCH,
    CAPABILITIES_LIST,
)

WRITE_TOOL_NAMES = (
    ACKNOWLEDGMENT_DRAFT,
    CLARIFICATION_LETTER_DRAFT,
    COLLECTION_TASK_NOTES_PROPOSE,
    EXEMPTION_CITATIONS_SUGGEST,
)

#: Wire tool name -> the ``PRRClient`` method that answers it.
#:
#: Before the names were namespaced the two were the same string, so nothing
#: had to say how a tool reached the REST contract. They are now deliberately
#: different vocabularies: the wire name answers to standards/07 and to the
#: other district servers it will share a gateway with, the method name to
#: this repository.
#:
#: This map is the dispatch, not a description of it: ``call()`` below
#: resolves every tool through it, so a tool can only reach the method paired
#: with its own name. Keep it that way. A tool body that passed its REST
#: method name as a literal instead could name another tool's method, and no
#: assertion about this map would see it.
TOOL_CLIENT_METHODS = {
    REQUESTS_LIST: "list_requests",
    REQUEST_GET: "get_request",
    OVERDUE_REQUESTS_LIST: "list_overdue_requests",
    COLLECTION_TASKS_LIST: "list_collection_tasks",
    RECORD_SYSTEMS_LIST: "list_record_systems",
    EXEMPTIONS_LIST: "list_exemptions",
    AUDIT_LOG_SEARCH: "search_audit_log",
    CAPABILITIES_LIST: "list_capabilities",
    ACKNOWLEDGMENT_DRAFT: "draft_acknowledgment",
    CLARIFICATION_LETTER_DRAFT: "draft_clarification_letter",
    COLLECTION_TASK_NOTES_PROPOSE: "update_collection_task_notes",
    EXEMPTION_CITATIONS_SUGGEST: "suggest_exemption_citations",
}


def _read_annotations(title: str) -> ToolAnnotations:
    """Annotate a tool that only ever issues a staff-API GET.

    Build standard 4 requires ``readOnlyHint``/``destructiveHint`` on every
    tool because the planned gateway gates its approval flows on them, and a
    missing hint defaults to the dangerous reading (``destructiveHint`` is
    ``true`` by default in the MCP schema). ``openWorldHint`` is false on
    every tool here: the only system any of them reaches is this district's
    own staff REST API.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _proposal_annotations(title: str, *, idempotent: bool) -> ToolAnnotations:
    """Annotate one of the four hosted draft/proposal tools.

    ``readOnlyHint`` is false on all four even though two of them only read
    and return a proposal. That is deliberate: these are the tools a gateway
    must route through an approval flow, and the honest-but-weaker hint would
    let a future gateway auto-approve them. ``destructiveHint`` stays false
    because none of them can destroy or overwrite district data -- the worst
    case is an editable draft a Records Officer discards.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )

READ_INSTRUCTIONS = (
    "Read-only access to the PSD401 Public Records Request system. "
    "Use these tools to look up requests, their status timelines, collection "
    "tasks, the record-system registry, exemption citations, the audit log, "
    "and the GUI/agent capability policy. There is no "
    "way to change a request's status, revise an estimate, or send a denial "
    "through this server; those require a human in the staff UI."
)

HOSTED_INSTRUCTIONS = (
    "Attributed access to the PSD401 Public Records Request system. Read tools "
    "inspect staff state. Four personal-token tools may create an acknowledgment "
    "or clarification draft, or return task-note and exemption-citation "
    "proposals. They never approve, queue, send, change request status, apply an "
    "estimate, attach a citation, resolve a clarification, deny a request, or "
    "exclude a camera. Every legal effect remains an accountable-human handoff "
    "in the staff UI."
)

ClientProvider = Callable[[Context], PRRClient]


def create_mcp_server(
    client_provider: ClientProvider,
    *,
    include_write_tools: bool = False,
    **fastmcp_options,
) -> FastMCP:
    """Register shared reads and, when requested, hosted write tools."""
    mcp = FastMCP(
        "prr-staff-api",
        instructions=(
            HOSTED_INSTRUCTIONS
            if include_write_tools
            else READ_INSTRUCTIONS
        ),
        **fastmcp_options,
    )

    async def call(tool_name, ctx, **kwargs):
        """Run one tool's REST method, resolved from the tool's wire name.

        The lookup is the point: a tool can only reach the method
        TOOL_CLIENT_METHODS pairs with its own name, and an unregistered name
        raises KeyError here instead of quietly calling something else.
        """
        client = client_provider(ctx)
        method = getattr(client, TOOL_CLIENT_METHODS[tool_name])
        return await anyio.to_thread.run_sync(partial(method, **kwargs))

    # Every tool below passes its MCP name explicitly rather than inheriting
    # the Python function name. The two namespaces answer to different rules:
    # the wire name follows standards/07 build standard 3, while the function
    # name stays the plain verb the capability contract and
    # apps/api/tests/test_agent_capabilities.py read out of this file.
    @mcp.tool(
        name=REQUESTS_LIST,
        annotations=_read_annotations("List public records requests"),
    )
    async def list_requests(
        ctx: Context,
        status: str | None = None,
        search: str | None = None,
        page: int | None = None,
        ordering: str | None = None,
    ) -> dict:
        """List public records requests (staff summary view: number, status,
        dates, requester contact, and a one-line subject — not the full
        description, and no timeline). Filter by
        `status` (an exact PRA status code: received, acknowledged,
        clarification, gathering, review, installment, fulfilled,
        closed_withdrawn, closed_abandoned, denied) and/or `search` (matches
        request number, description text, or requester email). `page` selects
        a page of results (50 per page); omit for page 1. `ordering` sorts by
        one whitelisted column — number, requester, subject, status, received,
        due, estimated, assignee, each accepting a "-" prefix for descending;
        anything else falls back to the default statutory-deadline order, as
        does requester for an identity that may not read requester identity.
        The `subject` field is a scanning label, not the legal record: when
        `subject_source` is "ai" it is an unadopted model suggestion (Records
        Officer identities only), and when it is "description" it is the
        opening of the request text. Use this to
        browse or filter the request queue; use get_request for one request's
        full detail and status timeline."""
        return await call(
            REQUESTS_LIST,
            ctx,
            status=status,
            search=search,
            page=page,
            ordering=ordering,
        )

    @mcp.tool(
        name=REQUEST_GET,
        annotations=_read_annotations("Get one public records request"),
    )
    async def get_request(number: str, ctx: Context) -> dict:
        """Get full detail for one public records request by its public number
        (e.g. "PRR-2026-0042"), including its complete staff-visible status
        timeline, estimate-revision history, collection tasks, and any
        camera-footage preservation."""
        return await call(REQUEST_GET, ctx, number=number)

    @mcp.tool(
        name=OVERDUE_REQUESTS_LIST,
        annotations=_read_annotations("List overdue public records requests"),
    )
    async def list_overdue_requests(
        ctx: Context,
        limit: int | None = None,
        response_format: Literal["concise", "detailed"] = CONCISE,
    ) -> dict:
        """List open public records requests past their RCW 42.56.520
        statutory response deadline, excluding already closed requests, worst
        overdue first as the staff queue orders them.

        **Report `total`, not `count`.** `count` is how many detail rows
        came back and is capped by `limit` (default 25, maximum 200);
        `total` is how many requests are overdue and is not capped. When
        they differ, `truncated` is true and a `note` says so.

        The one case where `total` is not the real number is
        `scan_complete: false`, meaning the scan hit its page ceiling before
        the end of the queue; then `total` is a floor, a `warning` field
        says so in words, and the staff UI's overdue view is authoritative.

        `response_format` is "concise" (number, subject, status, matter
        kind, due dates, assignee, requester name and email, days_overdue)
        or "detailed" (the full staff request-summary row). Requester name
        and email are null unless the caller is a Records Officer."""
        return await call(
            OVERDUE_REQUESTS_LIST,
            ctx,
            limit=limit,
            response_format=response_format,
        )

    @mcp.tool(
        name=COLLECTION_TASKS_LIST,
        annotations=_read_annotations("List collection tasks"),
    )
    async def list_collection_tasks(
        ctx: Context,
        request_number: str | None = None,
    ) -> dict:
        """List collection tasks, optionally scoped to one public request
        number, to see which systems must be searched and whether work is
        complete."""
        return await call(
            COLLECTION_TASKS_LIST,
            ctx,
            request_number=request_number,
        )

    @mcp.tool(
        name=RECORD_SYSTEMS_LIST,
        annotations=_read_annotations("List the record-system registry"),
    )
    async def list_record_systems(ctx: Context) -> dict:
        """List the district system registry, including custodians, retention
        windows, triage keywords, and adequate-search checklists."""
        return await call(RECORD_SYSTEMS_LIST, ctx)

    @mcp.tool(
        name=EXEMPTIONS_LIST,
        annotations=_read_annotations("List the exemption-citation taxonomy"),
    )
    async def list_exemptions(ctx: Context) -> dict:
        """List the seeded RCW 42.56 and FERPA exemption citation taxonomy."""
        return await call(EXEMPTIONS_LIST, ctx)

    @mcp.tool(
        name=AUDIT_LOG_SEARCH,
        annotations=_read_annotations("Search the compliance audit log"),
    )
    async def search_audit_log(
        ctx: Context,
        target: str | None = None,
    ) -> dict:
        """Search the append-only compliance audit log, optionally scoped to a
        request number or other target."""
        return await call(AUDIT_LOG_SEARCH, ctx, target=target)

    @mcp.tool(
        name=CAPABILITIES_LIST,
        annotations=_read_annotations("List the GUI/agent parity contract"),
    )
    async def list_capabilities(ctx: Context) -> dict:
        """List the machine-readable GUI/agent parity contract and each
        capability's direct, human-confirmation, or human-only policy."""
        return await call(CAPABILITIES_LIST, ctx)

    if include_write_tools:

        @mcp.tool(
            name=ACKNOWLEDGMENT_DRAFT,
            annotations=_proposal_annotations(
                "Draft a statutory acknowledgment for human review",
                # Each call stores another draft, so a retry is not free.
                idempotent=False,
            ),
        )
        # ``ctx`` leads here rather than trailing as it does on the tools
        # whose parameters are all required: one optional parameter means a
        # trailing ``ctx`` would follow a default, which Python forbids. This
        # matches the read tools, which also put it first for that reason.
        # FastMCP keys its argument model by name and drops ``ctx``, so the
        # position changes no part of the published schema.
        async def draft_acknowledgment(
            ctx: Context,
            request_number: str,
            estimated_completion: str | None = None,
        ) -> dict:
            """Create the RCW 42.56.520 acknowledgment draft for one request
            from the district's versioned statutory template, for human
            review. Requires an attributed personal token. This does not
            approve, queue, send, or change request status: the request
            becomes `acknowledged` only after a human approves the draft and
            delivery is confirmed, so drafting one does not satisfy or defer
            the five-business-day response.

            `estimated_completion` is optional and must be an ISO date
            (YYYY-MM-DD). Omit it to use the request's own estimate, or the
            district default when it has none. A date that disagrees with an
            estimate already on the request is rejected rather than applied;
            revising a published estimate is a separate human workflow.

            To work a backlog, call this once per request number; there is no
            batch form, and each draft is one case a human then reviews."""
            return await call(
                ACKNOWLEDGMENT_DRAFT,
                ctx,
                request_number=request_number,
                estimated_completion=estimated_completion,
            )

        @mcp.tool(
            name=CLARIFICATION_LETTER_DRAFT,
            annotations=_proposal_annotations(
                "Draft a clarification letter for human review",
                # Each call stores another draft, so a retry is not free.
                idempotent=False,
            ),
        )
        async def draft_clarification_letter(
            request_number: str,
            clarification_questions: str,
            clarification_due: str,
            ctx: Context,
        ) -> dict:
            """Create a clarification-request draft for human review through
            the existing staff REST primitive. Requires an attributed personal
            token. This does not approve, queue, send, or change request status.
            `clarification_due` must be an ISO date (YYYY-MM-DD)."""
            return await call(
                CLARIFICATION_LETTER_DRAFT,
                ctx,
                request_number=request_number,
                clarification_questions=clarification_questions,
                clarification_due=clarification_due,
            )

        @mcp.tool(
            name=COLLECTION_TASK_NOTES_PROPOSE,
            annotations=_proposal_annotations(
                "Propose replacement collection-task instructions",
                # It reads the task and returns a proposal; nothing changes,
                # so calling it twice costs nothing and leaves no trace.
                idempotent=True,
            ),
        )
        async def update_collection_task_notes(
            task_id: int,
            proposed_instructions: str,
            ctx: Context,
        ) -> dict:
            """Prepare replacement instructions for a collection task.
            Requires an attributed personal token. The tool returns the current
            and proposed text plus the normal PATCH handoff; it does not update
            the task."""
            return await call(
                COLLECTION_TASK_NOTES_PROPOSE,
                ctx,
                task_id=task_id,
                proposed_instructions=proposed_instructions,
            )

        @mcp.tool(
            name=EXEMPTION_CITATIONS_SUGGEST,
            annotations=_proposal_annotations(
                "Suggest exemption citations for a record item",
                idempotent=True,
            ),
        )
        async def suggest_exemption_citations(
            record_item_id: str,
            candidate_citation_ids: list[int],
            rationale: str,
            ctx: Context,
        ) -> dict:
            """Validate active exemption citations and return a proposal for a
            record item. Requires an attributed personal token. A reviewer must
            confirm the proposal; this tool never attaches a citation, creates
            a redaction, or withholds a record."""
            return await call(
                EXEMPTION_CITATIONS_SUGGEST,
                ctx,
                record_item_id=record_item_id,
                candidate_citation_ids=candidate_citation_ids,
                rationale=rationale,
            )

    return mcp
