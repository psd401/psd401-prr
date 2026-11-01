"""Who may overrule a template guardrail, and what it costs them to do it.

The district owns the words it sends and counsel reviews them, so this app's
authoring rules cannot be the final word -- they have already been wrong in
production. But "anyone may write anything" is not the alternative. The rule
here is one policy, not a matrix: a Records Officer or Application Admin may
publish past a guardrail by saying why, and every other role cannot.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import guardrails, services
from apps.correspondence.automatic_templates import TEMPLATE_SPECS
from apps.correspondence.models import AutomaticMessageTemplate, Correspondence

ACK_SUBJECT = "Public Records Request {{ request_number }} — Acknowledgment"
#: Trips the statute guardrail: no "RCW 42.56.520" anywhere.
ACK_WITHOUT_STATUTE = (
    "Dear {{ requester_name }},\n\n"
    "We received your request and will respond within 60 business days.\n"
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class TemplateGuardrailPolicyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "guardrail-officer",
            "guardrail-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.admin = User.objects.create_user(
            "guardrail-admin",
            "guardrail-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN, StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "guardrail-reviewer",
            "guardrail-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})

    def _create(self, actor, override_reason=""):
        return services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template=ACK_SUBJECT,
            body_template=ACK_WITHOUT_STATUTE,
            change_reason="Adopt the wording the district actually uses.",
            actor=actor,
            guardrail_override_reason=override_reason,
        )

    def test_an_officer_may_publish_past_a_guardrail_with_a_reason(self):
        template = self._create(
            self.officer,
            override_reason=(
                "Counsel approved this wording; the statute is cited in the "
                "attached cover letter instead."
            ),
        )

        self.assertEqual(
            template.guardrail_overrides,
            ["missing_phrase:rcw 42.56.520"],
        )
        self.assertIn("Counsel approved", template.guardrail_override_reason)

    def test_an_admin_may_too(self):
        template = self._create(
            self.admin,
            override_reason="Administratively approved for the pilot mailbox.",
        )
        self.assertTrue(template.guardrail_overrides)

    def test_without_a_reason_the_officer_is_asked_for_one(self):
        with self.assertRaises(guardrails.GuardrailOverrideRequired) as caught:
            self._create(self.officer)

        # The prompt must carry what publishing would cost, so the officer is
        # deciding rather than guessing at a rejection.
        consequences = " ".join(
            warning.consequence for warning in caught.exception.warnings
        )
        self.assertIn("must cite the statute", consequences)
        self.assertEqual(AutomaticMessageTemplate.objects.count(), 0)

    def test_a_token_gesture_is_not_a_reason(self):
        with self.assertRaises(guardrails.GuardrailOverrideRequired):
            self._create(self.officer, override_reason="ok")

    def test_a_reviewer_is_refused_outright(self):
        """Lower roles stay restricted -- that was the whole requirement.

        For templates the refusal lands one layer earlier than the guardrail:
        authoring a template version is already Records Officer only, so a
        Reviewer never reaches the override question. Asserted here anyway,
        because the requirement is "lower-level users are restricted", and a
        later change that opened template authoring to more roles would
        otherwise hand them the override silently.
        """
        with self.assertRaises(PermissionDenied):
            self._create(
                self.reviewer,
                override_reason=(
                    "I would like to publish this wording without the statute."
                ),
            )
        self.assertEqual(AutomaticMessageTemplate.objects.count(), 0)

    def test_the_override_policy_itself_excludes_lower_roles(self):
        """The policy is asserted directly, independent of who may author.

        ``guardrails.may_override`` is the single place the answer lives, so
        any surface that adopts this mechanism inherits the same boundary.
        """
        self.assertTrue(guardrails.may_override(self.officer))
        self.assertTrue(guardrails.may_override(self.admin))
        self.assertFalse(guardrails.may_override(self.reviewer))

        for role in (StaffRole.LIAISON, StaffRole.INTAKE_CLERK):
            with self.subTest(role=role):
                apply_staff_roles(self.reviewer, {role})
                self.assertFalse(guardrails.may_override(self.reviewer))

    def test_a_clean_template_never_asks_for_a_reason(self):
        """The ordinary path must not acquire a new ceremony."""
        spec = TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT]
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template=spec.subject_template,
            body_template=spec.body_template,
            change_reason="Adopt the built-in wording verbatim.",
            actor=self.officer,
        )
        self.assertEqual(template.guardrail_overrides, [])
        self.assertEqual(template.guardrail_override_reason, "")

    def test_the_override_is_audited(self):
        self._create(
            self.officer,
            override_reason="Counsel approved this exact wording on review.",
        )
        entry = AuditLog.objects.filter(
            action="automatic_template.draft_created",
        ).latest("at")
        self.assertEqual(
            entry.detail["guardrail_overrides"],
            ["missing_phrase:rcw 42.56.520"],
        )
        self.assertIn("Counsel approved", entry.detail["guardrail_override_reason"])

    def test_an_unrenderable_template_is_refused_even_with_a_reason(self):
        """No reason makes a KeyError inside substitution acceptable."""
        with self.assertRaisesMessage(ValidationError, "cannot be produced"):
            services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                subject_template=ACK_SUBJECT,
                body_template=ACK_WITHOUT_STATUTE + "\n{{ download_url }}\n",
                change_reason="Adopt the wording the district actually uses.",
                actor=self.officer,
                guardrail_override_reason=(
                    "I accept every consequence of this template."
                ),
            )

    def test_an_edit_resolves_guardrails_the_same_way(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT]
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template=spec.subject_template,
            body_template=spec.body_template,
            change_reason="Start from the built-in wording.",
            actor=self.officer,
        )

        with self.assertRaises(guardrails.GuardrailOverrideRequired):
            services.edit_automatic_message_template_draft(
                template,
                subject_template=ACK_SUBJECT,
                body_template=ACK_WITHOUT_STATUTE,
                change_reason="Drop the statutory citation.",
                actor=self.officer,
                expected_version=template.revision,
            )

        edited = services.edit_automatic_message_template_draft(
            template,
            subject_template=ACK_SUBJECT,
            body_template=ACK_WITHOUT_STATUTE,
            change_reason="Drop the statutory citation.",
            actor=self.officer,
            expected_version=template.revision,
            guardrail_override_reason=(
                "Counsel directed this wording for the pilot."
            ),
        )
        self.assertEqual(
            edited.guardrail_overrides,
            ["missing_phrase:rcw 42.56.520"],
        )

    def test_activation_does_not_re_litigate_the_decision(self):
        """The officer decided when they wrote it; do not ask them twice."""
        template = self._create(
            self.officer,
            override_reason="Counsel approved this exact wording on review.",
        )
        activated = services.activate_automatic_message_template(
            template,
            actor=self.officer,
            expected_version=template.revision,
            confirmed=True,
            reason="Activate the counsel-reviewed district wording.",
        )
        self.assertEqual(activated.state, AutomaticMessageTemplate.State.ACTIVE)
