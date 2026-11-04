# ABOUTME: Machine-readable contract mapping every staff GUI operation to its
# ABOUTME: agent REST primitive, execution policy, risk, and MCP discovery path.
from copy import deepcopy

from rest_framework import generics, serializers
from rest_framework.response import Response

AGENT_NATIVE_GUIDE_URL = "https://every.to/guides/agent-native"

AGENT_NATIVE_PRINCIPLES = {
    "parity": "Every GUI outcome must have an agent-accessible primitive or an explicit human handoff.",
    "granularity": "Capabilities describe atomic domain actions; agents compose them into outcomes.",
    "composability": "REST primitives stay reusable instead of embedding agent judgment in the API.",
    "legibility": "Agent actions use the same records, actors, validation, events, and audit trail as the GUI.",
}

#: What ``mcp_tool`` means on the wire.
#:
#: This catalog is itself read by agents, over the MCP tool that serves it,
#: and an agent that read ``mcp_tool: null`` as "parity promised but tool
#: missing" is exactly what issue #12 reported. Explaining the field in the
#: response is the fix that reaches that reader; a source comment is not.
MCP_TOOL_FIELD_POLICY = (
    "mcp_tool names the one MCP tool that executes this capability, for "
    "discovery. It is not a parity promise, and null does not mean agents "
    "are excluded: every row's agent_transport is rest, so a direct "
    "capability with no MCP tool is executed by calling its own method and "
    "path with an attributed personal access token. The MCP server exposes "
    "the workflows agents drive rather than one tool per route, so most "
    "rows name no tool."
)

AGENT_CONSTRAINT_DEFINITIONS = {
    "anonymous_write_only": (
        "The sole anonymous business operation accepts a new request and "
        "exposes no requester, request, status, or operational read surface."
    ),
    "accountable_human_actor": (
        "Execution must be attributed to the accountable staff member, "
        "not a service account."
    ),
    "interactive_staff_session": (
        "Token and integration callers receive an explicit handoff; the "
        "accountable human executes this action in the staff application."
    ),
    "counsel_review": "District counsel must approve the governing legal content or taxonomy.",
    "correspondence_delivery_state": (
        "Acknowledged, clarification, and denied states are applied only by the "
        "correspondence service after confirmed delivery; generic transitions "
        "cannot create them."
    ),
    "clarification_resolution_state": (
        "An open clarification may return to active work only through "
        "deliberate human resolution after a received response and substantive "
        "notes. A human-attributed withdrawn or abandoned closure instead "
        "atomically cancels the open cycle with a substantive reason."
    ),
    "denial_delivery_required": (
        "Denied is not an administrative transition; the correspondence "
        "service applies it only after an approved full-exemption denial with "
        "a frozen ExemptionCitation basis has confirmed delivery."
    ),
    "terminal_reason_required": (
        "An administrative terminal closure requires a current Records "
        "Officer and a substantive case-specific reason."
    ),
    "destructive": (
        "The action removes data or configuration and requires explicit "
        "human confirmation."
    ),
    "external_side_effects": (
        "The action changes communication with or delivery to an external system."
    ),
    "pii_minimization": (
        "Do not collect or require requester identity data beyond what is "
        "needed to respond."
    ),
    "malware_scanned": (
        "Requester-supplied bytes remain quarantined and unavailable until "
        "the configured malware scanner reports them clean."
    ),
    "explicit_confirmation": (
        "The caller must pass confirmed=true with the exact selection. A "
        "batch touches many cases at once, so the confirmation is what "
        "separates an intended action from a mis-click on a list."
    ),
    "batch_case_cap": (
        "At most 50 cases in one batch, one message per case, and one audit "
        "row and one independent delivery for each."
    ),
    "records_officer_authority": (
        "A currently authorized Records Officer must perform the action."
    ),
    "district_accounts_only": (
        "Every custodian must be an account in the configured district Google Workspace domain."
    ),
    "explicit_vault_scope": (
        "The officer must explicitly choose specific district accounts or all district accounts; "
        "an empty custodian list never silently widens a search."
    ),
    "no_export_or_hold": (
        "The operation may create a Vault matter container and count operation, but no hold, export, saved query, collection task, or legal outcome."
    ),
    "reviewed_vault_count": (
        "Collection must use the exact completed count input digest the "
        "Records Officer reviewed and confirmed."
    ),
    "targeted_hold_before_export": (
        "Named-account Gmail collection places or reuses the targeted Vault "
        "hold before starting the export; an all-account export is explicit "
        "and does not claim an organization-wide hold."
    ),
    "append_only_audit": (
        "The action records attributable before/after evidence in the "
        "append-only audit log."
    ),
    "integrity_verified": (
        "Every source and delivery artifact is checked against its recorded "
        "byte length and SHA-256 digest before use."
    ),
    "immutable_artifact": (
        "The generated artifact uses an opaque key and a create-only object "
        "write; application code has no overwrite path."
    ),
    "expiring_possession_link": (
        "Requester access is limited to one released installment by an "
        "unguessable possession capability with a fixed expiration."
    ),
    "requester_notice_required": (
        "Every initial release or expired-link renewal atomically creates or "
        "reuses the matching requester notice for human approval; the token "
        "cannot rotate through a notice-free service path."
    ),
    "csrf_protected": (
        "Browser execution requires the same-origin CSRF cookie and header."
    ),
    "idempotent_submission": (
        "Replaying one signed form token with identical content returns the "
        "same legal request instead of creating another."
    ),
    "email_intake_only": (
        "The closure is available only when retained inbound evidence proves "
        "the request was auto-created from an unmatched bare-mailbox email."
    ),
    "rate_limited": (
        "Shared-cache per-source and global hard ceilings bound anonymous use."
    ),
    "secret_write_only": (
        "Existing secret material is never retrievable; a generated replacement "
        "may appear only in its one-time create or rotate response."
    ),
    "secret_returned_once": (
        "The newly generated credential is returned only in the create or "
        "rotate response and is never stored or returned again."
    ),
    "active_target_status_only": (
        "A reopened request may land only in an active working status. "
        "Acknowledged, clarification and denied are true only once a message "
        "reached the requester, and reopening sends nothing."
    ),
    "no_new_statutory_deadline": (
        "Reopening does not restart the RCW 42.56.520 response clock. The "
        "deadline stays derived from the original receipt and accumulated "
        "tolling, so a request that closed overdue reopens overdue."
    ),
    "application_admin_authority": (
        "Only a current Application Admin may perform the action."
    ),
    "district_configuration_change": (
        "The action changes how the deployed system behaves for every case at "
        "once, takes effect without a redeploy, and is recorded in the "
        "setting's append-only history with the named actor and reason."
    ),
    "substantive_reason": (
        "The accountable human must provide a case-specific, legally "
        "substantive reason."
    ),
    "bounded_human_selection": (
        "One confirmation applies to an explicitly selected set of at most 200 "
        "records, or -- in the staff application only -- to every record a "
        "filter matches (at most 10,000) after the reviewer confirms the "
        "server's exact count and digest of that set. Never to a model's "
        "proposals accepted in bulk."
    ),
    "human_legal_judgment": (
        "A named reviewer must inspect the retained record and confirm the "
        "legal outcome; advisory automation may not apply it."
    ),
    "exemption_citation_required": (
        "Every redaction or full withholding must reference an active, "
        "protected ExemptionCitation row."
    ),
    "distinct_second_reviewer": (
        "A citation flagged for heightened review requires approval by a "
        "different currently authorized reviewer."
    ),
    "rasterized_burn_in": (
        "Released redactions are rebuilt as image-only PDF pages so covered "
        "text and document objects are not recoverable."
    ),
    "adopted_fee_schedule": (
        "A fee schedule is usable only after an accountable officer records "
        "district-counsel approval and formal agency adoption references."
    ),
    "statutory_fee_caps": (
        "RCW 42.56.120 statutory rates, the $2 flat alternative, and the "
        "ten-percent deposit ceiling are validated by the domain service."
    ),
    "append_only_fee_evidence": (
        "Fee schedules, assessment revisions, and payment receipts are "
        "immutable append-only evidence enforced by application and database."
    ),
    "voluntarily_known_requester_type": (
        "Requester type may be classified only from information already "
        "volunteered or known; staff must not ask for it for reporting."
    ),
    "human_external_transfer": (
        "The application prepares a review worksheet but never authenticates "
        "to or submits data into the external government reporting system."
    ),
    "annual_source_reconciliation": (
        "Finance, HR, risk, litigation, retention, and unallocated-time totals "
        "must be reconciled by accountable staff and recorded with methodology."
    ),
    "advisory_only": (
        "The model output is untrusted proposal data and is permanently marked "
        "not applied; it cannot change any legal or workflow field."
    ),
    "approved_ai_provider": (
        "The disabled-by-default provider may be enabled only after privacy, "
        "security, contracting, counsel, and Records Officer approval for the data sent."
    ),
    "prompt_injection_resistant": (
        "Request and record content are treated as untrusted data; output is "
        "strictly schema-, vocabulary-, count-, and size-validated."
    ),
    "coordinate_grounding": (
        "The provider may select only server-issued PDF text-span identifiers; "
        "the server validates those identifiers and derives page coordinates locally."
    ),
    "adequate_search_evidence": (
        "A named Records Officer must document detailed locations, custodians, "
        "methods, date ranges, results, completed collection tasks, obvious leads, "
        "and counsel approval before a no-records closure can be delivered."
    ),
}


def _capability(
    capability_id,
    outcome,
    method,
    path,
    ui_routes,
    *,
    execution="direct",
    risk="read",
    mcp_tool=None,
    constraints=None,
):
    """Build one catalog row.

    ``execution`` is the policy: "direct" means an agent may execute this
    with its own attributed token, "human_confirmation" that it needs an
    explicit confirmed selection, "human_only" that a token caller gets a
    handoff instead.

    ``mcp_tool`` is a narrower claim, and the two are easy to confuse. It
    names the one MCP tool that *executes this operation*, for discovery; it
    is not a parity promise and ``None`` does not mean agents are shut out.
    ``agent_transport`` is "rest" on every row, so a "direct" capability with
    no MCP tool is reachable the same way the catalog itself says: a
    ``POST``/``PATCH`` to ``path`` with a personal access token. The MCP
    server stays well under psd-dev-standards standards/07-mcp.md's ~15-tool
    ceiling by exposing the workflows an agent actually drives, not one tool
    per REST route, so most rows name no tool and always will.
    ``test_the_capability_catalog_names_every_executing_tool`` keeps the
    rows that do name one from drifting.
    """
    return {
        "id": capability_id,
        "outcome": outcome,
        "method": method,
        "path": path,
        "ui_routes": ui_routes,
        "agent_transport": "rest",
        "execution": execution,
        "risk": risk,
        "mcp_tool": mcp_tool,
        "constraints": constraints or [],
    }


# One row per distinct API operation used by the production React application.
# Query-string variations intentionally share one capability: filters are inputs
# to an atomic list operation, not separate powers. The static parity test scans
# frontend/src and fails if a GUI operation appears without a row here.
CAPABILITIES = (
    _capability(
        "capabilities.list",
        "Discover the live staff API capability, risk, constraint, and "
        "execution-policy catalog.",
        "GET",
        "/api/staff/capabilities/",
        ["/configuration/agent-access"],
        mcp_tool="psd_prr_capabilities_list",
    ),
    _capability(
        "requests.submit_public",
        "Submit one new public records request through the canonical intake "
        "pipeline without exposing any application read surface.",
        "POST",
        "/api/public/requests/",
        [],
        execution="anonymous",
        risk="high",
        constraints=[
            "anonymous_write_only",
            "csrf_protected",
            "rate_limited",
            "idempotent_submission",
            "pii_minimization",
            "malware_scanned",
            "append_only_audit",
        ],
    ),
    _capability(
        "api_tokens.list",
        "Review the current user's named personal access token metadata "
        "without retrieving any credential.",
        "GET",
        "/api/staff/auth/api-tokens/",
        ["/configuration/agent-access"],
        execution="human_only",
        risk="moderate",
        constraints=["interactive_staff_session"],
    ),
    _capability(
        "api_tokens.create",
        "Create a named, expiring personal access token; the credential is "
        "returned exactly once.",
        "POST",
        "/api/staff/auth/api-tokens/",
        ["/configuration/agent-access"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "append_only_audit",
            "secret_returned_once",
        ],
    ),
    _capability(
        "api_tokens.rotate",
        "Revoke one personal access token and return its named replacement "
        "credential exactly once.",
        "POST",
        "/api/staff/auth/api-tokens/{id}/rotate",
        ["/configuration/agent-access"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "append_only_audit",
            "secret_returned_once",
        ],
    ),
    _capability(
        "api_tokens.revoke",
        "Revoke one of the current user's personal access tokens while "
        "retaining its audit metadata.",
        "DELETE",
        "/api/staff/auth/api-tokens/{id}",
        ["/configuration/agent-access"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.list",
        "Browse, filter, search, and prioritize the request queue.",
        "GET",
        "/api/staff/requests/",
        ["/cases"],
        mcp_tool="psd_prr_requests_list",
    ),
    _capability(
        "requests.submit_staff",
        "Log a phone, mail, or in-person matter through the canonical legal "
        "intake pipeline as the accountable staff actor, as a public records "
        "request, a litigation/discovery pull, or a FERPA parent request. The "
        "kind is fixed at intake and decides which clock, which letters, and "
        "which report the matter is subject to.",
        "POST",
        "/api/staff/requests/intake/",
        ["/staff-intake"],
        risk="high",
        constraints=[
            "accountable_human_actor",
            "idempotent_submission",
            "pii_minimization",
            "malware_scanned",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.retrieve",
        "Read one request with its staff-visible timeline and related work.",
        "GET",
        "/api/staff/requests/{number}/",
        ["/requests/:number/overview", "/requests/:number/collection"],
        mcp_tool="psd_prr_request_get",
    ),
    _capability(
        "requests.assign_owner",
        "Assign or clear the active Records Officer accountable for one case.",
        "PATCH",
        "/api/staff/requests/{number}/",
        ["/requests/:number/overview"],
        risk="moderate",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.workspace_summary",
        "Read the bounded role-safe case header and canonical next-action projection.",
        "GET",
        "/api/staff/requests/{number}/workspace-summary/",
        [
            "/requests/:number/overview",
            "/requests/:number/messages",
            "/requests/:number/collection",
            "/requests/:number/records",
            "/requests/:number/releases",
            "/requests/:number/activity",
        ],
    ),
    _capability(
        "requests.activity",
        "Read the typed role-safe case feed of lifecycle, work, and delivery milestones.",
        "GET",
        "/api/staff/requests/{number}/activity/",
        ["/requests/:number/activity"],
    ),
    _capability(
        "requests.closure_readiness",
        "Read case closure blocker counts without applying a legal or transport outcome.",
        "GET",
        "/api/staff/requests/{number}/closure-readiness/",
        ["/requests/:number/releases"],
    ),
    _capability(
        "requests.advisory_intent.read",
        "Read the latest bounded request-intent proposal and its provenance.",
        "GET",
        "/api/staff/requests/{number}/advisory-intent/",
        ["/requests/:number/collection"],
        constraints=["advisory_only"],
    ),
    _capability(
        "requests.advisory_intent.request",
        "Queue a disabled-by-default model proposal for search terms, systems, and clarification questions.",
        "POST",
        "/api/staff/requests/{number}/advisory-intent/",
        ["/requests/:number/collection"],
        risk="high",
        constraints=[
            "accountable_human_actor",
            "approved_ai_provider",
            "prompt_injection_resistant",
            "advisory_only",
            "human_legal_judgment",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.advisory_intake.read",
        "Read the latest suggested intake label (new, follow-up, duplicate, "
        "related, non-request, spam) and whether an officer answered it.",
        "GET",
        "/api/staff/requests/{number}/advisory-intake/",
        ["/requests/:number/overview"],
        constraints=["advisory_only"],
    ),
    _capability(
        "requests.advisory_intake.request",
        "Queue a model's suggested intake label for this case; it merges, "
        "closes, and sends nothing.",
        "POST",
        "/api/staff/requests/{number}/advisory-intake/",
        ["/requests/:number/overview"],
        risk="high",
        constraints=[
            "accountable_human_actor",
            "approved_ai_provider",
            "prompt_injection_resistant",
            "advisory_only",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.advisory_intake.feedback",
        "Record whether the officer confirmed or dismissed the suggested "
        "intake label, and what they judged it to be.",
        "POST",
        "/api/staff/requests/{number}/advisory-intake/feedback/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="moderate",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "requests.vault_counts.read",
        "Read the latest human-scoped Google Vault Gmail count and its provenance.",
        "GET",
        "/api/staff/requests/{number}/vault-counts/",
        ["/requests/:number/collection"],
        constraints=["records_officer_authority", "append_only_audit"],
    ),
    _capability(
        "requests.vault_counts.request",
        "Count possible Gmail messages for human-approved terms, an explicit district-account scope, and dates without exporting or applying a workflow outcome.",
        "POST",
        "/api/staff/requests/{number}/vault-counts/",
        ["/requests/:number/collection"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "district_accounts_only",
            "explicit_vault_scope",
            "no_export_or_hold",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.vault_collection.start",
        "Create a durable Vault Gmail collection task from the exact reviewed count, then hold named custodians and export into the review queue.",
        "POST",
        "/api/staff/requests/{number}/vault-collection/",
        ["/requests/:number/collection"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "reviewed_vault_count",
            "district_accounts_only",
            "explicit_vault_scope",
            "targeted_hold_before_export",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.record_uploads.create",
        "Add one or more files to an open case as unreviewed candidate "
        "records; each file is quarantined, malware-scanned, deduplicated by "
        "SHA-256, and retained immutably, with a per-file outcome.",
        "POST",
        "/api/staff/requests/{number}/record-uploads/",
        ["/requests/:number/records"],
        risk="moderate",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "malware_scanned",
            "integrity_verified",
            "immutable_artifact",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.transition",
        "Apply an authorized administrative lifecycle transition; "
        "communication-driven denial is excluded.",
        "POST",
        "/api/staff/requests/{number}/transition/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "denial_delivery_required",
            "terminal_reason_required",
            "correspondence_delivery_state",
            "clarification_resolution_state",
        ],
    ),
    _capability(
        "requests.close_case",
        "Close an open case as withdrawn or abandoned with a substantive "
        "written reason, atomically cancelling any open clarification without "
        "inventing a response.",
        "POST",
        "/api/staff/requests/{number}/close/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "substantive_reason",
            "clarification_resolution_state",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.close_non_request",
        "Administratively close an auto-created email that is not a public "
        "records request while retaining the request, raw evidence, timeline, "
        "and audit trail.",
        "POST",
        "/api/staff/requests/{number}/close-non-request/",
        ["/inbox", "/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "substantive_reason",
            "email_intake_only",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.reopen",
        "Reopen a closed request into an active working status with a named "
        "Records Officer and a written reason, leaving the closure on the "
        "timeline.",
        "POST",
        "/api/staff/requests/{number}/reopen/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "substantive_reason",
            "active_target_status_only",
            "no_new_statutory_deadline",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.merge",
        "Merge a duplicate case into the case of record: every requester "
        "email moves to the target, the duplicate closes as Closed – "
        "Duplicate with a link back, and the target keeps its own received "
        "date and statutory deadline.",
        "POST",
        "/api/staff/requests/{number}/merge/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "substantive_reason",
            "no_new_statutory_deadline",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.read_case_notes",
        "Read one case's internal staff notes, newest first. Internal to the "
        "district: a note never reaches the requester through any letter, "
        "production, or export.",
        "GET",
        "/api/staff/requests/{number}/notes/",
        ["/requests/:number", "/inbox"],
    ),
    _capability(
        "requests.add_case_note",
        "Append one internal staff note to a case under the acting staff "
        "member's name. Append-only, audited, and never sent to the "
        "requester; it changes no status, deadline, or correspondence.",
        "POST",
        "/api/staff/requests/{number}/notes/",
        ["/requests/:number"],
        risk="low",
        constraints=[
            "accountable_human_actor",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.mark_inbox_handled",
        "Mark every open requester-email row on one case Handled by the "
        "acting Records Officer without changing any message or the case.",
        "POST",
        "/api/staff/requests/{number}/inbox-handled/",
        ["/inbox"],
        risk="moderate",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.verify_ferpa_identity",
        "Record, under a named Records Officer, that a FERPA parent or "
        "eligible student proved who they are (34 CFR 99.10): the method and "
        "what the officer relied on. Discloses nothing, sends nothing, and "
        "changes no status; it is the precondition for releasing an education "
        "record to that person.",
        "POST",
        "/api/staff/requests/{number}/ferpa-identity-verification/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "substantive_reason",
            "human_legal_judgment",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.record_external_initial_response",
        "Record, on a Records Officer's written statement, that the "
        "RCW 42.56.520 first response reached the requester through a channel "
        "this system did not deliver (the retired NextRequest tenant, district "
        "email, or mail): the channel, the date when known, and where a copy "
        "can be found. Sends nothing and starts no clock.",
        "POST",
        "/api/staff/requests/{number}/external-initial-response/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "substantive_reason",
            "human_legal_judgment",
            "append_only_audit",
        ],
    ),
    _capability(
        "intake_attachments.download",
        "Obtain a short-lived download for a malware-scanned intake attachment.",
        "POST",
        "/api/staff/intake-attachments/{id}/download/",
        ["/requests/:number/overview"],
        risk="moderate",
        constraints=["malware_scanned"],
    ),
    _capability(
        "collection_tasks.list",
        "Browse and filter collection assignments.",
        "GET",
        "/api/staff/collection-tasks/",
        ["/requests/:number/collection"],
        mcp_tool="psd_prr_collection_tasks_list",
    ),
    _capability(
        "collection_tasks.retrieve",
        "Open the exact authorized collection assignment linked from work.",
        "GET",
        "/api/staff/collection-tasks/{id}/",
        ["/requests/:number/collection"],
    ),
    _capability(
        "collection_tasks.create",
        "Create and assign a manual department search task for one request.",
        "POST",
        "/api/staff/collection-tasks/",
        ["/requests/:number/collection"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "records_officer_authority",
            "accountable_human_actor",
        ],
    ),
    _capability(
        "staff_users.list",
        "List minimal active-staff labels for accountable task assignment.",
        "GET",
        "/api/staff/staff-users/",
        [
            "/requests/:number/overview",
            "/requests/:number/messages",
            "/requests/:number/collection",
        ],
    ),
    _capability(
        "collection_tasks.update",
        "Update task state, due date, or assignee.",
        "PATCH",
        "/api/staff/collection-tasks/{id}/",
        ["/requests/:number/collection"],
        execution="human_confirmation",
        risk="moderate",
    ),
    _capability(
        "collection_tasks.supersede",
        "Take one collection's unreviewed records out of review, keeping "
        "every record as evidence.",
        "POST",
        "/api/staff/collection-tasks/{id}/supersede/",
        ["/requests/:number/collection"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "explicit_confirmation",
            "append_only_audit",
        ],
    ),
    _capability(
        "preservations.list",
        "Browse camera-footage preservations and holds.",
        "GET",
        "/api/staff/preservations/",
        ["/requests/:number/collection"],
    ),
    _capability(
        "preservations.exclude_camera",
        "Exclude one camera from an active preservation hold.",
        "POST",
        "/api/staff/preservations/{id}/exclude-camera/",
        ["/requests/:number/collection"],
        execution="human_only",
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "record_systems.list",
        "Browse and filter the district record-system registry.",
        "GET",
        "/api/staff/record-systems/",
        ["/configuration/registry", "/requests/:number/collection"],
        mcp_tool="psd_prr_record_systems_list",
    ),
    _capability(
        "record_systems.create",
        "Add a district record system to the triage registry.",
        "POST",
        "/api/staff/record-systems/",
        ["/configuration/registry"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_systems.update",
        "Update custodians, default task ownership, retention data, verification state, or activation.",
        "PATCH",
        "/api/staff/record-systems/{id}/",
        ["/configuration/registry"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "automatic_message_templates.list",
        "Inspect active, draft, and retired automatic-message template versions.",
        "GET",
        "/api/staff/automatic-message-templates/",
        ["/configuration/message-templates"],
    ),
    _capability(
        "automatic_message_templates.create",
        "Create an editable inactive version of an automatic-message template.",
        "POST",
        "/api/staff/automatic-message-templates/",
        ["/configuration/message-templates"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "automatic_message_templates.update",
        "Edit an inactive automatic-message template draft.",
        "PATCH",
        "/api/staff/automatic-message-templates/{id}/",
        ["/configuration/message-templates"],
        execution="human_confirmation",
        risk="moderate",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "automatic_message_templates.preview",
        "Render a PII-free example from one managed template version.",
        "POST",
        "/api/staff/automatic-message-templates/{id}/preview/",
        ["/configuration/message-templates"],
    ),
    _capability(
        "automatic_message_templates.activate",
        "Activate one exact immutable template version for future automatic messages.",
        "POST",
        "/api/staff/automatic-message-templates/{id}/activate/",
        ["/configuration/message-templates"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "accountable_human_actor",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "exemptions.list",
        "Browse the exemption-citation taxonomy.",
        "GET",
        "/api/staff/exemptions/",
        [
            "/configuration/exemptions",
            "/requests/:number/messages",
            "/requests/:number/records",
        ],
        mcp_tool="psd_prr_exemptions_list",
    ),
    _capability(
        "exemptions.create",
        "Add a statutory exemption citation.",
        "POST",
        "/api/staff/exemptions/",
        ["/configuration/exemptions"],
        execution="human_confirmation",
        risk="high",
        constraints=["counsel_review"],
    ),
    _capability(
        "exemptions.update",
        "Update or deactivate a statutory exemption citation.",
        "PATCH",
        "/api/staff/exemptions/{id}/",
        ["/configuration/exemptions"],
        execution="human_confirmation",
        risk="high",
        constraints=["counsel_review"],
    ),
    _capability(
        "requests.correct_requester",
        "Correct the requester identity a forwarded submission recorded, with "
        "a written reason, so statutory notices address the actual requester.",
        "POST",
        "/api/staff/requests/{number}/correct-requester/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "substantive_reason",
            "accountable_human_actor",
            "append_only_audit",
        ],
    ),
    # No staff UI route: the /requesters screen was unreachable from the shell
    # (no navigation entry anywhere linked to it) and was removed. The REST
    # primitives stay so agents keep the capability; rule 10 requires every GUI
    # outcome to have an agent primitive, not the converse. Restore the
    # ui_routes entry if a requester directory is ever added to the navigation.
    _capability(
        "requesters.list",
        "Browse and search requester contact records.",
        "GET",
        "/api/staff/requesters/",
        [],
    ),
    _capability(
        "requesters.update",
        "Update a requester's optional contact details without changing identity email.",
        "PATCH",
        "/api/staff/requesters/{id}/",
        [],
        execution="human_confirmation",
        risk="moderate",
        constraints=["pii_minimization"],
    ),
    _capability(
        "record_items.list",
        "Browse and filter the processed-record inventory.",
        "GET",
        "/api/staff/record-items/",
        ["/requests/:number/records", "/requests/:number/releases"],
    ),
    _capability(
        "record_items.retrieve",
        "Open the exact authorized processed record linked from review work.",
        "GET",
        "/api/staff/record-items/{id}/",
        ["/requests/:number/records"],
    ),
    _capability(
        "record_items.facets",
        "Count the review inventory by tag, sender domain, custodian, review "
        "state, and advisory suggestion for the current filter.",
        "GET",
        "/api/staff/record-items/facets/",
        ["/requests/:number/records"],
    ),
    _capability(
        "record_items.bulk_selection",
        "Preview exactly which records one filter decision would change, "
        "with the count and digest a confirmation must send back.",
        "GET",
        "/api/staff/record-items/bulk-selection/",
        ["/requests/:number/records"],
    ),
    _capability(
        "record_items.bulk_review",
        "Record one named human responsiveness decision across a selected, "
        "visible set of records, or every record a filter matches after the "
        "reviewer confirms the exact count.",
        "POST",
        "/api/staff/record-items/bulk-review/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "human_legal_judgment",
            "bounded_human_selection",
            "interactive_staff_session",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.advisory_responsiveness.read",
        "Read the latest bounded responsiveness proposal and its provenance.",
        "GET",
        "/api/staff/record-items/{id}/advisory-responsiveness/",
        ["/requests/:number/records"],
        constraints=["advisory_only"],
    ),
    _capability(
        "record_items.advisory_responsiveness.request",
        "Queue a model suggestion of responsive, non-responsive, or uncertain without applying review state.",
        "POST",
        "/api/staff/record-items/{id}/advisory-responsiveness/",
        ["/requests/:number/records"],
        risk="high",
        constraints=[
            "accountable_human_actor",
            "approved_ai_provider",
            "prompt_injection_resistant",
            "advisory_only",
            "human_legal_judgment",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.advisory_redactions.read",
        "Read the latest coordinate-grounded AI redaction candidates and their named human dispositions.",
        "GET",
        "/api/staff/record-items/{id}/advisory-redactions/",
        ["/requests/:number/records"],
        constraints=["advisory_only", "integrity_verified"],
    ),
    _capability(
        "record_items.advisory_redactions.request",
        "Queue a verified-PDF Bedrock pass that proposes bounded source spans and active exemption citations without creating redactions.",
        "POST",
        "/api/staff/record-items/{id}/advisory-redactions/",
        ["/requests/:number/records"],
        risk="high",
        constraints=[
            "accountable_human_actor",
            "approved_ai_provider",
            "prompt_injection_resistant",
            "coordinate_grounding",
            "advisory_only",
            "human_legal_judgment",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "redaction_proposals.accept",
        "Accept one human-edited AI candidate through the normal exemption-backed redaction service.",
        "POST",
        "/api/staff/redaction-proposals/{id}/accept/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "human_legal_judgment",
            "exemption_citation_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "redaction_proposals.reject",
        "Reject one AI candidate with a substantive named human reason and no legal effect.",
        "POST",
        "/api/staff/redaction-proposals/{id}/reject/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "human_legal_judgment",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.build_derivative",
        "Render one non-PDF record into the reviewable PDF the preview and redaction burn-in require, leaving the original record untouched.",
        "POST",
        "/api/staff/record-items/{id}/build-derivative/",
        ["/requests/:number/records"],
        risk="moderate",
        constraints=[
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.render_media_redactions",
        "Burn every marked interval into a released copy of an audio or video record by re-encoding, retaining the original and a manifest of exactly what was applied.",
        "POST",
        "/api/staff/record-items/{id}/render-media-redactions/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "human_legal_judgment",
            "exemption_citation_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.opaque_disposition",
        "Record a named human decision, with stated basis and reasoning, for a record no available renderer can open.",
        "POST",
        "/api/staff/record-items/{id}/opaque-disposition/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "human_legal_judgment",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.stream",
        "Stream one audio or video record to the staff reviewer under the same role check and no-store headers as a page raster.",
        "GET",
        "/api/staff/record-items/{id}/stream/",
        ["/requests/:number/records"],
        risk="read",
    ),
    _capability(
        "media_redactions.list",
        "List the timed withholdings marked on audio and video records.",
        "GET",
        "/api/staff/media-redactions/",
        ["/requests/:number/records"],
        risk="read",
    ),
    _capability(
        "media_redactions.create",
        "Mark one interval of an audio or video record for withholding, with the exemption citation the log is built from.",
        "POST",
        "/api/staff/media-redactions/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "human_legal_judgment",
            "exemption_citation_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.preview_metadata",
        "Inspect verified PDF page-count metadata without exposing an object-store URL.",
        "GET",
        "/api/staff/record-items/{id}/preview-metadata/",
        ["/requests/:number/records"],
        constraints=["integrity_verified", "append_only_audit"],
    ),
    _capability(
        "record_items.preview_page",
        "Render one verified candidate-record page as a no-store raster preview.",
        "GET",
        "/api/staff/record-items/{id}/preview-page/",
        ["/requests/:number/records"],
        constraints=["integrity_verified", "append_only_audit"],
    ),
    _capability(
        "record_items.review",
        "Record a named human responsiveness or full-withholding decision.",
        "POST",
        "/api/staff/record-items/{id}/review/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "human_legal_judgment",
            "exemption_citation_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "redactions.list",
        "Read exemption-backed normalized redaction regions for one record.",
        "GET",
        "/api/staff/redactions/",
        ["/requests/:number/records"],
    ),
    _capability(
        "redactions.create",
        "Add one normalized redaction region after human review.",
        "POST",
        "/api/staff/redactions/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "human_legal_judgment",
            "exemption_citation_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "redactions.void",
        "Void a mistaken redaction while retaining its row, reason, and audit evidence.",
        "POST",
        "/api/staff/redactions/{id}/void/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=["substantive_reason", "append_only_audit"],
    ),
    _capability(
        "record_items.second_review",
        "Approve a heightened-risk review as a distinct named reviewer.",
        "POST",
        "/api/staff/record-items/{id}/second-review/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "human_legal_judgment",
            "distinct_second_reviewer",
            "append_only_audit",
        ],
    ),
    _capability(
        "record_items.render_redactions",
        "Build one immutable image-only PDF from the approved redaction plan.",
        "POST",
        "/api/staff/record-items/{id}/render-redactions/",
        ["/requests/:number/records"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "human_legal_judgment",
            "distinct_second_reviewer",
            "integrity_verified",
            "immutable_artifact",
            "rasterized_burn_in",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_schedules.list",
        "Review immutable district fee schedules and identify the currently effective adopted snapshot.",
        "GET",
        "/api/staff/fee-schedules/",
        ["/requests/:number/releases"],
    ),
    _capability(
        "jlarc_reports.retrieve",
        "Generate the reproducible RCW 40.14.026 baseline, all 15 metrics, request log, and data-quality warnings for one year.",
        "GET",
        "/api/staff/jlarc-reports/{id}/",
        ["/oversight/reporting"],
        constraints=["human_external_transfer"],
    ),
    _capability(
        "jlarc_reports.export_csv",
        "Download a no-store CSV worksheet for human review and transfer into JLARC's online system.",
        "GET",
        "/api/staff/jlarc-reports/{id}/export-csv/",
        ["/oversight/reporting"],
        constraints=["human_external_transfer"],
    ),
    _capability(
        "jlarc_reports.record_supplement",
        "Append a reconciled annual Finance, HR, risk, litigation, retention, recovery, and unallocated-time input revision.",
        "POST",
        "/api/staff/jlarc-reports/{id}/supplement/",
        ["/oversight/reporting"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "annual_source_reconciliation",
            "append_only_audit",
        ],
    ),
    _capability(
        "requests.classify_jlarc",
        "Append the voluntarily known requester type, fulfillment format, scanning fact, and statutory request-log descriptions for one request.",
        "POST",
        "/api/staff/requests/{number}/jlarc-classification/",
        ["/oversight/reporting", "/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "voluntarily_known_requester_type",
            "pii_minimization",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_schedules.activate",
        "Activate an immutable fee schedule after recording counsel approval and formal district adoption references.",
        "POST",
        "/api/staff/fee-schedules/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "counsel_review",
            "adopted_fee_schedule",
            "statutory_fee_caps",
            "append_only_fee_evidence",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_waiver_floors.list",
        "Review the adopted amounts below which an assessment prompts a waiver, and which one is currently effective.",
        "GET",
        "/api/staff/fee-waiver-floors/",
        ["/requests/:number/releases"],
    ),
    _capability(
        "fee_waiver_floors.activate",
        "Adopt, with a counsel reference, the amount below which an officer is prompted to waive a fee. It waives nothing on its own.",
        "POST",
        "/api/staff/fee-waiver-floors/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "counsel_review",
            "append_only_fee_evidence",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_deposit_gates.list",
        "Review the RCW 42.56.120(2) deposits holding collection or production on a case, and whether each is open, paid, or lifted.",
        "GET",
        "/api/staff/fee-deposit-gates/",
        ["/requests/:number/releases"],
    ),
    _capability(
        "fee_deposit_gates.apply",
        "Hold collection or production on one case until the requester pays a deposit of no more than ten percent of the estimated charge.",
        "POST",
        "/api/staff/fee-deposit-gates/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "adopted_fee_schedule",
            "statutory_fee_caps",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_deposit_gates.record_payment",
        "Record the deposit receipt that releases the held collection or production work on one case.",
        "POST",
        "/api/staff/fee-deposit-gates/{id}/record-payment/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "append_only_fee_evidence",
            "append_only_audit",
        ],
    ),
    _capability(
        "fee_deposit_gates.lift",
        "Release work the District was holding for an unpaid deposit, on a named officer's written reason.",
        "POST",
        "/api/staff/fee-deposit-gates/{id}/lift/",
        ["/requests/:number/releases"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.list",
        "Browse installment fees, payment state, and releases.",
        "GET",
        "/api/staff/installments/",
        ["/requests/:number/releases"],
    ),
    _capability(
        "installments.create",
        "Open the next installment for one request from explicitly selected, release-ready record items.",
        "POST",
        "/api/staff/installments/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "human_legal_judgment",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.set_items",
        "Replace one open installment's record membership before any package is prepared.",
        "POST",
        "/api/staff/installments/{id}/set-items/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "human_legal_judgment",
            "immutable_artifact",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.assess_fee",
        "Calculate or revise one installment fee under the current adopted schedule while retaining every prior assessment.",
        "POST",
        "/api/staff/installments/{id}/assess-fee/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "adopted_fee_schedule",
            "statutory_fee_caps",
            "append_only_fee_evidence",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.record_payment",
        "Record one attributable district receipt against the current installment fee assessment.",
        "POST",
        "/api/staff/installments/{id}/record-payment/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "append_only_fee_evidence",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.prepare",
        "Build one immutable, verified requester installment package.",
        "POST",
        "/api/staff/installments/{id}/prepare/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "integrity_verified",
            "immutable_artifact",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.generate_exemption_log",
        "Generate one immutable RCW 42.56.210(3) exemption and withholding log from approved review evidence.",
        "POST",
        "/api/staff/installments/{id}/generate-exemption-log/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "exemption_citation_required",
            "distinct_second_reviewer",
            "immutable_artifact",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.release",
        "Release one verified package behind a scoped, expiring requester link.",
        "POST",
        "/api/staff/installments/{id}/release/",
        # The Releases tab now offers one button, "Release and send"
        # (installments.release_and_send). Releasing without announcing stays
        # an addressable primitive for agents and recovery, but it is no
        # longer a screen, so claiming a UI route here would make the parity
        # test assert a button that does not exist.
        [],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "integrity_verified",
            "expiring_possession_link",
            "requester_notice_required",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.release_and_send",
        "Release one verified package and deliver its requester notice as a single accountable Records Officer action.",
        "POST",
        "/api/staff/installments/{id}/release-and-send/",
        ["/requests/:number/releases"],
        execution="human_only",
        risk="high",
        constraints=[
            "records_officer_authority",
            "interactive_staff_session",
            "human_legal_judgment",
            "integrity_verified",
            "expiring_possession_link",
            "external_side_effects",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.revoke",
        "Withdraw the requester's access to one released package with a recorded reason, leaving the artifact and audit trail intact.",
        "POST",
        "/api/staff/installments/{id}/revoke/",
        ["/requests/:number/releases"],
        execution="human_only",
        risk="high",
        constraints=[
            "records_officer_authority",
            "interactive_staff_session",
            "human_legal_judgment",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.set_access_code",
        "Require a separately communicated access code on one installment's requester download.",
        "POST",
        "/api/staff/installments/{id}/set-access-code/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "append_only_audit",
        ],
    ),
    _capability(
        "installments.clear_access_code",
        "Remove the access code from one installment's requester download.",
        "POST",
        "/api/staff/installments/{id}/clear-access-code/",
        ["/requests/:number/releases"],
        execution="human_confirmation",
        risk="high",
        constraints=[
            "records_officer_authority",
            "append_only_audit",
        ],
    ),
    _capability(
        "time_entries.list",
        "Browse staff time logged against requests.",
        "GET",
        "/api/staff/time-entries/",
        ["/requests/:number/overview"],
    ),
    _capability(
        "time_entries.create",
        "Log time against a request as the authenticated staff actor.",
        "POST",
        "/api/staff/time-entries/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="moderate",
    ),
    _capability(
        "time_entries.delete",
        "Delete a time entry.",
        "DELETE",
        "/api/staff/time-entries/{id}/",
        ["/requests/:number/overview"],
        execution="human_confirmation",
        risk="high",
        constraints=["destructive"],
    ),
    _capability(
        "counsel_domains.list",
        "Browse the district's own counsel email domains that tag records "
        "into the privilege queue.",
        "GET",
        "/api/staff/counsel-domains/",
        ["/configuration/counsel-domains"],
    ),
    _capability(
        "counsel_domains.create",
        "Add one district-counsel email domain so its records are separated "
        "for human review.",
        "POST",
        "/api/staff/counsel-domains/",
        ["/configuration/counsel-domains"],
        execution="human_confirmation",
        risk="moderate",
        constraints=["counsel_review", "append_only_audit"],
    ),
    _capability(
        "counsel_domains.update",
        "Correct or deactivate a district-counsel email domain.",
        "PATCH",
        "/api/staff/counsel-domains/{id}/",
        ["/configuration/counsel-domains"],
        execution="human_confirmation",
        risk="moderate",
        constraints=["counsel_review", "append_only_audit"],
    ),
    _capability(
        "counsel_domains.delete",
        "Delete a district-counsel email domain entry.",
        "DELETE",
        "/api/staff/counsel-domains/{id}/",
        ["/configuration/counsel-domains"],
        execution="human_confirmation",
        risk="moderate",
        constraints=["destructive", "append_only_audit"],
    ),
    _capability(
        "app_settings.list",
        "Browse every district setting the application manages — switches, "
        "limits, rates, timeouts, domains, address lists, Google service "
        "identities, sign-in configuration, the malware scanner, the Alta "
        "camera API, the portal address, and district closure days — with "
        "its current value, its "
        "type and bounds, its consequence, the result of its last live "
        "credential check, and who last changed it and why. A write-only "
        "secret reports only whether it is set, a fingerprint, and who set "
        "it; its value is never returned.",
        "GET",
        "/api/staff/settings/",
        ["/configuration/settings"],
    ),
    _capability(
        "app_settings.history",
        "Read the full append-only change history of one district setting.",
        "GET",
        "/api/staff/settings/{id}/history/",
        ["/configuration/settings"],
    ),
    _capability(
        "app_settings.update",
        "Change one district setting — a switch, a limit, a rate, a timeout, "
        "a domain list, the advisory model, a Google service identity, a "
        "sign-in setting, an integration address, the district closure "
        "days, or a write-only secret such as the Google sign-in client "
        "secret or the Alta token — with a written reason, taking effect "
        "without a "
        "redeploy. A secret can be replaced or cleared here and never read "
        "back, and a sign-in change that would leave no Application Admin "
        "able to sign in is refused.",
        "PATCH",
        "/api/staff/settings/{id}/",
        ["/configuration/settings"],
        execution="human_only",
        risk="high",
        constraints=[
            "application_admin_authority",
            "interactive_staff_session",
            "accountable_human_actor",
            "substantive_reason",
            "district_configuration_change",
            "append_only_audit",
        ],
    ),
    _capability(
        "identity_mappings.list",
        "Browse Google Workspace group to application-role mappings.",
        "GET",
        "/api/staff/google-role-mappings/",
        ["/configuration/identity"],
    ),
    _capability(
        "identity_mappings.create",
        "Map an existing Google Workspace group to an application role.",
        "POST",
        "/api/staff/google-role-mappings/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
    ),
    _capability(
        "identity_mappings.update",
        "Update or deactivate a Google Workspace role mapping.",
        "PATCH",
        "/api/staff/google-role-mappings/{id}/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
    ),
    _capability(
        "identity_mappings.delete",
        "Delete a Google Workspace role mapping.",
        "DELETE",
        "/api/staff/google-role-mappings/{id}/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
        constraints=["destructive"],
    ),
    _capability(
        "direct_role_grants.list",
        "Browse named individuals granted an application role without a "
        "Google group.",
        "GET",
        "/api/staff/direct-role-grants/",
        ["/configuration/identity"],
    ),
    _capability(
        "direct_role_grants.create",
        "Grant an application role directly to one named Workspace address, "
        "with a written reason.",
        "POST",
        "/api/staff/direct-role-grants/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
    ),
    _capability(
        "direct_role_grants.update",
        "Update or deactivate a direct application-role grant.",
        "PATCH",
        "/api/staff/direct-role-grants/{id}/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
    ),
    _capability(
        "direct_role_grants.delete",
        "Delete a direct application-role grant.",
        "DELETE",
        "/api/staff/direct-role-grants/{id}/",
        ["/configuration/identity"],
        execution="human_confirmation",
        risk="high",
        constraints=["destructive"],
    ),
    _capability(
        "integration_identities.list",
        "Review the four integration scopes, named accounts, token creation "
        "timestamps, and exact-scope validity without retrieving a credential.",
        "GET",
        "/api/staff/integration-identities/",
        ["/configuration/identity"],
        execution="human_only",
        risk="moderate",
        constraints=["interactive_staff_session"],
    ),
    _capability(
        "integration_identities.provision",
        "Provision one named, exact-scope integration identity and return its "
        "credential exactly once.",
        "POST",
        "/api/staff/integration-identities/{id}/provision/",
        ["/configuration/identity"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "secret_write_only",
            "secret_returned_once",
            "append_only_audit",
        ],
    ),
    _capability(
        "integration_identities.rotate",
        "Immediately revoke one integration credential and return its "
        "exact-scope replacement exactly once.",
        "POST",
        "/api/staff/integration-identities/{id}/rotate/",
        ["/configuration/identity"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "secret_write_only",
            "secret_returned_once",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.list",
        "Browse compact, cursor-paginated correspondence summaries; request "
        "scoping exposes only bounded subject and snippet content.",
        "GET",
        "/api/staff/correspondence/",
        [
            "/inbox",
            "/requests/:number/messages",
            "/configuration/correspondence",
        ],
    ),
    _capability(
        "correspondence.retrieve",
        "Read the complete authorized content and evidence for one message.",
        "GET",
        "/api/staff/correspondence/{id}/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.search",
        "Search immutable received and approved message snapshots within one "
        "authorized request using a bounded POST body.",
        "POST",
        "/api/staff/correspondence/search/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.threads",
        "Browse display-only provider thread summaries within one authorized "
        "request without merging legal correspondence records.",
        "GET",
        "/api/staff/correspondence/threads/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.thread_messages",
        "Browse compact correspondence summaries for one display-only thread.",
        "GET",
        "/api/staff/correspondence/thread-messages/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.operations",
        "Read PII-free delivery, inbound, quarantine, delivery-exception, "
        "age, and failure aggregates.",
        "GET",
        "/api/staff/correspondence-operations/",
        ["/configuration/correspondence"],
    ),
    _capability(
        "correspondence.readiness",
        "Read fixed-code readiness for outbound correspondence dependencies.",
        "GET",
        "/api/staff/correspondence-operations/readiness/",
        ["/configuration/correspondence"],
    ),
    _capability(
        "correspondence.draft_general",
        "Create normal case-related correspondence as an editable draft.",
        "POST",
        "/api/staff/correspondence/drafts/general/",
        ["/requests/:number/messages"],
        risk="moderate",
    ),
    _capability(
        "correspondence.draft_submission_receipt",
        "Create the non-statutory courtesy submission receipt as a staff "
        "draft; it cannot satisfy or defer the five-business-day response.",
        "POST",
        "/api/staff/correspondence/drafts/submission-receipt/",
        ["/requests/:number/messages"],
        risk="moderate",
    ),
    _capability(
        "correspondence.template_prefill",
        "Render the district's editable starting point for a staff-authored "
        "correspondence kind.",
        "GET",
        "/api/staff/correspondence/template-prefill/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.draft_acknowledgment",
        "Create an acknowledgment draft using the versioned statutory template.",
        "POST",
        "/api/staff/correspondence/drafts/acknowledgment/",
        ["/requests/:number/messages"],
        risk="moderate",
        mcp_tool="psd_prr_acknowledgment_draft",
    ),
    _capability(
        "correspondence.draft_clarification",
        "Create a clarification-request draft without changing request status.",
        "POST",
        "/api/staff/correspondence/drafts/clarification/",
        ["/requests/:number/messages"],
        risk="moderate",
        mcp_tool="psd_prr_clarification_letter_draft",
    ),
    _capability(
        "correspondence.draft_estimate_revision",
        "Prepare an estimate-revision draft for accountable human review.",
        "POST",
        "/api/staff/correspondence/drafts/estimate-revision/",
        ["/requests/:number/messages"],
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
        ],
    ),
    _capability(
        "correspondence.draft_scope_clarification",
        "Draft the scope conversation: the count, what was searched, the narrowing options the officer chose, and whether this is an RCW 42.56.520(3) clarification request or informational only.",
        "POST",
        "/api/staff/correspondence/drafts/scope-clarification/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "interactive_staff_session",
            # The clarification form tolls the District's response period the
            # moment it is delivered, and a letter reporting a large count is
            # read as pressure to give up. Neither is a judgment an agent or
            # a background task may make on the District's behalf.
            "human_legal_judgment",
            "counsel_review",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_installment",
        "Create a Records-Officer-only installment notice from a released, "
        "verified package and its application-scoped requester download.",
        "POST",
        "/api/staff/correspondence/drafts/installment/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="high",
    ),
    _capability(
        "correspondence.draft_responsive_records",
        "Create a staff-authored secure responsive-records notice draft.",
        "POST",
        "/api/staff/correspondence/drafts/responsive-records/",
        ["/requests/:number/messages"],
        risk="moderate",
    ),
    _capability(
        "correspondence.draft_closure",
        "Create a closure draft for Records Officer review.",
        "POST",
        "/api/staff/correspondence/drafts/closure/",
        ["/requests/:number/messages"],
        risk="high",
    ),
    _capability(
        "correspondence.draft_no_records_closure",
        "Attest to a reasonable search and create a versioned no-records or out-of-retention closure draft.",
        "POST",
        "/api/staff/correspondence/drafts/no-records-closure/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "adequate_search_evidence",
            "counsel_review",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_fee_estimate",
        "Create the RCW 42.56.120 copying-charge estimate, carrying how the "
        "charge was calculated and the deposit the District asks for. It "
        "holds no work by itself.",
        "POST",
        "/api/staff/correspondence/drafts/fee-estimate/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "adopted_fee_schedule",
            "statutory_fee_caps",
            "counsel_review",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_completion_closure",
        "Create the completion-closure draft that fulfills the request once "
        "its delivery is confirmed; drafting it closes nothing.",
        "POST",
        "/api/staff/correspondence/drafts/completion-closure/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "counsel_review",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_nonpayment_closure",
        "Create the RCW 42.56.120(2) closure draft for an unpaid fee or "
        "deposit, carrying the amount, the date it was requested, and the "
        "fee_unpaid reason code.",
        "POST",
        "/api/staff/correspondence/drafts/nonpayment-closure/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "counsel_review",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_third_party_notice",
        "Notify a named person, and optionally their attorney, under "
        "RCW 42.56.540 that records concerning them will be disclosed; the "
        "notice is audited on the case and never sent to the requester.",
        "POST",
        "/api/staff/correspondence/drafts/third-party-notice/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "counsel_review",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.batch_draft_closures",
        "Draft the same completion closure or general notice for up to 50 "
        "selected cases; each case gets its own editable draft and nothing "
        "is approved or sent.",
        "POST",
        "/api/staff/correspondence/batch/closure-drafts/",
        ["/inbox"],
        execution="human_only",
        risk="moderate",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "explicit_confirmation",
            "batch_case_cap",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.batch_approve_and_send",
        "Approve and release up to 50 completion-closure, non-payment-closure "
        "or general drafts, one independent delivery and one audit row per "
        "case; denials and no-records closures are refused.",
        "POST",
        "/api/staff/correspondence/batch/approve-and-send/",
        ["/inbox"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "explicit_confirmation",
            "batch_case_cap",
            "external_side_effects",
            "correspondence_delivery_state",
            "append_only_audit",
        ],
    ),
    _capability(
        "correspondence.draft_denial",
        "Create a Records-Officer-only full-exemption denial draft with one "
        "or more active exemption_citation_ids; this does not deny or "
        "transition the request.",
        "POST",
        "/api/staff/correspondence/drafts/denial/",
        ["/requests/:number/messages"],
        risk="high",
    ),
    _capability(
        "correspondence.edit",
        "Edit a draft with optimistic concurrency: authored kinds accept "
        "subject/body, denial additionally requires exemption_citation_ids, "
        "while structured kinds accept only their kind-specific inputs and "
        "are re-rendered; unknown fields are rejected.",
        "PATCH",
        "/api/staff/correspondence/{id}/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="moderate",
    ),
    _capability(
        "correspondence.delete_draft",
        "Delete a never-approved draft with optimistic concurrency; "
        "approved correspondence and drafts linked to other case evidence "
        "are refused.",
        "POST",
        "/api/staff/correspondence/{id}/delete/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="moderate",
    ),
    _capability(
        "correspondence.preview",
        "Preview the exact recipient-visible content produced by delivery rendering.",
        "GET",
        "/api/staff/correspondence/{id}/preview/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "correspondence.approve",
        "Approve and freeze the immutable correspondence content and recipient snapshot.",
        "POST",
        "/api/staff/correspondence/{id}/approve/",
        # No UI route: the staff workspace approves and sends in one action
        # (correspondence.approve_and_send). Approving without releasing stays
        # an addressable primitive for agents and recovery, but it is no longer
        # a button, so claiming a UI route here would make the parity test
        # assert a screen that does not exist.
        [],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "correspondence.queue",
        "Queue approved correspondence for durable external delivery.",
        "POST",
        "/api/staff/correspondence/{id}/queue/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "external_side_effects",
        ],
    ),
    _capability(
        "correspondence.approve_and_send",
        "Approve a draft and hand it to delivery in one accountable action.",
        "POST",
        "/api/staff/correspondence/{id}/approve-and-send/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
            "external_side_effects",
        ],
    ),
    _capability(
        "correspondence.cancel",
        "Cancel an eligible unsent correspondence delivery.",
        "POST",
        "/api/staff/correspondence/{id}/cancel/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="high",
        constraints=["external_side_effects"],
    ),
    _capability(
        "correspondence.retry",
        "Retry an eligible failed delivery without changing its deterministic Message-ID.",
        "POST",
        "/api/staff/correspondence/{id}/retry/",
        ["/requests/:number/messages"],
        execution="human_confirmation",
        risk="high",
        constraints=["external_side_effects"],
    ),
    _capability(
        "correspondence.attempts",
        "Read sanitized delivery-attempt history for one correspondence.",
        "GET",
        "/api/staff/correspondence/{id}/attempts/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "inbound_correspondence.list",
        "Browse an explicit unmatched or quarantined inbound-message queue "
        "without requester PII in list rows.",
        "GET",
        "/api/staff/inbound-correspondence-receipts/",
        ["/inbox"],
        risk="moderate",
    ),
    _capability(
        "inbound_correspondence.retrieve",
        "Review one retained inbound message and sanitized attachment "
        "availability or normalized delivery-status outcomes as a Records "
        "Officer.",
        "GET",
        "/api/staff/inbound-correspondence-receipts/{id}/",
        ["/inbox"],
        risk="moderate",
    ),
    _capability(
        "inbound_correspondence.match",
        "Associate parsed inbound correspondence to one request after an "
        "accountable human reviews the retained evidence; delivery-status "
        "notifications are excluded.",
        "POST",
        "/api/staff/inbound-correspondence-receipts/{id}/match/",
        ["/inbox"],
        execution="human_only",
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "inbound_correspondence.rematch",
        "Correct an inbound correspondence request association without "
        "rewriting the retained message evidence; delivery-status "
        "notifications are excluded.",
        "POST",
        "/api/staff/inbound-correspondence-receipts/{id}/rematch/",
        ["/inbox"],
        execution="human_only",
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "inbound_correspondence.redirect_email_intake",
        "Atomically add an automatically opened email case's retained message "
        "to an existing request and close the leftover case as not a records "
        "request; nothing is deleted and no requester message is sent.",
        "POST",
        "/api/staff/inbound-correspondence-receipts/{id}/redirect/",
        ["/inbox", "/requests/:number/overview"],
        execution="human_only",
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
            "interactive_staff_session",
            "records_officer_authority",
            "email_intake_only",
            "append_only_audit",
        ],
    ),
    _capability(
        "inbound_correspondence.associate_clarification_response",
        "Explicitly associate an already-matched inbound message with one "
        "open clarification cycle without resolving it.",
        "POST",
        (
            "/api/staff/inbound-correspondence-receipts/{id}/"
            "associate-clarification/"
        ),
        ["/inbox"],
        execution="human_only",
        risk="high",
        constraints=[
            "substantive_reason",
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "correspondence_attachments.download",
        "Obtain a short-lived, user-bound download for a clean correspondence attachment.",
        "POST",
        "/api/staff/correspondence-attachments/{id}/download/",
        ["/inbox", "/requests/:number/messages"],
        risk="moderate",
        constraints=["malware_scanned"],
    ),
    _capability(
        "clarifications.list",
        "Browse clarification cycles requiring staff review.",
        "GET",
        "/api/staff/clarification-cycles/",
        ["/inbox", "/requests/:number/messages"],
    ),
    _capability(
        "clarifications.retrieve",
        "Read the legal and correspondence state of one clarification cycle.",
        "GET",
        "/api/staff/clarification-cycles/{id}/",
        ["/requests/:number/messages"],
    ),
    _capability(
        "clarifications.resolve",
        "Resolve a received clarification response as the accountable Records Officer.",
        "POST",
        "/api/staff/clarification-cycles/{id}/resolve/",
        ["/requests/:number/messages"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "interactive_staff_session",
        ],
    ),
    _capability(
        "work_items.list",
        "Browse and search role-scoped current and disposed next-action metadata "
        "without copying requester or message content.",
        "GET",
        "/api/staff/work-items/",
        ["/inbox", "/review"],
    ),
    _capability(
        "work_items.retrieve",
        "Retrieve one role-scoped work item with its complete append-only work "
        "history and the caller's personal read state.",
        "GET",
        "/api/staff/work-items/{id}/",
        ["/work/:id"],
    ),
    _capability(
        "work_items.counts",
        "Count role-scoped unresolved and disposed work without treating team "
        "handling as legal or transport state.",
        "GET",
        "/api/staff/work-items/counts/",
        ["/inbox", "/review"],
    ),
    _capability(
        "work_items.assignment_targets",
        "List only active, PII-minimal staff choices whose current roles are "
        "valid targets for one role-scoped work item.",
        "GET",
        "/api/staff/work-items/{id}/assignment-targets/",
        ["/work/:id"],
    ),
    _capability(
        "work_items.read",
        "Mark one work item read for the attributed caller only.",
        "POST",
        "/api/staff/work-items/{id}/read/",
        ["/requests/:number/messages", "/work/:id"],
        risk="low",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "work_items.unread",
        "Mark one work item unread for the attributed caller only.",
        "POST",
        "/api/staff/work-items/{id}/unread/",
        ["/requests/:number/messages", "/work/:id"],
        risk="low",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "work_items.assignment",
        "Assign, reassign, or unassign one next action to an active staff member "
        "authorized for its work kind.",
        "POST",
        "/api/staff/work-items/{id}/assignment/",
        ["/work/:id"],
        risk="moderate",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "work_items.disposition",
        "Set one work item's shared team handling to Waiting or Handled without "
        "changing its source evidence.",
        "POST",
        "/api/staff/work-items/{id}/disposition/",
        ["/inbox", "/requests/:number/messages", "/work/:id"],
        risk="moderate",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "work_items.reopen",
        "Reopen one human-disposed work item as Needs attention without reopening "
        "or transitioning its legal request; source exclusions remain derived.",
        "POST",
        "/api/staff/work-items/{id}/reopen/",
        ["/requests/:number/messages", "/work/:id"],
        risk="moderate",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "work_items.no_action_required",
        "Record an allowlisted, reasoned team decision that no next action is "
        "required without changing legal or transport evidence.",
        "POST",
        "/api/staff/work-items/{id}/no-action-required/",
        ["/inbox", "/requests/:number/messages", "/work/:id"],
        execution="human_only",
        risk="high",
        constraints=[
            "accountable_human_actor",
            "records_officer_authority",
            "interactive_staff_session",
            "substantive_reason",
            "append_only_audit",
        ],
    ),
    _capability(
        "work_items.bulk_metadata",
        "Apply at most 50 role-checked, versioned read, assignment, waiting, "
        "handled, or reopen metadata mutations atomically.",
        "POST",
        "/api/staff/work-items/bulk-metadata/",
        ["/requests/:number/messages"],
        risk="moderate",
        constraints=["accountable_human_actor", "append_only_audit"],
    ),
    _capability(
        "audit.list",
        "Search the append-only compliance audit log.",
        "GET",
        "/api/staff/audit-log/",
        ["/oversight/audit"],
        mcp_tool="psd_prr_audit_log_search",
    ),
    _capability(
        "advisory_evaluations.list",
        "Read model-comparison evaluation runs: accuracy, safety recall, "
        "cost, and latency per candidate model on labelled past decisions.",
        "GET",
        "/api/staff/advisory-evaluations/",
        ["/oversight/ai-evaluation"],
        constraints=["records_officer_authority"],
    ),
)


def capability_catalog():
    """Return a fresh serializable catalog so callers cannot mutate policy."""
    return {
        "source": AGENT_NATIVE_GUIDE_URL,
        "principles": deepcopy(AGENT_NATIVE_PRINCIPLES),
        "policy": {
            "anonymous": "Any requester may execute this one write-only public intake primitive after its CSRF and abuse controls pass.",
            "direct": "Agent may execute through the staff REST API with its attributed token.",
            "human_confirmation": "Agent may prepare the action, but a human must explicitly approve it before execution.",
            "human_only": "Agent must hand off; the accountable human executes this action in the staff UI.",
        },
        "constraint_definitions": deepcopy(AGENT_CONSTRAINT_DEFINITIONS),
        "mcp_tool_policy": MCP_TOOL_FIELD_POLICY,
        "capabilities": deepcopy(list(CAPABILITIES)),
    }


class AgentCapabilitySerializer(serializers.Serializer):
    id = serializers.CharField()
    outcome = serializers.CharField()
    method = serializers.CharField()
    path = serializers.CharField()
    ui_routes = serializers.ListField(child=serializers.CharField())
    agent_transport = serializers.CharField()
    execution = serializers.ChoiceField(
        choices=["anonymous", "direct", "human_confirmation", "human_only"],
    )
    risk = serializers.ChoiceField(choices=["read", "low", "moderate", "high"])
    mcp_tool = serializers.CharField(allow_null=True)
    constraints = serializers.ListField(child=serializers.CharField())


class AgentCapabilityCatalogSerializer(serializers.Serializer):
    source = serializers.URLField()
    principles = serializers.DictField(child=serializers.CharField())
    policy = serializers.DictField(child=serializers.CharField())
    constraint_definitions = serializers.DictField(child=serializers.CharField())
    mcp_tool_policy = serializers.CharField()
    capabilities = AgentCapabilitySerializer(many=True)


class AgentCapabilityCatalogView(generics.GenericAPIView):
    """Staff-only discovery endpoint for GUI/agent parity and policy."""

    serializer_class = AgentCapabilityCatalogSerializer

    def get(self, request):
        serializer = self.get_serializer(capability_catalog())
        return Response(serializer.data)
