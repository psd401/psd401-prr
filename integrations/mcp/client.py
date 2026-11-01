# ABOUTME: Requests-based client for the PRR staff REST API used by MCP.
# ABOUTME: Four personal-token tools are limited to drafts and proposals.
"""
PRRClient wraps the staff API (apps/api, mounted at /api/staff/) with a plain
`requests.Session`. This module intentionally has no `mcp` import, keeping the
REST contract independent of either the hosted or stdio MCP transport. The
MCP-facing modules (`tools.py`, `hosted.py`, and `server.py`) layer the SDK
over this client.

Read methods return the staff API response parsed from JSON. The only write
requests are the acknowledgment- and clarification-draft POSTs, and every
write-tool method requires the `prr_pat_` marker used by attributed personal
access tokens. Both drafts stop at `draft`: approving, queueing, sending, and
the status transition that follows confirmed delivery stay human-only.
Collection-note and exemption-citation tools fetch authoritative rows and
return structured proposals; they never PATCH a task or attach legal evidence.
The standalone stdio server registers only the read methods, so the shared
read-only service deployment remains write-free.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import re
import uuid
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests

# Mirrors apps.records.services.CLOSED_STATUSES. Duplicated rather than
# imported because this module must stay Django-free (see module docstring).
CLOSED_STATUSES = frozenset({
    "fulfilled", "closed_withdrawn", "closed_abandoned", "denied",
})

# The district's compliance calendar runs on Pacific time (config.settings
# TIME_ZONE); "overdue" has to be computed against that date, not whatever
# timezone the machine running this client happens to be in.
_DISTRICT_TZ = ZoneInfo("America/Los_Angeles")
# PRR-, LIT-, and FER- are the three matter-kind prefixes (ADR 0004).
_REQUEST_NUMBER = re.compile(
    r"(?:PRR|LIT|FER)-[0-9]{4}-[0-9]{4,11}\Z", re.ASCII
)
_PERSONAL_TOKEN_MARKER = "prr_pat_"

#: psd-dev-standards standards/07-mcp.md build standard 3: every tool response
#: is paginated and truncated well under a model's context budget, and offers a
#: `response_format` of "concise" or "detailed".
CONCISE = "concise"
DETAILED = "detailed"
RESPONSE_FORMATS = (CONCISE, DETAILED)

#: Bounds for the overdue scan. The default is a triage-sized page a Records
#: Officer can actually act on; the ceiling exists so a caller cannot ask for
#: the whole backlog in one response. OVERDUE_MAX_PAGES bounds the *work*
#: rather than the output: the staff API pages at 50 rows, so this reads at
#: most 2,000 requests before reporting an incomplete scan.
OVERDUE_DEFAULT_LIMIT = 25
OVERDUE_MAX_LIMIT = 200
OVERDUE_MAX_PAGES = 40

#: The concise projection of an overdue row: enough to rank the queue, open
#: the right request, and tell an officer which case it is at a glance.
#:
#: `subject`, `requester_name` and `requester_email` are here by the data
#: owner's decision. A tool result leaves for the caller's model provider,
#: and `subject` can be the opening of the requester's own wording (when
#: `subject_source` is "description"), which can name a student. Hagel (CIO,
#: data owner) signed off on 2026-10-19 that MCP tool results may carry all
#: three; the sign-off is recorded in docs/phase-2-production-handoffs.md.
#: The staff serializer still nulls requester contact for callers who are
#: not Records Officers, and that applies here unchanged.
#:
#: `matter_kind` stays because which statute's clock was missed decides what
#: happens next (rule 13), and a bare request number does not say.
OVERDUE_CONCISE_FIELDS = (
    "number",
    "subject",
    "status",
    "matter_kind",
    "statutory_response_due",
    "next_due",
    "next_due_basis",
    "assigned_officer_username",
    "requester_name",
    "requester_email",
)


def _district_today() -> dt.date:
    return dt.datetime.now(_DISTRICT_TZ).date()


def _validated_request_number(value, *, field: str) -> str:
    """Reject anything that is not a canonical matter number, before any HTTP.

    The number is either interpolated into a REST path or sent as a request
    body field, so the guard belongs in front of the request rather than
    behind it. ``field`` names the caller's own parameter so the error
    teaches the vocabulary for the argument the model actually passed, which
    is build standard 3's ask.
    """
    if not isinstance(value, str) or not _REQUEST_NUMBER.fullmatch(value):
        raise ValueError(f"{field} must be a canonical PRR request number.")
    return value


def _validated_iso_date(value, *, field: str) -> dt.date:
    """Accept only a dashed ``YYYY-MM-DD`` string, which is what we document.

    ``date.fromisoformat`` is wider than that on Python 3.12: it also accepts
    the compact ``YYYYMMDD`` form, and a caller importing ``PRRClient``
    directly who passed the integer ``20261020`` would have had it coerced by
    ``str()`` and silently accepted -- never seeing the error that teaches the
    documented format. ``strptime`` accepts the one form the docstring and the
    tool schema promise, and a non-string is rejected rather than stringified.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD).")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO date (YYYY-MM-DD)."
        ) from exc


def _validated_response_format(value) -> str:
    """Accept only the two documented formats, and say what the other one is.

    Build standard 3 asks for errors that teach: a caller that guessed
    "summary" or "full" learns the real vocabulary from the message rather
    than retrying blindly.

    Over MCP this rarely fires, because the tool's parameter is typed as a
    two-value Literal and the schema rejects a bad value before the call
    reaches here -- which is the better outcome, since the enum is in the
    tool definition where a model reads it up front. This stays for the
    callers that have no schema in front of them: REST users of this module
    and anything importing PRRClient directly.
    """
    if value in RESPONSE_FORMATS:
        return value
    raise ValueError(
        f"response_format must be {CONCISE!r} (triage fields only) or "
        f"{DETAILED!r} (the full staff request-summary row); got {value!r}."
    )


def _validated_overdue_limit(value) -> int:
    if value is None:
        return OVERDUE_DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"limit must be a whole number of rows between 1 and "
            f"{OVERDUE_MAX_LIMIT}; got {value!r}."
        )
    if not 1 <= value <= OVERDUE_MAX_LIMIT:
        raise ValueError(
            f"limit must be between 1 and {OVERDUE_MAX_LIMIT} rows; got "
            f"{value}. Ask for a smaller page and read `truncated` to see "
            f"whether more overdue requests remain."
        )
    return value


def _shaped_overdue_row(row, response_format, due, today) -> dict:
    """Project one overdue row, adding the number a triage decision needs.

    `due` is passed in already parsed rather than re-read from the row: the
    caller has to parse it to decide the row is overdue at all, and parsing
    it twice would let the projection disagree with the filter.

    `days_overdue` is computed here rather than asked of the caller because
    the PRA clock runs on the district's Pacific date (see `_district_today`),
    and a model doing the subtraction against its own idea of "today" is how
    a deadline report quietly goes wrong by a day.
    """
    if response_format == DETAILED:
        shaped = dict(row)
    else:
        # Every concise field is always present, null when the staff API did
        # not send it. Omitting absent keys instead would let a serializer
        # rename silently delete a column from this view while `detailed`
        # kept it; a null is a visible symptom, a missing key is not.
        shaped = {
            field: row.get(field) for field in OVERDUE_CONCISE_FIELDS
        }
    shaped["days_overdue"] = (today - due).days
    return shaped


def _validated_base_url(base_url: str) -> str:
    value = str(base_url).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise ValueError("base_url must be a valid HTTPS URL.") from exc
    hostname = (parsed.hostname or "").lower()
    loopback_http = (
        parsed.scheme.lower() == "http"
        and (hostname == "localhost" or bool(address and address.is_loopback))
    )
    if (
        not hostname
        or (parsed.scheme.lower() != "https" and not loopback_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "base_url must be HTTPS without credentials, query, or fragment; "
            "HTTP is allowed only for loopback development."
        )
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}{parsed.path.rstrip('/')}"


class PRRClient:
    """Thin wrapper over the PSD401 PRR staff API."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
        self.base_url = _validated_base_url(base_url)
        self.timeout = timeout
        self._uses_personal_token = token.startswith(_PERSONAL_TOKEN_MARKER)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict | None = None):
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise requests.exceptions.HTTPError(
                "PRR API redirects are not allowed.",
                response=response,
            )
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict):
        response = self.session.post(
            f"{self.base_url}{path}",
            json=body,
            timeout=self.timeout,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise requests.exceptions.HTTPError(
                "PRR API redirects are not allowed.",
                response=response,
            )
        response.raise_for_status()
        return response.json()

    def _require_attributed_personal_token(self):
        if not self._uses_personal_token:
            raise PermissionError(
                "MCP write tools require an attributed personal access token; "
                "the shared read-only MCP identity is not permitted."
            )

    def list_requests(self, status: str | None = None, search: str | None = None,
                       page: int | None = None, ordering: str | None = None):
        """List PublicRecordsRequest summaries — GET /api/staff/requests/.

        `status` filters to an exact RequestStatus code (e.g. "gathering"),
        or to a group: "open" (every non-terminal status) or "closed"
        (Fulfilled, Denied, and every Closed status).
        `search` matches request number, description, or requester email
        (server-side, case-insensitive substring). `page` selects a page of
        the DRF-paginated result (50 per page). `ordering` sorts the page by
        one whitelisted column: number, requester, subject, status, received,
        due, estimated, or assignee, prefixed with "-" for descending; an
        unknown key, and requester for any identity that may not read
        requester identity, fall back to the default order. All four are
        optional; omitting them returns page 1, unfiltered, ordered by
        statutory deadline (soonest first).
        """
        params = {k: v for k, v in
                  {"status": status, "search": search, "page": page,
                   "ordering": ordering}.items()
                  if v is not None}
        return self._get("/api/staff/requests/", params or None)

    def get_request(self, number: str):
        """Full detail for one request, including its status timeline —
        GET /api/staff/requests/<number>/.

        `number` is the public request number (e.g. "PRR-2026-0042"), not
        the internal UUID. The response's "events" key is the complete
        staff-visible timeline (every StatusEvent: occurred_at, from_status,
        to_status, note, actor, visible_to_requester — newest first) — a
        complete staff-visible compliance timeline. It also includes
        estimate_revisions, collection_tasks, and footage_preservations for
        the request.
        """
        number = _validated_request_number(number, field="number")
        return self._get(
            f"/api/staff/requests/{quote(number, safe='')}/"
        )

    def list_overdue_requests(
        self,
        limit: int | None = None,
        response_format: str = CONCISE,
    ) -> dict:
        """Open requests past their RCW 42.56.520 statutory response
        deadline — a client-side filter layered over GET
        /api/staff/requests/.

        The staff-requests resource has no server-side filter on
        statutory_response_due (only `status` and `search` — see
        RequestsViewSet.get_queryset in apps/api/resources/requests.py), so
        this walks pages of list_requests() and keeps rows where
        statutory_response_due is set, is strictly before today in the
        district's Pacific timezone, and status is not one of the closed
        statuses (fulfilled, closed_withdrawn, closed_abandoned, denied) —
        the same "still open" definition apps/records/tasks.py's watchdogs
        use.

        `limit` caps the detail rows returned (default
        OVERDUE_DEFAULT_LIMIT, ceiling OVERDUE_MAX_LIMIT), because a
        district-sized backlog returns more text than a model's context can
        hold. **It does not cap the count.** The scan keeps counting every
        overdue request it finds and reports that as `total`, so the one
        number a missed statutory deadline turns on is never silently a page
        size. `count` is how many rows came back; `total` is how many
        requests are overdue; `truncated` says the two differ.

        The only thing that can make `total` incomplete is the page ceiling:
        OVERDUE_MAX_PAGES bounds how far the scan reads, and
        `scan_complete: false` means it stopped early, so `total` is a floor
        and the staff UI's own overdue view is authoritative. At the staff
        API's 50 rows per page the ceiling is 2,000 requests, which is well
        past any plausible district queue, so in practice `total` is the
        real number.

        `response_format` is "concise" (the default: just the fields needed
        to triage and open a request) or "detailed" (the full staff
        request-summary row as the REST API returned it).
        """
        response_format = _validated_response_format(response_format)
        limit = _validated_overdue_limit(limit)
        today = _district_today()
        overdue: list[dict] = []
        total = 0
        pages_scanned = 0
        scan_complete = False
        page = 1
        while pages_scanned < OVERDUE_MAX_PAGES:
            payload = self.list_requests(page=page)
            pages_scanned += 1
            for row in payload["results"]:
                raw_due = row.get("statutory_response_due")
                if not raw_due or row.get("status") in CLOSED_STATUSES:
                    continue
                due = dt.date.fromisoformat(raw_due)
                if due >= today:
                    continue
                total += 1
                # Shape only what is returned. Counting the rest is cheap;
                # projecting rows nobody will read is not.
                if len(overdue) < limit:
                    overdue.append(
                        _shaped_overdue_row(row, response_format, due, today),
                    )
            if not payload.get("next"):
                scan_complete = True
                break
            page += 1
        envelope = {
            "results": overdue,
            "count": len(overdue),
            "total": total,
            "response_format": response_format,
            "limit": limit,
            # Derived, not tracked: with `total` counted independently of
            # `limit`, truncation is simply "we counted more than we shaped".
            "truncated": total > len(overdue),
            # Kept because it is the evidence behind an incomplete scan: the
            # difference between "read 2 pages and finished" and "read 40 and
            # gave up" is the difference between a total and a floor.
            "pages_scanned": pages_scanned,
            "scan_complete": scan_complete,
            "as_of": today.isoformat(),
        }
        # Prose, not just flags. A model summarizing this envelope for an
        # officer will paraphrase it, and a boolean is the first thing lost in
        # a paraphrase; a sentence carrying the caveat survives being retold.
        # Getting the overdue count wrong is a statutory-deadline error, so
        # the caveat travels with the data rather than only in the docstring.
        if not scan_complete:
            envelope["warning"] = (
                f"INCOMPLETE SCAN: stopped after {pages_scanned} pages "
                f"without reaching the end of the queue, so total={total} is "
                "a floor and not the number of overdue requests. Do not "
                "report it as a total. The staff UI's overdue view is "
                "authoritative."
            )
        elif envelope["truncated"]:
            envelope["note"] = (
                f"Showing the {len(overdue)} most urgent of {total} overdue "
                f"requests. Report {total} as the number overdue; raise "
                "`limit` to see more rows."
            )
        return envelope

    def list_collection_tasks(self, request_number: str | None = None):
        """List CollectionTask search assignments — GET
        /api/staff/collection-tasks/.

        Pass `request_number` (the public PRR-YYYY-NNNN number) to scope to
        one request's tasks; omit it to list every collection task across
        all requests.
        """
        params = {"request": request_number} if request_number else None
        return self._get("/api/staff/collection-tasks/", params)

    def list_record_systems(self):
        """List the district's system registry — GET
        /api/staff/record-systems/.

        Each row is one place responsive records can live (SIS, email,
        camera DVRs, ...): custodian title/email, retention window, triage
        keywords, and the adequate-search checklist used to build
        collection-task instructions.
        """
        return self._get("/api/staff/record-systems/")

    def list_exemptions(self):
        """List the seeded RCW 42.56 / FERPA exemption citation taxonomy —
        GET /api/staff/exemptions/. Every redaction or withholding in the
        system cites one of these rows.
        """
        return self._get("/api/staff/exemptions/")

    def search_audit_log(self, target: str | None = None):
        """Search the append-only audit log — GET /api/staff/audit-log/.

        `target` filters to entries whose target contains the given string
        (case-insensitive substring — a request number like "PRR-2026-0042"
        is the common case); omit it to fetch the most recent entries
        across every target, newest first.
        """
        params = {"target": target} if target else None
        return self._get("/api/staff/audit-log/", params)

    def list_capabilities(self):
        """Discover every staff-GUI outcome, its REST primitive, and its
        agent execution policy — GET /api/staff/capabilities/.

        The catalog is the machine-readable parity contract for agents. An
        `execution` value of `direct` permits token-authenticated execution;
        `human_confirmation` requires explicit approval first; `human_only`
        requires a handoff to the accountable human in the staff UI.
        """
        return self._get("/api/staff/capabilities/")

    def draft_acknowledgment(
        self,
        request_number: str,
        estimated_completion: str | None = None,
    ):
        """Create an acknowledgment draft without approval or legal effect.

        POST /api/staff/correspondence/drafts/acknowledgment/ renders the
        versioned statutory acknowledgment template into an editable draft.
        It is the one RCW 42.56.520 five-business-day letter a records
        officer writes for every new request, and before this tool existed
        the only agent-reachable drafting primitive was the clarification
        letter -- so an officer facing a backlog of unacknowledged cases had
        to open each one in the staff UI by hand.

        Omit ``estimated_completion`` and the service uses the request's own
        estimate, or the district default when the request has none. Passing
        a date that disagrees with an estimate already on the request is
        rejected by the service rather than silently revising it: changing a
        published estimate is the human estimate-revision workflow (rule 6),
        not a side effect of drafting a letter.

        Like every tool in this module, this creates a draft and stops. It
        does not approve, queue, send, set ``initial_response_delivered``, or
        transition the request; ``acknowledged`` is applied only by the
        correspondence service after a human-approved delivery is confirmed.
        """
        self._require_attributed_personal_token()
        body = {
            "request_number": _validated_request_number(
                request_number,
                field="request_number",
            ),
        }
        if estimated_completion is not None:
            body["estimated_completion"] = _validated_iso_date(
                estimated_completion,
                field="estimated_completion",
            ).isoformat()
        return self._post(
            "/api/staff/correspondence/drafts/acknowledgment/",
            body,
        )

    def draft_clarification_letter(
        self,
        request_number: str,
        clarification_questions: str,
        clarification_due: str,
    ):
        """Create a clarification draft without approval or legal effect."""
        self._require_attributed_personal_token()
        request_number = _validated_request_number(
            request_number,
            field="request_number",
        )
        questions = str(clarification_questions).strip()
        if not questions:
            raise ValueError("clarification_questions cannot be blank.")
        due = _validated_iso_date(
            clarification_due,
            field="clarification_due",
        )
        return self._post(
            "/api/staff/correspondence/drafts/clarification/",
            {
                "request_number": request_number,
                "clarification_questions": questions,
                "clarification_due": due.isoformat(),
            },
        )

    def update_collection_task_notes(
        self,
        task_id: int,
        proposed_instructions: str,
    ):
        """Return a reviewable task-instructions proposal without PATCHing."""
        self._require_attributed_personal_token()
        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise ValueError("task_id must be a positive integer.")
        instructions = str(proposed_instructions).strip()
        if not instructions:
            raise ValueError("proposed_instructions cannot be blank.")
        task = self._get(f"/api/staff/collection-tasks/{task_id}/")
        return {
            "proposal_type": "collection_task_instructions",
            "applied": False,
            "task": {
                "id": task["id"],
                "request_number": task["request_number"],
                "current_instructions": task["instructions"],
                "assigned_to_username": task.get("assigned_to_username"),
            },
            "proposal": {"instructions": instructions},
            "human_gate": {
                "required": True,
                "action": "Review and accept the proposed instructions.",
                "rest_primitive": {
                    "method": "PATCH",
                    "path": f"/api/staff/collection-tasks/{task_id}/",
                    "body": {"instructions": instructions},
                },
            },
        }

    def suggest_exemption_citations(
        self,
        record_item_id: str,
        candidate_citation_ids: list[int],
        rationale: str,
    ):
        """Return validated active-citation candidates without attaching them."""
        self._require_attributed_personal_token()
        try:
            normalized_item_id = str(uuid.UUID(str(record_item_id)))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("record_item_id must be a UUID.") from exc
        if (
            not isinstance(candidate_citation_ids, list)
            or not candidate_citation_ids
        ):
            raise ValueError(
                "candidate_citation_ids must contain at least one citation."
            )
        normalized_citation_ids = []
        for citation_id in candidate_citation_ids:
            if (
                isinstance(citation_id, bool)
                or not isinstance(citation_id, int)
                or citation_id < 1
            ):
                raise ValueError(
                    "candidate_citation_ids must contain positive integers."
                )
            if citation_id not in normalized_citation_ids:
                normalized_citation_ids.append(citation_id)
        proposal_rationale = str(rationale).strip()
        if not proposal_rationale:
            raise ValueError("rationale cannot be blank.")

        item = self._get(
            f"/api/staff/record-items/{quote(normalized_item_id, safe='')}/"
        )
        citations = []
        for citation_id in normalized_citation_ids:
            citation = self._get(
                f"/api/staff/exemptions/{citation_id}/"
            )
            if not citation["active"]:
                raise ValueError(
                    f"Exemption citation {citation_id} is not active."
                )
            citations.append(citation)
        return {
            "proposal_type": "record_item_exemption_citations",
            "applied": False,
            "record_item": {
                "id": item["id"],
                "request_number": item["request_number"],
                "original_filename": item["original_filename"],
                "review_state": item["review_state"],
            },
            "proposal": {
                "candidate_citations": citations,
                "rationale": proposal_rationale,
            },
            "human_gate": {
                "required": True,
                "action": (
                    "A reviewer must confirm each citation before attaching "
                    "it to a redaction or withholding."
                ),
            },
        }
