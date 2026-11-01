# ABOUTME: Overridable template guardrails and who is allowed to override them.
# ABOUTME: Warns with the consequence rather than refusing the district's wording.
"""Template rules that warn an authorized officer instead of refusing them.

The district owns the words it sends. Counsel reviews them. When this app's
authoring rules and the district's approved letter disagree, the app is not
automatically right -- and it has already been wrong in production: PR #186
compelled an ``{{ estimated_completion }}`` token that the district's
counsel-approved acknowledgment expresses as "60 business days", and every
acknowledgment silently became unsendable for eleven days.

So these rules stop being refusals and become warnings, with two properties
that keep them worth having:

* **The consequence is stated, not the rule.** "Requesters will not receive a
  download link" is actionable. "Required template token(s) missing:
  download_url" is a puzzle whose answer is in the source code.
* **Overriding is attributable.** A Records Officer or Application Admin may
  proceed, but only by saying why, and the reason is stored on the template
  version and written to the audit log. Every other role is still refused --
  a Reviewer cannot quietly delete the statutory citation from a letter.

What is NOT overridable is anything that would make the template fail to
render at all: an unknown token raises ``KeyError`` inside the substitution,
and unbalanced template syntax has no defined output. Those are crashes, not
policy, and no reason makes them send.
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.api.identity import has_staff_role
from apps.api.models import StaffRole

#: Roles permitted to publish a template that trips a guardrail. Records
#: Officer is the statutory role; Application Admin administers the system.
#: Reviewer, Liaison, and Intake Clerk are refused -- they may still draft and
#: propose, but they cannot overrule a legal guardrail on their own signature.
OVERRIDE_ROLES = frozenset({StaffRole.RECORDS_OFFICER, StaffRole.ADMIN})

MINIMUM_OVERRIDE_REASON = 15


@dataclass(frozen=True)
class GuardrailWarning:
    """One rule this template trips, in the officer's terms."""

    code: str
    #: What is missing, named as content rather than as a token.
    message: str
    #: What the requester will or will not receive because of it.
    consequence: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "consequence": self.consequence,
        }


class GuardrailOverrideRequired(ValidationError):
    """The actor may proceed, but has not yet acknowledged the consequences.

    Carries the warnings so the API can render them and ask for a reason,
    rather than making the officer guess what "invalid" meant.
    """

    def __init__(self, warnings: tuple[GuardrailWarning, ...]):
        self.warnings = tuple(warnings)
        super().__init__(
            "This template does not meet a guardrail. Review what it means "
            "for the requester, then confirm with a reason to publish it "
            "anyway."
        )


class GuardrailNotOverridable(ValidationError):
    """The actor's role may not overrule this guardrail."""

    def __init__(self, warnings: tuple[GuardrailWarning, ...]):
        self.warnings = tuple(warnings)
        super().__init__(
            "This template does not meet a guardrail, and only a Records "
            "Officer or Application Admin may publish one that does not. "
            "Ask one of them to review it: "
            + " ".join(warning.consequence for warning in warnings)
        )


def may_override(actor) -> bool:
    return any(has_staff_role(actor, role) for role in OVERRIDE_ROLES)


def resolve(
    warnings,
    *,
    actor,
    override_reason: str,
) -> tuple[tuple[str, ...], str]:
    """Decide whether tripped guardrails stop this publish.

    Returns the acknowledged codes and the cleaned reason so the caller can
    store both on the template version. A template that trips nothing returns
    empty values and never asks for a reason -- the ordinary path is unchanged.
    """
    warnings = tuple(warnings)
    if not warnings:
        return (), ""
    if not may_override(actor):
        raise GuardrailNotOverridable(warnings)
    reason = str(override_reason or "").strip()
    if len(reason) < MINIMUM_OVERRIDE_REASON:
        # An empty or one-word reason is how an override log becomes useless
        # a year later, when someone is asking why a letter said what it said.
        raise GuardrailOverrideRequired(warnings)
    return tuple(warning.code for warning in warnings), reason
