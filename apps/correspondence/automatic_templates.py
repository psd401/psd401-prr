"""Safe token rendering for governed, district-editable message templates.

Every requester-facing outbound correspondence kind has an editable template
here. Two families exist and they are deliberately different:

* **Structured kinds** (``MANAGED_TEMPLATE_KINDS``) render the requester's
  message from a template plus validated structured inputs. Their built-in,
  source-controlled version stays the fallback; an activated managed version
  replaces it for future drafts and is reproducible from its exact immutable
  row. Because the safe renderer is token-only, structural parts of the
  built-in Django templates (search-step lists, conditional paragraphs) are
  supplied as pre-rendered tokens rather than template logic.
* **Free-form kinds** (``FREE_FORM_TEMPLATE_KINDS``) stay human-authored: the
  officer writes the subject and body. Their template is a drafting starting
  point only, rendered through :func:`render_prefill`, and never becomes the
  legal snapshot on its own.

Editability never relaxes a legal guarantee. ``required_tokens`` keeps
mandatory content (the request number, a revised date, a download link, the
search evidence) from being edited away, ``required_phrases`` keeps mandatory
statutory language present, and no template may set request state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils import timezone
from django.utils.formats import date_format

from apps.configuration import runtime

from .guardrails import GuardrailWarning
from .models import AutomaticMessageTemplate, Correspondence
from .rendering import RenderedMessage, canonical_body, validate_subject


@dataclass(frozen=True)
class TemplateSpec:
    label: str
    identifier: str
    #: Source-controlled built-in version, or "" for free-form drafting aids.
    builtin_version: str
    subject_template: str
    body_template: str
    allowed_tokens: frozenset[str]
    #: Tokens that must remain present in the template text.
    required_tokens: frozenset[str]
    #: Tokens whose rendered value must be non-empty. Conditional paragraphs
    #: stay out of this set: they are required in the source but legitimately
    #: render empty (no previous estimate, no exemptions, in-retention).
    required_values: frozenset[str] = field(default=frozenset())
    #: ``(lowercase needle, operator message)`` pairs that must survive edits.
    required_phrases: tuple[tuple[str, str], ...] = field(default=())
    #: Grouping label for the configuration UI.
    group: str = "Statutory correspondence"


COMMON_TOKENS = frozenset(
    {
        "request_number",
        "requester_name",
        "received_date",
        "request_description",
        "pra_officer_email",
    }
)

_SIGNATURE = """Sincerely,
Public Records Officer
Peninsula School District No. 401
"""

_TOKEN = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_DJANGO_SYNTAX = re.compile(r"{%.*?%}|{#.*?#}|{{\s*[a-z_]+\.[^}]*}}|\|[a-z]+")

# The legal text of a structured template has exactly one home:
# templates/correspondence/<identifier>/v<version>.txt. The built-in default
# offered here is DERIVED from that file rather than re-typed, so the two can
# never drift. This module only renames each Django expression to the safe
# token that stands in for it, which makes _TOKEN_RENAMES the per-kind record
# of exactly which template logic each pre-rendered token replaces.

_COMMON_RENAMES = (
    ("{{ request.number }}", "{{ request_number }}"),
    (
        '{{ request.requester.name|default:"Requester" }}',
        "{{ requester_name }}",
    ),
    ('{{ request.received_at|date:"F j, Y" }}', "{{ received_date }}"),
    ("{{ request.description }}", "{{ request_description }}"),
)

_TOKEN_RENAMES = {
    "acknowledgment": (*_COMMON_RENAMES, ('{{ estimated_completion|date:"F j, Y" }}', "{{ estimated_completion }}")),
    "submission_receipt": _COMMON_RENAMES,
    "clarification_request": (*_COMMON_RENAMES, ('{{ clarification_due|date:"F j, Y" }}', "{{ clarification_due }}")),
    "estimate_revision": (
        *_COMMON_RENAMES,
        # The conditional previous-estimate line, including its newline.
        (
            (
                "{% if previous_date %}Previous estimated completion: "
                '{{ previous_date|date:"F j, Y" }}\n{% endif %}'
            ),
            "{{ previous_estimate_line }}",
        ),
        ('{{ new_date|date:"F j, Y" }}', "{{ new_date }}"),
        ("{{ reason }}", "{{ revision_reason }}"),
    ),
    "installment_notice": (
        *_COMMON_RENAMES,
        ("{{ installment.sequence }}", "{{ installment_sequence }}"),
        (
            "{{ item_count }} record{{ item_count|pluralize }}",
            "{{ item_count_phrase }}",
        ),
        (
            '{{ download_expires_at|date:"F j, Y, g:i a T" }}',
            "{{ download_expires_at }}",
        ),
        # The conditional exemption-log paragraph.
        (
            (
                "{% if has_exemptions %}\nThe package includes an exemption and "
                "withholding log identifying each\nredaction or record withheld "
                "in full and the legal basis applied under\n"
                "RCW 42.56.210(3).\n{% endif %}"
            ),
            "{{ exemption_log_paragraph }}",
        ),
        # The final-vs-more-installments paragraph.
        (
            (
                "{% if is_final %}\nDistrict staff have marked this as the final "
                "installment. Delivery of this\nnotice does not by itself close "
                "the request; closure remains a separate\nRecords Officer "
                "action.\n{% else %}\nAdditional installments are currently "
                "expected.\n{% endif %}"
            ),
            "{{ final_installment_paragraph }}",
        ),
    ),
    # Every other scope-conversation token is supplied to both paths already
    # rendered by the helpers below, so only the reply-by date needs renaming.
    "scope_clarification": (*_COMMON_RENAMES, ('{{ response_by|date:"F j, Y" }}', "{{ response_by }}")),
    "no_records_closure": (
        *_COMMON_RENAMES,
        (
            '{{ search_completed_at|date:"F j, Y" }}',
            "{{ search_completed_date }}",
        ),
        # The adequate-search step list.
        (
            (
                "{% for step in search_steps %}\n- {{ step.location }} "
                "(custodian: {{ step.custodian }}): {{ step.method }};\n  date "
                "range {{ step.date_range }}; result: {{ step.result }}."
                "\n{% endfor %}"
            ),
            "{{ search_steps }}",
        ),
        # The out-of-retention paragraph.
        (
            (
                '{% if determination == "outside_retention" %}\n\nThe requested '
                "records were outside the verified retention period. Retention"
                "\nschedule reference: {{ retention_schedule_reference }}. Staff "
                "also checked\nincident holds, prior exports, email/Drive "
                "copies, and other likely copies.\n{% endif %}"
            ),
            "{{ retention_paragraph }}",
        ),
        # The WAC 44-14-04003(6) not-maintained paragraph.
        (
            (
                '{% if determination == "not_maintained" %}\n\nThe District does '
                "not maintain a record answering this request. Under\n"
                "WAC 44-14-04003(6), the Public Records Act requires an agency to "
                "provide\nexisting identifiable records; the District is not "
                "required to create a record\nto answer a question or to compile "
                "information it does not already keep in\nrecord form.\n"
                "{% endif %}"
            ),
            "{{ not_maintained_paragraph }}",
        ),
    ),
    "fee_estimate": (
        *_COMMON_RENAMES,
        # The optional RCW 42.56.120(2) deposit paragraph.
        (
            (
                "{% if deposit_amount %}\nBefore the District does that copying "
                "work it asks for a deposit of\n{{ deposit_amount }}. "
                "RCW 42.56.120(2) allows a deposit of up to ten\npercent of the "
                "estimated charge. The deposit is applied to the charge; it is\n"
                "not an additional cost. The District will hold work on this "
                "request until the\ndeposit is received.\n{% endif %}"
            ),
            "{{ deposit_paragraph }}",
        ),
    ),
    "completion_closure": _COMMON_RENAMES,
    "nonpayment_closure": (*_COMMON_RENAMES, ('{{ payment_requested_on|date:"F j, Y" }}', "{{ payment_requested_on_date }}")),
    "third_party_notice": (*_COMMON_RENAMES, ('{{ response_by|date:"F j, Y" }}', "{{ response_by_date }}")),
}


def _builtin_default(
    identifier: str,
    version: str,
    allowed_tokens: frozenset[str],
) -> tuple[str, str]:
    """Read one source-controlled template as safe token-only source."""
    from .rendering import TEMPLATES

    path = (
        Path(settings.BASE_DIR)
        / "templates"
        / TEMPLATES[(identifier, version)]
    )
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    for needle, replacement in _TOKEN_RENAMES[identifier]:
        raw = raw.replace(needle, replacement)

    subject = ""
    body_lines: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{#") or stripped in {
            "{% autoescape off %}",
            "{% endautoescape %}",
        }:
            continue
        if not subject and stripped.startswith("Subject:"):
            subject = line.split("Subject:", 1)[1].strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()

    combined = f"{subject}\n{body}"
    leftover = _DJANGO_SYNTAX.search(combined)
    if leftover is not None:
        raise ImproperlyConfigured(
            f"{identifier} v{version} carries template logic with no safe "
            f"token: {leftover.group(0)!r}. Add a rename to _TOKEN_RENAMES "
            "and a token to the kind's allowed_tokens."
        )
    # A bare, un-renamed `{{ field }}` has no dot and no filter, so the syntax
    # check above cannot see it. Without this, a new field added to a source
    # template would be accepted at import and then raise KeyError inside
    # _TOKEN.sub at send time. Fail closed at import instead.
    unknown = sorted(set(_TOKEN.findall(combined)).difference(allowed_tokens))
    if unknown:
        raise ImproperlyConfigured(
            f"{identifier} v{version} references template field(s) with no "
            f"safe token: {', '.join(unknown)}. Add a rename to "
            "_TOKEN_RENAMES, a token to the kind's allowed_tokens, and a "
            "value to _values()."
        )
    return subject, body


def _structured_spec(
    *,
    label: str,
    kind: str,
    builtin_version: str,
    allowed_tokens: frozenset[str],
    required_tokens: frozenset[str],
    required_values: frozenset[str] = frozenset(),
    required_phrases: tuple[tuple[str, str], ...] = (),
    group: str = "Statutory correspondence",
) -> TemplateSpec:
    subject, body = _builtin_default(kind, builtin_version, allowed_tokens)
    return TemplateSpec(
        label=label,
        identifier=kind,
        builtin_version=builtin_version,
        subject_template=subject,
        body_template=body,
        allowed_tokens=allowed_tokens,
        required_tokens=required_tokens,
        required_values=required_values,
        required_phrases=required_phrases,
        group=group,
    )


def _starting_point_spec(
    *,
    label: str,
    kind: str,
    subject_template: str,
    body_template: str,
) -> TemplateSpec:
    """A drafting aid for a kind with no source-controlled legal template."""
    return TemplateSpec(
        label=label,
        identifier=kind,
        builtin_version="",
        subject_template=subject_template,
        body_template=body_template + _SIGNATURE,
        allowed_tokens=COMMON_TOKENS,
        required_tokens=frozenset({"request_number"}),
        group="Staff-authored starting points",
    )


TEMPLATE_SPECS = {
    Correspondence.Kind.ACKNOWLEDGMENT: _structured_spec(
        label="Five-day acknowledgment",
        kind=Correspondence.Kind.ACKNOWLEDGMENT,
        builtin_version="2",
        allowed_tokens=COMMON_TOKENS | {"estimated_completion"},
        # This letter *is* the RCW 42.56.520 five-day response, so it must
        # cite the statute and it must carry a real estimate. But it does not
        # have to express that estimate as a rendered calendar date.
        #
        # PR #113 removed `estimated_completion` from this set on purpose: the
        # district's counsel-approved wording states the estimate as a
        # duration ("60 business days") rather than a date, and that is a
        # lawful estimate of "the time the agency will require to respond."
        # PR #186 reinstated the token requirement without checking the
        # already-activated district template, which made every acknowledgment
        # draft fail at render time with an authoring error that named a
        # compose-dialog field. Requiring the token here is stricter than
        # RCW 42.56.520 and overrides a decision the district already made
        # with counsel; requiring the *value* below is not, and is what keeps
        # the case's tracked estimate real either way.
        required_tokens=frozenset({"request_number"}),
        required_values=frozenset({"estimated_completion"}),
        required_phrases=(
            (
                "rcw 42.56.520",
                (
                    "The five-day response must cite the statute that requires "
                    "the estimate."
                ),
            ),
        ),
    ),
    Correspondence.Kind.SUBMISSION_RECEIPT: _structured_spec(
        label="Submission receipt",
        kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS,
        required_tokens=frozenset(
            {
                "request_number",
                "requester_name",
                "request_description",
                "pra_officer_email",
            }
        ),
        required_values=frozenset(
            {"requester_name", "request_description", "pra_officer_email"}
        ),
        required_phrases=(
            (
                "rcw 42.56.520",
                (
                    "The receipt must distinguish itself from the "
                    "five-business-day RCW 42.56.520 response."
                ),
            ),
            (
                "five business day",
                (
                    "The receipt must distinguish itself from the "
                    "five-business-day RCW 42.56.520 response."
                ),
            ),
        ),
        group="Non-statutory courtesy",
    ),
    Correspondence.Kind.CLARIFICATION_REQUEST: _structured_spec(
        label="Clarification request",
        kind=Correspondence.Kind.CLARIFICATION_REQUEST,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS
        | {"clarification_questions", "clarification_due"},
        required_tokens=frozenset(
            {
                "request_number",
                "clarification_questions",
                "clarification_due",
            }
        ),
        required_values=frozenset(
            {"clarification_questions", "clarification_due"}
        ),
    ),
    Correspondence.Kind.ESTIMATE_REVISION: _structured_spec(
        label="Estimate revision",
        kind=Correspondence.Kind.ESTIMATE_REVISION,
        builtin_version="2",
        allowed_tokens=COMMON_TOKENS
        | {"previous_estimate_line", "new_date", "revision_reason"},
        required_tokens=frozenset(
            {"request_number", "new_date", "revision_reason"}
        ),
        required_values=frozenset({"new_date", "revision_reason"}),
    ),
    Correspondence.Kind.INSTALLMENT_NOTICE: _structured_spec(
        label="Installment notice",
        kind=Correspondence.Kind.INSTALLMENT_NOTICE,
        builtin_version="3",
        allowed_tokens=COMMON_TOKENS
        | {
            "installment_sequence",
            "item_count_phrase",
            "download_url",
            "download_expires_at",
            "exemption_log_paragraph",
            "final_installment_paragraph",
        },
        required_tokens=frozenset(
            {
                "request_number",
                "installment_sequence",
                "download_url",
                "download_expires_at",
                "exemption_log_paragraph",
                "final_installment_paragraph",
            }
        ),
        required_values=frozenset(
            {
                "installment_sequence",
                "download_url",
                "download_expires_at",
            }
        ),
    ),
    Correspondence.Kind.NO_RECORDS_CLOSURE: _structured_spec(
        label="No-records / out-of-retention closure",
        kind=Correspondence.Kind.NO_RECORDS_CLOSURE,
        builtin_version="2",
        allowed_tokens=COMMON_TOKENS
        | {
            "search_completed_date",
            "search_steps",
            "adequacy_reason",
            "retention_paragraph",
            "not_maintained_paragraph",
        },
        required_tokens=frozenset(
            {
                "request_number",
                "search_completed_date",
                "search_steps",
                "adequacy_reason",
                "retention_paragraph",
                "not_maintained_paragraph",
            }
        ),
        required_values=frozenset(
            {"search_completed_date", "search_steps", "adequacy_reason"}
        ),
        required_phrases=(
            (
                "not a determination that responsive records are exempt",
                (
                    "A no-records closure must state that it is not an exemption "
                    "determination."
                ),
            ),
        ),
    ),
    Correspondence.Kind.FEE_ESTIMATE: _structured_spec(
        label="Fee estimate and deposit request",
        kind=Correspondence.Kind.FEE_ESTIMATE,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS
        | {"estimated_amount", "estimate_basis", "deposit_paragraph"},
        required_tokens=frozenset(
            {"request_number", "estimated_amount", "estimate_basis"}
        ),
        required_values=frozenset({"estimated_amount", "estimate_basis"}),
        required_phrases=(
            (
                "rcw 42.56.120",
                (
                    "A fee estimate must cite the statute the District's "
                    "copying charge comes from."
                ),
            ),
            (
                "locating",
                (
                    "A fee estimate must keep the sentence saying the District "
                    "does not charge for the time spent locating records "
                    "(RCW 42.56.120(1))."
                ),
            ),
        ),
    ),
    Correspondence.Kind.COMPLETION_CLOSURE: _structured_spec(
        label="Completion closure",
        kind=Correspondence.Kind.COMPLETION_CLOSURE,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS | {"production_summary"},
        required_tokens=frozenset({"request_number", "production_summary"}),
        required_values=frozenset({"production_summary"}),
        required_phrases=(
            (
                "does not affect",
                (
                    "A completion closure must not read as waiving the "
                    "requester's right to seek review of a withholding."
                ),
            ),
        ),
    ),
    Correspondence.Kind.NONPAYMENT_CLOSURE: _structured_spec(
        label="Non-payment closure",
        kind=Correspondence.Kind.NONPAYMENT_CLOSURE,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS
        | {"amount_due", "payment_requested_on_date"},
        required_tokens=frozenset(
            {"request_number", "amount_due", "payment_requested_on_date"}
        ),
        required_values=frozenset(
            {"amount_due", "payment_requested_on_date"}
        ),
        required_phrases=(
            (
                "rcw 42.56.120",
                (
                    "A non-payment closure must cite the statute that permits "
                    "the District to stop fulfilling an unpaid request."
                ),
            ),
        ),
    ),
    Correspondence.Kind.THIRD_PARTY_NOTICE: _structured_spec(
        label="Third-party notice",
        kind=Correspondence.Kind.THIRD_PARTY_NOTICE,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS
        | {"employee_name", "disclosed_requesters", "response_by_date"},
        required_tokens=frozenset(
            {
                "request_number",
                "employee_name",
                "disclosed_requesters",
                "response_by_date",
            }
        ),
        required_values=frozenset(
            {"employee_name", "disclosed_requesters", "response_by_date"}
        ),
        required_phrases=(
            (
                "rcw 42.56.540",
                (
                    "A third-party notice must cite the statute that gives the "
                    "notified person their right to seek an injunction."
                ),
            ),
        ),
        group="Third-party notice",
    ),
    Correspondence.Kind.SCOPE_CLARIFICATION: _structured_spec(
        label="Scope conversation (count and narrowing options)",
        kind=Correspondence.Kind.SCOPE_CLARIFICATION,
        builtin_version="1",
        allowed_tokens=COMMON_TOKENS
        | {
            "match_count_phrase",
            "query_summary",
            "narrowing_options",
            "response_by",
            "fee_paragraph",
            "scope_response_paragraph",
        },
        required_tokens=frozenset(
            {
                "request_number",
                "match_count_phrase",
                "query_summary",
                "response_by",
                "scope_response_paragraph",
            }
        ),
        # The narrowing menu and the fee line legitimately render empty: an
        # officer may send a count with no options, and no fee may be stated
        # at all until the district adopts a schedule with counsel.
        required_values=frozenset(
            {
                "match_count_phrase",
                "query_summary",
                "response_by",
                "scope_response_paragraph",
            }
        ),
    ),
    Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE: _starting_point_spec(
        label="Responsive-records notice (starting point)",
        kind=Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE,
        subject_template=(
            "Public Records Request {{ request_number }} — Responsive Records"
        ),
        body_template="""Dear {{ requester_name }},

Peninsula School District has located records responsive to public records
request {{ request_number }}, received {{ received_date }}.

[Describe what is being made available, how it will be provided, and any
remaining installments.]

If you have questions, contact the Public Records Officer at
{{ pra_officer_email }}.

""",
    ),
    Correspondence.Kind.CLOSURE: _starting_point_spec(
        label="Closure (starting point)",
        kind=Correspondence.Kind.CLOSURE,
        subject_template=(
            "Public Records Request {{ request_number }} — Request Closed"
        ),
        body_template="""Dear {{ requester_name }},

Peninsula School District is closing public records request
{{ request_number }}, received {{ received_date }}.

[State the basis for closure and what the District provided.]

If you have questions, contact the Public Records Officer at
{{ pra_officer_email }}.

""",
    ),
    Correspondence.Kind.DENIAL: _starting_point_spec(
        label="Denial (starting point)",
        kind=Correspondence.Kind.DENIAL,
        subject_template=(
            "Public Records Request {{ request_number }} — Denial"
        ),
        body_template="""Dear {{ requester_name }},

Peninsula School District is withholding the records responsive to public
records request {{ request_number }}, received {{ received_date }}.

You requested:
"{{ request_description }}"

[State each exemption claimed, a brief explanation of how it applies to the
withheld records, and the requester's right to seek review. Every exemption
must also be attached to this draft as an exemption citation.]

If you have questions, contact the Public Records Officer at
{{ pra_officer_email }}.

""",
    ),
    Correspondence.Kind.GENERAL: _starting_point_spec(
        label="General correspondence (starting point)",
        kind=Correspondence.Kind.GENERAL,
        subject_template="Public Records Request {{ request_number }}",
        body_template="""Dear {{ requester_name }},

[Write the message about public records request {{ request_number }}.]

If you have questions, contact the Public Records Officer at
{{ pra_officer_email }}.

""",
    ),
}

#: Kinds whose legal snapshot is rendered from a template plus structured
#: inputs, and whose activated managed version therefore replaces the built-in.
MANAGED_TEMPLATE_KINDS = frozenset(
    kind for kind, spec in TEMPLATE_SPECS.items() if spec.builtin_version
)

#: Kinds that stay human-authored; their template is a drafting aid only.
FREE_FORM_TEMPLATE_KINDS = frozenset(
    kind for kind, spec in TEMPLATE_SPECS.items() if not spec.builtin_version
)



def template_spec(kind: str) -> TemplateSpec:
    try:
        return TEMPLATE_SPECS[kind]
    except KeyError as exc:
        raise ValidationError(
            "Only requester-facing outbound correspondence kinds have "
            "editable message templates."
        ) from exc


#: What the requester loses when a token is absent, said as content rather
#: than as a variable name. A guardrail an officer cannot act on is a puzzle,
#: and "Required template token(s) missing: download_url" is a puzzle whose
#: answer lives in the source code.
TOKEN_CONSEQUENCES: dict[str, str] = {
    "request_number": (
        "The letter will not state the tracking number, so a requester "
        "replying about it cannot be matched to their case automatically."
    ),
    "requester_name": "The letter will not address the requester by name.",
    "received_date": "The letter will not say when the request was received.",
    "request_description": (
        "The letter will not repeat what was asked for, so the requester "
        "cannot confirm the District understood their request."
    ),
    "pra_officer_email": (
        "The letter will not tell the requester who to contact with "
        "questions."
    ),
    "estimated_completion": (
        "The letter will not state a completion date. RCW 42.56.520 still "
        "requires an estimate of the time needed, so the wording must give "
        "one some other way."
    ),
    "clarification_questions": (
        "The letter will ask for clarification without saying what is "
        "unclear, and a clarification request tolls the statutory clock."
    ),
    "clarification_due": (
        "The letter will not say by when the requester must reply, so a "
        "later closure for non-response is hard to defend."
    ),
    "new_date": "The revised estimate letter will not state the new date.",
    "revision_reason": (
        "The letter will not say why the estimate moved. An unexplained "
        "revision is a Yousoufian penalty aggravator."
    ),
    "estimated_amount": (
        "The letter will ask for money without saying how much. A charge "
        "the requester cannot see is not a charge they can dispute."
    ),
    "estimate_basis": (
        "The letter will not show how the charge was calculated, which is "
        "what lets a requester check it against the adopted fee schedule."
    ),
    "deposit_paragraph": (
        "A deposit the District is holding work on will not be stated, so "
        "the requester will not know what unblocks their request."
    ),
    "download_url": (
        "The requester will not receive a link to their records. The letter "
        "will announce records they have no way to reach."
    ),
    "download_expires_at": (
        "The requester will not be told when their download link stops "
        "working."
    ),
    "installment_sequence": (
        "The letter will not say which installment this is, so a requester "
        "receiving several cannot tell them apart or notice a gap."
    ),
    "exemption_log_paragraph": (
        "Withheld or redacted records will be released without the "
        "RCW 42.56.210(3) exemption log that explains them."
    ),
    "final_installment_paragraph": (
        "The requester will not be told whether this is the last "
        "installment, so they will not know the response is complete."
    ),
    "search_steps": (
        "A no-records closure will not show where the District looked. The "
        "adequacy of the search is the whole defence of that closure."
    ),
    "search_completed_date": (
        "The closure will not say when the search was completed."
    ),
    "adequacy_reason": (
        "The closure will not explain why the search was reasonably "
        "calculated to find responsive records."
    ),
    "retention_paragraph": (
        "The closure will not say whether records once existed and were "
        "destroyed under a retention schedule."
    ),
    "not_maintained_paragraph": (
        "A closure for a request the District keeps no record of will not "
        "say so, or cite WAC 44-14-04003(6) for why the District is not "
        "required to create one."
    ),
    "production_summary": (
        "The closure will not say what the District produced, so the "
        "requester cannot tell what they were given before the case closed."
    ),
    "amount_due": (
        "The closure will not say how much was owed, so a requester who "
        "wants to pay and re-request has no figure to act on."
    ),
    "payment_requested_on_date": (
        "The closure will not say when payment was requested, which is the "
        "fact that makes a non-payment closure defensible."
    ),
    "employee_name": (
        "The notice will not name the person whose records are at stake, so "
        "it does not read as being addressed to them at all."
    ),
    "disclosed_requesters": (
        "The notice will not say who asked for the records, which the "
        "notified person needs in order to decide whether to seek an order."
    ),
    "response_by_date": (
        "The notice will not give a date by which the notified person must "
        "act, so the District cannot show it waited a reasonable time."
    ),
    "match_count_phrase": (
        "The letter will ask the requester to consider narrowing without "
        "saying how much material the District found, which is the only "
        "reason the question is being asked."
    ),
    "query_summary": (
        "The letter will state a number without saying what was searched, so "
        "the requester cannot tell whether the District looked in the right "
        "place or judge the options offered."
    ),
    "narrowing_options": (
        "The letter will report a large number and offer the requester no "
        "narrower alternative, which reads as pressure to withdraw rather "
        "than as help."
    ),
    "response_by": (
        "The letter will not say by when the requester should reply."
    ),
    "fee_paragraph": (
        "The letter will not state the copying costs the requester would "
        "bear at this volume."
    ),
    "scope_response_paragraph": (
        "The letter will not say whether it is an RCW 42.56.520(3) "
        "clarification request. That sentence is the whole difference "
        "between a letter that tolls the District's response period and one "
        "that does not."
    ),
}


def _token_consequence(token: str) -> str:
    return TOKEN_CONSEQUENCES.get(
        token,
        f"The letter will not include the {token.replace('_', ' ')}.",
    )


def validate_template_syntax(
    kind: str,
    subject_template: str,
    body_template: str,
) -> tuple[str, str]:
    """Check only what makes a template renderable at all.

    These are not policy and are not overridable. An unknown token raises
    ``KeyError`` inside the substitution, and leftover template syntax has no
    defined output -- neither produces a letter anyone could send, so no
    reason makes them acceptable.
    """
    spec = template_spec(kind)
    subject = str(subject_template or "").strip()
    body = str(body_template or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    combined = f"{subject}\n{body}"
    unknown = set(_TOKEN.findall(combined)).difference(spec.allowed_tokens)
    if unknown:
        raise ValidationError(
            f"Unknown template token(s): {', '.join(sorted(unknown))}. "
            "These have no value to substitute, so the message cannot be "
            "produced at all."
        )
    scrubbed = _TOKEN.sub("sample", combined)
    if any(marker in scrubbed for marker in ("{{", "}}", "{%", "%}", "{#", "#}")):
        raise ValidationError("Template source contains unsupported template syntax.")
    validate_subject(_TOKEN.sub("sample", subject))
    canonical_body(_TOKEN.sub("sample", body))
    if len(body) > 20000:
        raise ValidationError("Message template body is too long.")
    return subject, body


def template_guardrail_warnings(
    kind: str,
    subject: str,
    body: str,
) -> tuple[GuardrailWarning, ...]:
    """Every content guardrail this source trips, with its consequence.

    Nothing here refuses the template. The caller decides, based on who is
    asking, whether tripping these stops the publish -- see ``guardrails``.
    """
    spec = template_spec(kind)
    combined = f"{subject}\n{body}"
    tokens = set(_TOKEN.findall(combined))
    warnings = [
        GuardrailWarning(
            code=f"missing_token:{token}",
            message=f"The template no longer uses {{{{ {token} }}}}.",
            consequence=_token_consequence(token),
        )
        for token in sorted(spec.required_tokens.difference(tokens))
    ]
    if "{{ request_number }}" not in subject:
        warnings.append(
            GuardrailWarning(
                code="subject_missing_request_number",
                message="The subject line no longer includes the request number.",
                consequence=(
                    "Replies will not thread onto the case automatically, so "
                    "staff will have to match them by hand."
                ),
            )
        )
    legal_copy = combined.lower().replace("five-business-day", "five business day")
    warnings.extend(
        GuardrailWarning(
            code=f"missing_phrase:{needle}",
            message=f"The template no longer contains \u201c{needle}\u201d.",
            consequence=message,
        )
        for needle, message in spec.required_phrases
        if needle not in legal_copy
    )
    return tuple(warnings)


def validate_template_source(
    kind: str,
    subject_template: str,
    body_template: str,
) -> tuple[str, str, tuple[GuardrailWarning, ...]]:
    """Normalize a template and report what it would cost to publish it.

    Raises only for a template that cannot render. Content guardrails come
    back as warnings for the caller to resolve against the actor's role.
    """
    subject, body = validate_template_syntax(kind, subject_template, body_template)
    return subject, body, template_guardrail_warnings(kind, subject, body)


def _date(value) -> str:
    return date_format(value, "F j, Y") if value is not None else ""


def _search_steps_block(steps) -> str:
    """Reproduce the built-in template's ``{% for step %}`` output exactly.

    Each iteration of that loop emits the newline before the entry, the entry
    across two lines with a two-space continuation indent, and the newline
    after it. ManagedTemplateFidelityTests asserts this byte-for-byte.
    """
    return "".join(
        f"\n- {step.get('location', '')} "
        f"(custodian: {step.get('custodian', '')}): "
        f"{step.get('method', '')};\n"
        f"  date range {step.get('date_range', '')}; "
        f"result: {step.get('result', '')}.\n"
        for step in steps or ()
    )


#: What an officer can weigh and a requester can answer in one reply. The
#: advisory proposes at most this many and the officer sends at most this
#: many, so the two never disagree about what a scope conversation looks like.
MAX_NARROWING_OPTIONS = 4

#: Said the same way in both forms so a reader can tell them apart at a
#: glance, and so neither can be edited into the other by accident.
SCOPE_RESPONSE_PARAGRAPHS = {
    "clarification": (
        "The District is requesting clarification of this request under\n"
        "RCW 42.56.520(3). The time for the District to respond is measured "
        "from its\nreceipt of your clarification. If the District receives "
        "no clarification and\nthe request remains unclear, it is not "
        "obligated to respond further."
    ),
    "informational": (
        "This letter is informational. The District is not requesting "
        "clarification\nunder RCW 42.56.520(3), the time for the District "
        "to respond is unchanged,\nand work on your request as submitted "
        "continues. You may narrow your\nrequest at any time by replying."
    ),
}


def narrowing_options_block(options) -> str:
    """Render the officer's chosen narrowing menu, or nothing at all."""
    if not options:
        return ""
    lines = ["\nNarrower alternatives you may wish to consider:\n"]
    for index, option in enumerate(options, start=1):
        terms = ", ".join(str(term) for term in option.get("terms", ()))
        lines.append(
            f"\n{index}. {option.get('rationale', '')}\n"
            f"   Search terms: {terms}\n"
        )
    return "".join(lines)


def match_count_phrase(match_count: int) -> str:
    """How many items the search universe holds, said in words."""
    return f"{match_count:,} item{'' if match_count == 1 else 's'}"


def fee_paragraph() -> str:
    """State copying costs only when an adopted schedule says what they are.

    RCW 42.56.120 forbids charging to locate records, and the District may
    not quote a cost at all before it adopts a fee schedule with counsel.
    Until then this renders empty rather than guessing.
    """
    from apps.records.fees import current_fee_schedule

    if current_fee_schedule() is None:
        return ""
    return (
        "\nThe District may charge the copying costs in its adopted fee "
        "schedule for the\nrecords it produces. It may not charge for the "
        "time spent locating or\npreparing them (RCW 42.56.120). A narrower "
        "request generally costs less.\n"
    )


def _values(context: dict) -> dict[str, str]:
    request = context.get("request")
    if request is None or request.requester_id is None:
        raise ValidationError(
            "Message-template rendering requires a request and requester."
        )
    installment = context.get("installment")
    previous_date = context.get("previous_date")
    item_count = context.get("item_count")
    values = {
        "request_number": request.number,
        "requester_name": request.requester.name or "Requester",
        "received_date": date_format(
            timezone.localtime(request.received_at),
            "F j, Y",
        ),
        "request_description": request.description,
        "pra_officer_email": runtime.get("PRA_OFFICER_EMAIL"),
        "estimated_completion": _date(context.get("estimated_completion")),
        "clarification_questions": context.get("clarification_questions", ""),
        "clarification_due": _date(context.get("clarification_due")),
        "new_date": _date(context.get("new_date")),
        "revision_reason": context.get("reason", ""),
        "previous_estimate_line": (
            f"Previous estimated completion: {_date(previous_date)}\n"
            if previous_date is not None
            else ""
        ),
        "installment_sequence": (
            str(installment.sequence) if installment is not None else ""
        ),
        "item_count_phrase": (
            ""
            if item_count is None
            else f"{item_count} record{'' if item_count == 1 else 's'}"
        ),
        "download_url": context.get("download_url", ""),
        "download_expires_at": (
            date_format(
                timezone.localtime(context["download_expires_at"]),
                "F j, Y, g:i a T",
            )
            if context.get("download_expires_at") is not None
            else ""
        ),
        "exemption_log_paragraph": (
            "\nThe package includes an exemption and withholding log "
            "identifying each\nredaction or record withheld in full and the "
            "legal basis applied under\nRCW 42.56.210(3).\n"
            if context.get("has_exemptions")
            else ""
        ),
        "final_installment_paragraph": (
            "\nDistrict staff have marked this as the final installment. "
            "Delivery of this\nnotice does not by itself close the request; "
            "closure remains a separate\nRecords Officer action.\n"
            if context.get("is_final")
            else "\nAdditional installments are currently expected.\n"
        ),
        "search_completed_date": (
            date_format(
                timezone.localtime(context["search_completed_at"]),
                "F j, Y",
            )
            if context.get("search_completed_at") is not None
            else ""
        ),
        "search_steps": _search_steps_block(context.get("search_steps")),
        # The scope-conversation fragments are rendered once, by the service
        # that validates them, and both template paths read the same strings.
        "match_count_phrase": context.get("match_count_phrase", ""),
        "query_summary": context.get("query_summary", ""),
        "narrowing_options": context.get("narrowing_options", ""),
        "response_by": _date(context.get("response_by")),
        "fee_paragraph": context.get("fee_paragraph", ""),
        "scope_response_paragraph": context.get(
            "scope_response_paragraph",
            "",
        ),
        "adequacy_reason": context.get("adequacy_reason", ""),
        "retention_paragraph": (
            # Two leading newlines: the built-in template's conditional opens
            # with a blank line before this paragraph.
            "\n\nThe requested records were outside the verified retention "
            "period. Retention\nschedule reference: "
            f"{context.get('retention_schedule_reference', '')}. Staff also "
            "checked\nincident holds, prior exports, email/Drive copies, and "
            "other likely copies.\n"
            if context.get("determination") == "outside_retention"
            else ""
        ),
        "not_maintained_paragraph": (
            # Two leading newlines, matching the built-in conditional.
            "\n\nThe District does not maintain a record answering this "
            "request. Under\nWAC 44-14-04003(6), the Public Records Act "
            "requires an agency to provide\nexisting identifiable records; "
            "the District is not required to create a record\nto answer a "
            "question or to compile information it does not already keep in\n"
            "record form.\n"
            if context.get("determination") == "not_maintained"
            else ""
        ),
        "estimated_amount": context.get("estimated_amount", ""),
        "estimate_basis": context.get("estimate_basis", ""),
        # The deposit is optional: an estimate with no deposit request
        # renders no deposit paragraph at all rather than a zero-dollar one.
        "deposit_paragraph": (
            "\nBefore the District does that copying work it asks for a "
            "deposit of\n"
            f"{context.get('deposit_amount', '')}. RCW 42.56.120(2) allows a "
            "deposit of up to ten\npercent of the estimated charge. The "
            "deposit is applied to the charge; it is\nnot an additional "
            "cost. The District will hold work on this request until the\n"
            "deposit is received.\n"
            if context.get("deposit_amount")
            else ""
        ),
        "production_summary": context.get("production_summary", ""),
        "amount_due": context.get("amount_due", ""),
        "payment_requested_on_date": _date(
            context.get("payment_requested_on")
        ),
        "employee_name": context.get("employee_name", ""),
        "disclosed_requesters": context.get("disclosed_requesters", ""),
        "response_by_date": _date(context.get("response_by")),
    }
    return {key: str(value) for key, value in values.items()}


def _require_present_values(spec: TemplateSpec, values: dict[str, str]) -> None:
    """Refuse to render a message whose mandatory content resolved empty.

    This applies to the built-in path as well as the managed one: a request
    with no download link or no search evidence must fail closed no matter
    which version of the template is active.
    """
    empty = sorted(
        token
        for token in spec.required_values
        if not values.get(token, "").strip()
    )
    if empty:
        raise ValidationError(
            "Message-template rendering is missing required content for: "
            f"{', '.join(empty)}."
        )


def require_present_template_values(kind: str, context: dict) -> None:
    """Public form of the mandatory-content check for the built-in path."""
    _require_present_values(template_spec(kind), _values(context))


def render_source(
    kind: str,
    subject_template: str,
    body_template: str,
    context: dict,
) -> RenderedMessage:
    spec = template_spec(kind)
    # Syntax only. Re-applying *content* guardrails here is what broke the
    # acknowledgment for eleven days: a rule tightened in code invalidated a
    # template the district had already published, and the failure surfaced to
    # an officer composing a letter months later. Content rules are decided
    # once, when a human publishes the template, by a human who can act on
    # them. Rendering only needs the substitution to be possible.
    subject_template, body_template = validate_template_syntax(
        kind,
        subject_template,
        body_template,
    )
    values = _values(context)
    _require_present_values(spec, values)

    def replace(match):
        return values[match.group(1)]

    return RenderedMessage(
        subject=validate_subject(_TOKEN.sub(replace, subject_template)),
        body=canonical_body(_TOKEN.sub(replace, body_template)),
    )


def validate_template_name(kind: str, name: str) -> str:
    """Normalize a template name and refuse one on a single-slot kind."""
    normalized = " ".join(str(name or "").split())
    if not normalized:
        return ""
    if kind not in AutomaticMessageTemplate.NAMED_KINDS:
        raise ValidationError(
            "Only general correspondence may carry several named templates; "
            "every other kind has exactly one."
        )
    if len(normalized) > 120:
        raise ValidationError("A template name is at most 120 characters.")
    return normalized


def canonical_template_name(kind: str, name: str) -> str:
    """The stored spelling of a name, so case alone never makes a second template."""
    normalized = validate_template_name(kind, name)
    if not normalized:
        return ""
    existing = (
        AutomaticMessageTemplate.objects.filter(kind=kind, name__iexact=normalized)
        .order_by("created_at")
        .values_list("name", flat=True)
        .first()
    )
    return existing or normalized


def active_template(
    kind: str,
    name: str = "",
) -> AutomaticMessageTemplate | None:
    template_spec(kind)
    return AutomaticMessageTemplate.objects.filter(
        kind=kind,
        name__iexact=validate_template_name(kind, name),
        state=AutomaticMessageTemplate.State.ACTIVE,
    ).first()


def active_template_names(kind: str) -> list[str]:
    """Every activated starting point for a named kind, name order.

    The unnamed default is reported as an empty string when it exists, so the
    compose dialog can list "District default" first without a second query.
    """
    if kind not in AutomaticMessageTemplate.NAMED_KINDS:
        return []
    return sorted(
        AutomaticMessageTemplate.objects.filter(
            kind=kind,
            state=AutomaticMessageTemplate.State.ACTIVE,
        ).values_list("name", flat=True)
    )


def render_active(kind: str, context: dict) -> tuple[RenderedMessage, str, str]:
    """Render a structured kind from its active managed or built-in version."""
    spec = template_spec(kind)
    if kind not in MANAGED_TEMPLATE_KINDS:
        raise ValidationError(
            "Free-form correspondence is authored by staff, not rendered from "
            "a template."
        )
    # Mandatory content is checked before either branch, so the built-in
    # template fails closed on missing content exactly like a managed one.
    _require_present_values(spec, _values(context))
    managed = active_template(kind)
    if managed is None:
        from .rendering import render_versioned_template

        return (
            render_versioned_template(
                spec.identifier,
                spec.builtin_version,
                context,
            ),
            spec.identifier,
            spec.builtin_version,
        )
    return (
        _render_managed(spec, managed, context),
        spec.identifier,
        f"managed:{managed.pk}",
    )


def _render_managed(
    spec: TemplateSpec,
    managed: AutomaticMessageTemplate,
    context: dict,
) -> RenderedMessage:
    """Render an activated template, blaming the template when it is at fault.

    ``render_source`` re-checks the stored source against the *current* spec,
    so tightening an authoring rule in code retroactively invalidates a
    template the district activated under the old rule. That is a real guard
    -- a rule tightened for a legal reason must not keep being bypassed by an
    old row -- but the officer who trips it is composing a letter, not editing
    a template, and the raw error names a token as though it were the form
    field they just filled in. Say what is actually wrong and who can fix it.
    """
    try:
        return render_source(
            spec.identifier,
            managed.subject_template,
            managed.body_template,
            context,
        )
    except ValidationError as exc:
        raise ValidationError(
            f"The active \u201c{spec.label}\u201d message template no longer "
            "meets the current requirements, so this message cannot be "
            f"drafted from it: {'; '.join(exc.messages)} A Records Officer "
            "must publish a corrected version under Message templates. "
            "Nothing you entered in this form caused this."
        ) from exc


def render_prefill(
    kind: str,
    context: dict,
    *,
    name: str = "",
) -> RenderedMessage:
    """Render a staff drafting starting point for a free-form kind.

    The result is a suggestion. The officer remains the author of record and
    the correspondence snapshot is built from what the officer submits.
    """
    spec = template_spec(kind)
    if kind not in FREE_FORM_TEMPLATE_KINDS:
        raise ValidationError(
            "This correspondence kind is rendered from its structured "
            "template, not from a drafting starting point."
        )
    name = validate_template_name(kind, name)
    managed = active_template(kind, name)
    if name and managed is None:
        raise ValidationError(
            f"There is no active \u201c{name}\u201d template for this kind."
        )
    return render_source(
        kind,
        managed.subject_template if managed else spec.subject_template,
        managed.body_template if managed else spec.body_template,
        context,
    )


def render_managed_reference(kind: str, reference: str, context: dict) -> RenderedMessage:
    if not reference.startswith("managed:"):
        raise ValidationError("Managed template reference is invalid.")
    try:
        template = AutomaticMessageTemplate.objects.get(
            pk=reference.removeprefix("managed:"),
            kind=kind,
            state__in=[
                AutomaticMessageTemplate.State.ACTIVE,
                AutomaticMessageTemplate.State.RETIRED,
            ],
        )
    except (AutomaticMessageTemplate.DoesNotExist, ValueError) as exc:
        raise ValidationError("Managed template version is unavailable.") from exc
    return render_source(
        kind,
        template.subject_template,
        template.body_template,
        context,
    )
