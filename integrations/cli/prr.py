#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.32,<3",
# ]
# ///
# ABOUTME: Token-authenticated command-line client over registered PRR REST capabilities.
# ABOUTME: The client owns presentation only; Django retains authorization and compliance.
"""Run the PSD401 public-records command-line client.

The CLI is deliberately a thin HTTP client. It does not reproduce deadline,
authorization, exemption, correspondence, or lifecycle rules locally. Every
operation maps to a registered staff capability (or ``auth/me``), and the
server remains the authority for validation and human-only handoffs.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections.abc import Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit

import requests

COMMAND_CAPABILITIES = {
    "capabilities": "capabilities.list",
    "requests list": "requests.list",
    "requests get": "requests.retrieve",
    "requests transition": "requests.transition",
    "tasks list": "collection_tasks.list",
    "tasks update": "collection_tasks.update",
    "systems list": "record_systems.list",
    "exemptions list": "exemptions.list",
    "audit search": "audit.list",
    "time log": "time_entries.create",
    "correspondence draft general": "correspondence.draft_general",
    "correspondence draft acknowledgment": (
        "correspondence.draft_acknowledgment"
    ),
    "correspondence draft clarification": (
        "correspondence.draft_clarification"
    ),
    "correspondence draft estimate-revision": (
        "correspondence.draft_estimate_revision"
    ),
    "correspondence draft installment": (
        "correspondence.draft_installment"
    ),
    "correspondence draft responsive-records": (
        "correspondence.draft_responsive_records"
    ),
    "correspondence draft closure": "correspondence.draft_closure",
    "correspondence draft denial": "correspondence.draft_denial",
}

_AGENT_ACCESS_ROUTE = "/agent-access"
_PREFERRED_COLUMNS = (
    "number",
    "id",
    "request_number",
    "status",
    "state",
    "kind",
    "name",
    "short_name",
    "citation",
    "system_name",
    "assigned_to_username",
    "due_date",
    "statutory_response_due",
    "date",
    "minutes",
    "activity",
    "action",
    "target",
    "occurred_at",
    "execution",
    "risk",
    "method",
    "path",
)


class CLIError(Exception):
    """Expected, user-actionable CLI failure."""

    def __init__(self, message, *, payload=None, exit_code=1):
        super().__init__(message)
        self.payload = payload or {
            "code": "cli_error",
            "detail": str(message),
        }
        self.exit_code = exit_code


class HumanApprovalRequired(CLIError):
    """Structured handoff returned by a human-only server action."""

    def __init__(self, payload, staff_ui_route):
        handoff = payload.get("handoff") or {}
        augmented = dict(payload)
        augmented["staff_ui_route"] = staff_ui_route
        action = str(handoff.get("action") or "Complete this action")
        if not action.endswith("."):
            action += "."
        message = "\n".join(
            [
                "Human approval required",
                f"Capability: {handoff.get('capability_id', 'unknown')}",
                f"Action: {action}",
                f"Required role: {handoff.get('required_role', 'staff')}",
                f"Staff UI route: {staff_ui_route}",
            ]
        )
        super().__init__(message, payload=augmented, exit_code=2)


def _validated_base_url(base_url):
    value = str(base_url).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise CLIError("PRR_BASE_URL must be a valid HTTPS URL.") from exc

    hostname = (parsed.hostname or "").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    loopback_http = (
        parsed.scheme.lower() == "http"
        and (
            hostname == "localhost"
            or bool(address and address.is_loopback)
        )
    )
    if (
        not hostname
        or (
            parsed.scheme.lower() != "https"
            and not loopback_http
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CLIError(
            "PRR_BASE_URL must use HTTPS without credentials, query, or "
            "fragment. HTTP is allowed only for localhost or another "
            "loopback address."
        )
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return (
        f"{parsed.scheme.lower()}://{authority}"
        f"{parsed.path.rstrip('/')}"
    )


def _required_configuration(args, environ):
    base_url = (args.base_url or environ.get("PRR_BASE_URL", "")).strip()
    token = (args.api_token or environ.get("PRR_API_TOKEN", "")).strip()
    missing = []
    if not base_url:
        missing.append("PRR_BASE_URL")
    if not token:
        missing.append("PRR_API_TOKEN")
    if missing:
        raise CLIError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Set the environment variable(s) or pass the matching "
            "global flag."
        )
    if any(character.isspace() for character in token):
        raise CLIError(
            "PRR_API_TOKEN must be one token value without whitespace."
        )
    return _validated_base_url(base_url), token


def _response_payload(response):
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        return {"detail": response.text.strip() or "The server returned no detail."}


def _api_error_message(status_code, payload):
    if isinstance(payload, Mapping):
        code = str(payload.get("code") or "")
        detail = payload.get("detail")
        if detail:
            return f"PRR API error {status_code} ({code or 'error'}): {detail}"
        messages = []
        for field, value in payload.items():
            if isinstance(value, Sequence) and not isinstance(value, str):
                rendered = "; ".join(str(item) for item in value)
            else:
                rendered = str(value)
            messages.append(f"{field}: {rendered}")
        if messages:
            return f"PRR API error {status_code}: " + " | ".join(messages)
    return f"PRR API error {status_code}."


class PRRClient:
    """Small HTTP adapter shared by every CLI command."""

    def __init__(self, base_url, token, *, timeout=30.0):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "psd401-prr-cli/1",
            }
        )

    def request(
        self,
        method,
        path,
        *,
        params=None,
        body=None,
        route_values=None,
    ):
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            raise CLIError(f"Could not reach the PRR API: {exc}") from exc

        if 300 <= response.status_code < 400:
            raise CLIError("PRR API redirects are refused.")

        payload = _response_payload(response)
        if response.ok:
            return payload

        code = (
            str(payload.get("code") or "")
            if isinstance(payload, Mapping)
            else ""
        )
        if code == "human_approval_required":
            capability_id = str(
                (payload.get("handoff") or {}).get("capability_id") or ""
            )
            route = self._staff_ui_route(
                capability_id,
                route_values=route_values or {},
            )
            raise HumanApprovalRequired(payload, route)
        if response.status_code == 401:
            if code == "token_expired":
                raise CLIError(
                    "The personal API token has expired. Mint a new token on "
                    f"the Agent Access page ({_AGENT_ACCESS_ROUTE}).",
                    payload={
                        **payload,
                        "remediation": (
                            "Mint a new token on the Agent Access page "
                            f"({_AGENT_ACCESS_ROUTE})."
                        ),
                    },
                )
            if code == "token_revoked":
                raise CLIError(
                    "The personal API token was revoked. Mint or rotate a "
                    f"token on the Agent Access page ({_AGENT_ACCESS_ROUTE}).",
                    payload={
                        **payload,
                        "remediation": (
                            "Mint or rotate a token on the Agent Access page "
                            f"({_AGENT_ACCESS_ROUTE})."
                        ),
                    },
                )
            if code in {"invalid_token", ""}:
                raise CLIError(
                    "The API token is invalid. Check PRR_API_TOKEN or mint/"
                    "rotate a token on the Agent Access page "
                    f"({_AGENT_ACCESS_ROUTE}).",
                    payload={
                        **(payload or {}),
                        "code": code or "invalid_token",
                        "remediation": (
                            "Check PRR_API_TOKEN or mint/rotate a token on "
                            f"the Agent Access page ({_AGENT_ACCESS_ROUTE})."
                        ),
                    },
                )
        raise CLIError(
            _api_error_message(response.status_code, payload),
            payload=payload
            if isinstance(payload, Mapping)
            else {
                "code": "api_error",
                "detail": str(payload),
                "status": response.status_code,
            },
        )

    def _staff_ui_route(self, capability_id, *, route_values):
        """Resolve a handoff against the live catalog without masking it."""
        route = _AGENT_ACCESS_ROUTE
        try:
            response = self.session.request(
                "GET",
                f"{self.base_url}/api/staff/capabilities/",
                timeout=self.timeout,
                allow_redirects=False,
            )
            if response.ok:
                catalog = _response_payload(response) or {}
                capability = next(
                    (
                        item
                        for item in catalog.get("capabilities", [])
                        if item.get("id") == capability_id
                    ),
                    None,
                )
                if capability and capability.get("ui_routes"):
                    route = str(capability["ui_routes"][0])
        except requests.exceptions.RequestException:
            pass
        for name, value in route_values.items():
            route = route.replace(f":{name}", quote(str(value), safe=""))
        return route


def _add_json_flag(parser):
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON.",
    )


def _leaf(subparsers, name, *, help_text, capability, handler):
    parser = subparsers.add_parser(name, help=help_text)
    parser.set_defaults(
        handler=handler,
        command_capability=capability,
        json_output=False,
    )
    _add_json_flag(parser)
    return parser


def _add_recipients(parser):
    parser.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Add a To recipient. Repeat as needed.",
    )
    parser.add_argument(
        "--cc",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Add a Cc recipient. Repeat as needed.",
    )
    parser.add_argument(
        "--bcc",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Add a Bcc recipient. Repeat as needed.",
    )


def _add_authored_draft(draft_subparsers, name, capability, help_text):
    parser = _leaf(
        draft_subparsers,
        name,
        help_text=help_text,
        capability=capability,
        handler=_command_correspondence_draft,
    )
    parser.add_argument("request_number")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    _add_recipients(parser)
    parser.set_defaults(draft_kind=name)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="prr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Token-authenticated CLI over the PSD401 PRR staff API. "
            "Django remains the authority for compliance and authorization."
        ),
        epilog="""command set:
  me
  capabilities
  requests list|get|transition
  tasks list|update
  systems list
  exemptions list
  audit search
  time log
  correspondence draft general|acknowledgment|clarification|estimate-revision
  correspondence draft installment|responsive-records|closure|denial

Run any leaf command with --help for its arguments.""",
    )
    parser.add_argument(
        "--base-url",
        help="PRR application origin (default: PRR_BASE_URL).",
    )
    parser.add_argument(
        "--api-token",
        help="Personal API token (default: PRR_API_TOKEN).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    command = parser.add_subparsers(dest="command", required=True)

    me = _leaf(
        command,
        "me",
        help_text="Show the authenticated staff identity (auth/me).",
        capability=None,
        handler=_command_me,
    )
    me.set_defaults(command_capability="auth/me")

    _leaf(
        command,
        "capabilities",
        help_text="Render the live capability and execution-policy catalog.",
        capability=COMMAND_CAPABILITIES["capabilities"],
        handler=_command_capabilities,
    )

    requests_parser = command.add_parser(
        "requests",
        help="Read requests and perform authorized lifecycle transitions.",
    )
    request_commands = requests_parser.add_subparsers(
        dest="requests_command",
        required=True,
    )
    request_list = _leaf(
        request_commands,
        "list",
        help_text="List and filter public-records requests.",
        capability=COMMAND_CAPABILITIES["requests list"],
        handler=_command_requests_list,
    )
    request_list.add_argument("--status")
    request_list.add_argument("--search")
    request_list.add_argument("--page", type=int)

    request_get = _leaf(
        request_commands,
        "get",
        help_text="Get one request by public number.",
        capability=COMMAND_CAPABILITIES["requests get"],
        handler=_command_requests_get,
    )
    request_get.add_argument("number")

    request_transition = _leaf(
        request_commands,
        "transition",
        help_text=(
            "Request an authorized administrative transition. Human-only "
            "outcomes return a structured handoff."
        ),
        capability=COMMAND_CAPABILITIES["requests transition"],
        handler=_command_requests_transition,
    )
    request_transition.add_argument("number")
    request_transition.add_argument("to_status")
    request_transition.add_argument("--note", default="")

    tasks_parser = command.add_parser(
        "tasks",
        help="List and update collection tasks.",
    )
    task_commands = tasks_parser.add_subparsers(
        dest="tasks_command",
        required=True,
    )
    task_list = _leaf(
        task_commands,
        "list",
        help_text="List collection tasks.",
        capability=COMMAND_CAPABILITIES["tasks list"],
        handler=_command_tasks_list,
    )
    task_list.add_argument("--request", dest="request_number")
    task_list.add_argument("--state")
    task_list.add_argument("--urgent", action="store_true")
    task_list.add_argument("--page", type=int)

    task_update = _leaf(
        task_commands,
        "update",
        help_text="Update task state, due date, instructions, or assignee.",
        capability=COMMAND_CAPABILITIES["tasks update"],
        handler=_command_tasks_update,
    )
    task_update.add_argument("id", type=int)
    task_update.add_argument("--state")
    task_update.add_argument("--due-date")
    task_update.add_argument("--instructions")
    assignee = task_update.add_mutually_exclusive_group()
    assignee.add_argument("--assignee-id", type=int)
    assignee.add_argument(
        "--unassign",
        action="store_true",
        help="Clear the current assignee.",
    )

    systems_parser = command.add_parser("systems", help="Read record systems.")
    systems_commands = systems_parser.add_subparsers(
        dest="systems_command",
        required=True,
    )
    _leaf(
        systems_commands,
        "list",
        help_text="List the district record-system registry.",
        capability=COMMAND_CAPABILITIES["systems list"],
        handler=_command_systems_list,
    )

    exemptions_parser = command.add_parser(
        "exemptions",
        help="Read exemption citations.",
    )
    exemption_commands = exemptions_parser.add_subparsers(
        dest="exemptions_command",
        required=True,
    )
    _leaf(
        exemption_commands,
        "list",
        help_text="List the exemption-citation taxonomy.",
        capability=COMMAND_CAPABILITIES["exemptions list"],
        handler=_command_exemptions_list,
    )

    audit_parser = command.add_parser("audit", help="Search append-only audit.")
    audit_commands = audit_parser.add_subparsers(
        dest="audit_command",
        required=True,
    )
    audit_search = _leaf(
        audit_commands,
        "search",
        help_text="Search recent append-only audit entries.",
        capability=COMMAND_CAPABILITIES["audit search"],
        handler=_command_audit_search,
    )
    audit_search.add_argument("--target")
    audit_search.add_argument(
        "--cursor",
        help=(
            "Opaque cursor value from the previous response's next URL. "
            "Pass it unchanged."
        ),
    )

    time_parser = command.add_parser("time", help="Log staff time.")
    time_commands = time_parser.add_subparsers(
        dest="time_command",
        required=True,
    )
    time_log = _leaf(
        time_commands,
        "log",
        help_text="Log attributed staff time against a request.",
        capability=COMMAND_CAPABILITIES["time log"],
        handler=_command_time_log,
    )
    time_log.add_argument(
        "--request-id",
        required=True,
        help="Internal request UUID returned by requests get/list.",
    )
    time_log.add_argument("--date", required=True)
    time_log.add_argument("--minutes", required=True, type=int)
    time_log.add_argument("--activity", required=True)

    correspondence = command.add_parser(
        "correspondence",
        help="Create editable requester-correspondence drafts.",
    )
    correspondence_commands = correspondence.add_subparsers(
        dest="correspondence_command",
        required=True,
    )
    draft_parser = correspondence_commands.add_parser(
        "draft",
        help="Create a draft; approval and queueing remain human-only.",
    )
    draft_commands = draft_parser.add_subparsers(
        dest="draft_command",
        required=True,
    )

    _add_authored_draft(
        draft_commands,
        "general",
        COMMAND_CAPABILITIES["correspondence draft general"],
        "Create an editable general correspondence draft.",
    )
    acknowledgment = _leaf(
        draft_commands,
        "acknowledgment",
        help_text="Create a versioned acknowledgment draft.",
        capability=COMMAND_CAPABILITIES[
            "correspondence draft acknowledgment"
        ],
        handler=_command_correspondence_draft,
    )
    acknowledgment.add_argument("request_number")
    acknowledgment.add_argument("--estimated-completion")
    acknowledgment.set_defaults(draft_kind="acknowledgment")

    clarification = _leaf(
        draft_commands,
        "clarification",
        help_text="Create a clarification-request draft.",
        capability=COMMAND_CAPABILITIES[
            "correspondence draft clarification"
        ],
        handler=_command_correspondence_draft,
    )
    clarification.add_argument("request_number")
    clarification.add_argument("--questions", required=True)
    clarification.add_argument("--due", required=True)
    clarification.set_defaults(draft_kind="clarification")

    estimate = _leaf(
        draft_commands,
        "estimate-revision",
        help_text="Prepare an estimate-revision draft for human review.",
        capability=COMMAND_CAPABILITIES[
            "correspondence draft estimate-revision"
        ],
        handler=_command_correspondence_draft,
    )
    estimate.add_argument("request_number")
    estimate.add_argument("--new-date", required=True)
    estimate.add_argument("--reason", required=True)
    estimate.set_defaults(draft_kind="estimate-revision")

    installment = _leaf(
        draft_commands,
        "installment",
        help_text="Create a non-sendable installment-notice draft.",
        capability=COMMAND_CAPABILITIES[
            "correspondence draft installment"
        ],
        handler=_command_correspondence_draft,
    )
    installment.add_argument("request_number")
    installment.add_argument("--installment-id", type=int, required=True)
    installment.add_argument("--download-url", required=True)
    final_group = installment.add_mutually_exclusive_group(required=True)
    final_group.add_argument(
        "--final",
        action="store_true",
        dest="is_final",
    )
    final_group.add_argument(
        "--not-final",
        action="store_false",
        dest="is_final",
    )
    installment.set_defaults(draft_kind="installment")

    _add_authored_draft(
        draft_commands,
        "responsive-records",
        COMMAND_CAPABILITIES[
            "correspondence draft responsive-records"
        ],
        "Create a responsive-records notice draft.",
    )
    _add_authored_draft(
        draft_commands,
        "closure",
        COMMAND_CAPABILITIES["correspondence draft closure"],
        "Create a closure draft for Records Officer review.",
    )
    denial = _add_authored_draft(
        draft_commands,
        "denial",
        COMMAND_CAPABILITIES["correspondence draft denial"],
        "Create a full-exemption denial draft without denying the request.",
    )
    denial.add_argument(
        "--exemption-id",
        action="append",
        type=int,
        required=True,
        dest="exemption_ids",
        help="Active exemption citation ID. Repeat as needed.",
    )

    return parser


def _not_none(**values):
    return {
        key: value
        for key, value in values.items()
        if value is not None
    }


def _command_me(client, _args):
    return client.request("GET", "/api/staff/auth/me")


def _command_capabilities(client, _args):
    return client.request("GET", "/api/staff/capabilities/")


def _command_requests_list(client, args):
    return client.request(
        "GET",
        "/api/staff/requests/",
        params=_not_none(
            status=args.status,
            search=args.search,
            page=args.page,
        ),
    )


def _command_requests_get(client, args):
    return client.request(
        "GET",
        f"/api/staff/requests/{quote(args.number, safe='')}/",
    )


def _command_requests_transition(client, args):
    return client.request(
        "POST",
        f"/api/staff/requests/{quote(args.number, safe='')}/transition/",
        body={"to_status": args.to_status, "note": args.note},
        route_values={"number": args.number},
    )


def _command_tasks_list(client, args):
    return client.request(
        "GET",
        "/api/staff/collection-tasks/",
        params=_not_none(
            request=args.request_number,
            state=args.state,
            urgent="true" if args.urgent else None,
            page=args.page,
        ),
    )


def _command_tasks_update(client, args):
    body = _not_none(
        state=args.state,
        due_date=args.due_date,
        instructions=args.instructions,
        assigned_to_id=args.assignee_id,
    )
    if args.unassign:
        body["assigned_to_id"] = None
    if not body:
        raise CLIError(
            "tasks update needs at least one of --state, --due-date, "
            "--instructions, --assignee-id, or --unassign."
        )
    return client.request(
        "PATCH",
        f"/api/staff/collection-tasks/{args.id}/",
        body=body,
    )


def _command_systems_list(client, _args):
    return client.request("GET", "/api/staff/record-systems/")


def _command_exemptions_list(client, _args):
    return client.request("GET", "/api/staff/exemptions/")


def _command_audit_search(client, args):
    return client.request(
        "GET",
        "/api/staff/audit-log/",
        params=_not_none(
            target=args.target,
            cursor=unquote(args.cursor) if args.cursor is not None else None,
        ),
    )


def _command_time_log(client, args):
    return client.request(
        "POST",
        "/api/staff/time-entries/",
        body={
            "request": args.request_id,
            "date": args.date,
            "minutes": args.minutes,
            "activity": args.activity,
        },
    )


def _recipients(args):
    recipients = []
    for recipient_type in ("to", "cc", "bcc"):
        for email in getattr(args, recipient_type, []):
            recipients.append(  # noqa: PERF401 - nested over three recipient types
                {
                    "recipient_type": recipient_type,
                    "email": email,
                }
            )
    return recipients


def _command_correspondence_draft(client, args):
    kind = args.draft_kind
    body = {"request_number": args.request_number}
    path_kind = kind
    if kind in {
        "general",
        "responsive-records",
        "closure",
        "denial",
    }:
        body.update(subject=args.subject, body=args.body)
        recipients = _recipients(args)
        if recipients:
            body["recipients"] = recipients
    if kind == "denial":
        body["exemption_citation_ids"] = args.exemption_ids
    elif kind == "acknowledgment":
        if args.estimated_completion:
            body["estimated_completion"] = args.estimated_completion
    elif kind == "clarification":
        body.update(
            clarification_questions=args.questions,
            clarification_due=args.due,
        )
    elif kind == "estimate-revision":
        body.update(new_date=args.new_date, reason=args.reason)
    elif kind == "installment":
        body.update(
            installment_id=args.installment_id,
            download_url=args.download_url,
            is_final=args.is_final,
        )
    return client.request(
        "POST",
        f"/api/staff/correspondence/drafts/{path_kind}/",
        body=body,
        route_values={"number": args.request_number},
    )


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif isinstance(value, bool):
        rendered = "yes" if value else "no"
    else:
        rendered = str(value)
    if len(rendered) > 60:
        return rendered[:57] + "..."
    return rendered.replace("\n", " ")


def _table(rows, columns):
    normalized = [
        [_cell(row.get(column)) for column in columns]
        for row in rows
    ]
    widths = [
        max(
            len(column),
            *(len(row[index]) for row in normalized),
        )
        for index, column in enumerate(columns)
    ]

    def render(values):
        return "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(values)
        ).rstrip()

    lines = [
        render(list(columns)),
        render(["-" * width for width in widths]),
    ]
    lines.extend(render(row) for row in normalized)
    return "\n".join(lines)


def _human_output(payload):
    if payload is None:
        return "Done."
    if (
        isinstance(payload, Mapping)
        and isinstance(payload.get("capabilities"), list)
    ):
        rows = payload["capabilities"]
        return _table(
            rows,
            ("id", "method", "path", "execution", "risk"),
        )
    if isinstance(payload, Mapping) and isinstance(payload.get("results"), list):
        rows = payload["results"]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None

    if rows is not None:
        if not rows:
            return "No results."
        if not all(isinstance(row, Mapping) for row in rows):
            return "\n".join(_cell(row) for row in rows)
        available = {
            key
            for row in rows
            for key in row
        }
        columns = [
            column
            for column in _PREFERRED_COLUMNS
            if column in available
        ][:8]
        if not columns:
            columns = list(rows[0].keys())[:8]
        return _table(rows, columns)

    if isinstance(payload, Mapping):
        rows = [
            {"field": key, "value": value}
            for key, value in payload.items()
        ]
        return _table(rows, ("field", "value"))
    return _cell(payload)


def _emit_error(error, *, json_output):
    if json_output:
        print(
            json.dumps(error.payload, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
    else:
        print(f"ERROR: {error}", file=sys.stderr)


def main(argv=None, *, environ=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        base_url, token = _required_configuration(args, environment)
        if args.timeout <= 0:
            raise CLIError("--timeout must be greater than zero.")
        client = PRRClient(base_url, token, timeout=args.timeout)
        payload = args.handler(client, args)
    except CLIError as exc:
        _emit_error(
            exc,
            json_output=getattr(args, "json_output", False),
        )
        return exc.exit_code

    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(_human_output(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
