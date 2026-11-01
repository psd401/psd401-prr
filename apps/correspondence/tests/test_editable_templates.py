"""District-editable templates for every requester-facing correspondence kind.

Covers the two families added beyond the original automatic pair: structured
kinds whose activated version renders the legal snapshot, and free-form kinds
whose template is only a staff drafting starting point.
"""

import datetime as dt
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.configuration import runtime
from apps.correspondence import automatic_templates, services
from apps.correspondence.automatic_templates import (
    FREE_FORM_TEMPLATE_KINDS,
    MANAGED_TEMPLATE_KINDS,
    TEMPLATE_SPECS,
    render_prefill,
    render_source,
    validate_template_source,
)
from apps.correspondence.models import AutomaticMessageTemplate, Correspondence
from apps.correspondence.rendering import render_versioned_template
from apps.records.models import PublicRecordsRequest, Requester


class EditableTemplateCatalogTests(TestCase):
    def test_every_outbound_requester_kind_is_editable(self):
        """Every kind a requester can receive has an editable template."""
        machine_only = {
            Correspondence.Kind.CLARIFICATION_RESPONSE,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        }
        expected = set(Correspondence.Kind.values) - machine_only
        self.assertSetEqual(set(TEMPLATE_SPECS), expected)
        self.assertSetEqual(
            set(AutomaticMessageTemplate.Kind.values),
            expected,
        )

    def test_families_partition_the_catalog(self):
        self.assertSetEqual(
            MANAGED_TEMPLATE_KINDS | FREE_FORM_TEMPLATE_KINDS,
            set(TEMPLATE_SPECS),
        )
        self.assertFalse(MANAGED_TEMPLATE_KINDS & FREE_FORM_TEMPLATE_KINDS)
        self.assertSetEqual(
            FREE_FORM_TEMPLATE_KINDS,
            {
                Correspondence.Kind.GENERAL,
                Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE,
                Correspondence.Kind.CLOSURE,
                Correspondence.Kind.DENIAL,
            },
        )

    def test_managed_kinds_match_the_structured_template_registry(self):
        """A managed version may only replace a real versioned template."""
        self.assertSetEqual(
            set(MANAGED_TEMPLATE_KINDS),
            set(services.LEGAL_TEMPLATE_VERSIONS),
        )

    def test_editability_does_not_widen_automation(self):
        """Only the acknowledgment and receipt may be machine-authored."""
        self.assertSetEqual(
            set(services.AUTOMATED_CORRESPONDENCE_KINDS),
            {
                Correspondence.Kind.ACKNOWLEDGMENT,
                Correspondence.Kind.SUBMISSION_RECEIPT,
            },
        )
        self.assertTrue(
            services.AUTOMATED_CORRESPONDENCE_KINDS < MANAGED_TEMPLATE_KINDS
        )

    def test_every_built_in_template_passes_its_own_validation(self):  # no-assert: validate_template_source raises
        for kind, spec in TEMPLATE_SPECS.items():
            with self.subTest(kind=kind):
                validate_template_source(
                    kind,
                    spec.subject_template,
                    spec.body_template,
                )

    def test_removing_a_required_token_warns_with_its_consequence(self):
        """The warning must say what the requester loses, not name a token.

        These no longer refuse the edit -- a Records Officer may publish past
        them with a recorded reason -- but the warning has to be actionable,
        which means naming the effect rather than the variable.
        """
        cases = {
            Correspondence.Kind.ESTIMATE_REVISION: (
                "new_date",
                "new date",
            ),
            Correspondence.Kind.INSTALLMENT_NOTICE: (
                "download_url",
                "will not receive a link",
            ),
            Correspondence.Kind.NO_RECORDS_CLOSURE: (
                "search_steps",
                "where the District looked",
            ),
            Correspondence.Kind.CLARIFICATION_REQUEST: (
                "clarification_questions",
                "without saying what is unclear",
            ),
        }
        for kind, (token, consequence) in cases.items():
            spec = TEMPLATE_SPECS[kind]
            stripped = spec.body_template.replace(f"{{{{ {token} }}}}", "")
            with self.subTest(kind=kind):
                _, _, warnings = validate_template_source(
                    kind,
                    spec.subject_template,
                    stripped,
                )
                codes = {warning.code for warning in warnings}
                self.assertIn(f"missing_token:{token}", codes)
                self.assertIn(
                    consequence,
                    " ".join(warning.consequence for warning in warnings),
                )

    def test_a_template_that_cannot_render_is_still_refused_outright(self):
        """An unknown token is a crash, not a policy an officer may overrule."""
        spec = TEMPLATE_SPECS[Correspondence.Kind.CLOSURE]
        with self.assertRaisesMessage(ValidationError, "cannot be produced"):
            validate_template_source(
                Correspondence.Kind.CLOSURE,
                spec.subject_template,
                spec.body_template + "\n{{ download_url }}\n",
            )

    def test_no_records_closure_warns_about_its_non_exemption_statement(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.NO_RECORDS_CLOSURE]
        body = spec.body_template.replace(
            "This is not a determination that responsive records are exempt.",
            "",
        )
        _, _, warnings = validate_template_source(
            Correspondence.Kind.NO_RECORDS_CLOSURE,
            spec.subject_template,
            body,
        )
        self.assertIn(
            "not an exemption determination",
            " ".join(warning.consequence for warning in warnings),
        )

    def test_unknown_token_is_rejected_per_kind(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.CLOSURE]
        with self.assertRaisesMessage(ValidationError, "download_url"):
            validate_template_source(
                Correspondence.Kind.CLOSURE,
                spec.subject_template,
                spec.body_template + "\n{{ download_url }}\n",
            )

    def test_every_built_in_template_trips_no_guardrail(self):
        """The shipped defaults must never need an override to publish."""
        for kind, spec in TEMPLATE_SPECS.items():
            with self.subTest(kind=kind):
                _, _, warnings = validate_template_source(
                    kind,
                    spec.subject_template,
                    spec.body_template,
                )
                self.assertEqual(warnings, ())


class BuiltInDerivationGuardTests(TestCase):
    """The derivation must fail closed rather than serve a broken template."""

    def test_a_field_with_no_safe_token_is_refused_at_derivation(self):
        from apps.correspondence import automatic_templates

        spec = TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT]
        with self.assertRaisesMessage(ImproperlyConfigured, "no safe token"):
            automatic_templates._builtin_default(
                "acknowledgment",
                "2",
                # A bare, un-renamed field has no dot and no filter, so only
                # the allowed-token check can catch it.
                spec.allowed_tokens - {"estimated_completion"},
            )

    def test_template_logic_with_no_rename_is_refused_at_derivation(self):
        from apps.correspondence import automatic_templates

        original = automatic_templates._TOKEN_RENAMES["no_records_closure"]
        # Drop the search-step loop rename; the {% for %} must then be caught.
        automatic_templates._TOKEN_RENAMES["no_records_closure"] = tuple(
            rename
            for rename in original
            if "{% for" not in rename[0]
        )
        try:
            with self.assertRaisesMessage(
                ImproperlyConfigured,
                "template logic with no safe token",
            ):
                automatic_templates._builtin_default(
                    "no_records_closure",
                    "1",
                    TEMPLATE_SPECS[
                        Correspondence.Kind.NO_RECORDS_CLOSURE
                    ].allowed_tokens,
                )
        finally:
            automatic_templates._TOKEN_RENAMES["no_records_closure"] = original


class StructuredTemplateBase(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "editable-officer",
            "editable-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jane Public",
                email="jane@example.org",
            ),
            description="All records about the capital project.",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            estimated_completion=dt.date(2026, 9, 10),
            statutory_response_due=dt.date(2026, 8, 17),
        )

    def activate(self, kind, *, subject=None, body):
        draft = services.create_automatic_message_template_draft(
            kind=kind,
            subject_template=subject or TEMPLATE_SPECS[kind].subject_template,
            body_template=body,
            change_reason="Use the district-approved wording for this kind.",
            actor=self.officer,
        )
        return services.activate_automatic_message_template(
            draft,
            actor=self.officer,
            expected_version=draft.revision,
            confirmed=True,
            reason="Activate the counsel-reviewed district wording.",
        )


class StructuredEditableTemplateTests(StructuredTemplateBase):
    def test_activated_clarification_wording_renders_the_draft(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.CLARIFICATION_REQUEST]
        managed = self.activate(
            Correspondence.Kind.CLARIFICATION_REQUEST,
            body=spec.body_template + "\nDISTRICT CLARIFICATION FOOTER\n",
        )
        correspondence = services.create_clarification_draft(
            self.request,
            clarification_questions="Which schools and which date range?",
            clarification_due=dt.date(2026, 8, 24),
            actor=self.officer,
        )
        self.assertEqual(
            correspondence.template_identifier,
            "clarification_request",
        )
        self.assertEqual(
            correspondence.template_version,
            f"managed:{managed.pk}",
        )
        self.assertIn("DISTRICT CLARIFICATION FOOTER", correspondence.draft_body)
        self.assertIn(
            "Which schools and which date range?",
            correspondence.draft_body,
        )
        self.assertIn("August 24, 2026", correspondence.draft_body)

    def test_built_in_version_is_used_until_a_version_is_activated(self):
        correspondence = services.create_clarification_draft(
            self.request,
            clarification_questions="Which schools and which date range?",
            clarification_due=dt.date(2026, 8, 24),
            actor=self.officer,
        )
        self.assertEqual(correspondence.template_version, "1")

    def test_managed_snapshot_is_reproducible_after_activation_of_a_successor(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.ESTIMATE_REVISION]
        first = self.activate(
            Correspondence.Kind.ESTIMATE_REVISION,
            body=spec.body_template + "\nFIRST DISTRICT WORDING\n",
        )
        correspondence = services.create_estimate_revision_draft(
            self.request,
            new_date=dt.date(2026, 10, 8),
            reason="A third-party notice under RCW 42.56.540 added time.",
            actor=self.officer,
        )
        self.assertEqual(
            correspondence.template_version,
            f"managed:{first.pk}",
        )
        self.activate(
            Correspondence.Kind.ESTIMATE_REVISION,
            body=spec.body_template + "\nSECOND DISTRICT WORDING\n",
        )
        edited = services.edit_structured_draft(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
            structured_data={
                "new_date": dt.date(2026, 10, 15),
                "reason": "The third-party notice period was extended.",
            },
        )
        # The draft keeps rendering from the version it was created under.
        self.assertIn("FIRST DISTRICT WORDING", edited.draft_body)
        self.assertNotIn("SECOND DISTRICT WORDING", edited.draft_body)
        self.assertIn("October 15, 2026", edited.draft_body)

    def test_missing_mandatory_content_fails_closed_on_the_built_in_path(self):
        """No managed version is active, so the built-in must still refuse."""
        from apps.correspondence.automatic_templates import active_template

        self.assertIsNone(
            active_template(Correspondence.Kind.CLARIFICATION_REQUEST)
        )
        with self.assertRaisesMessage(
            ValidationError,
            "clarification_questions",
        ):
            services._render_template_draft(
                self.request,
                kind=Correspondence.Kind.CLARIFICATION_REQUEST,
                context={
                    "clarification_questions": "",
                    "clarification_due": dt.date(2026, 8, 24),
                },
                metadata={},
                actor=self.officer,
            )

    def test_both_render_paths_enforce_the_same_mandatory_content(self):
        for activate_managed in (False, True):
            with self.subTest(managed=activate_managed):
                request = PublicRecordsRequest.objects.create(
                    requester=Requester.objects.create(
                        name="Jane Public",
                        email=f"jane+{activate_managed}@example.org",
                    ),
                    description="All records about the capital project.",
                    received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
                    statutory_response_due=dt.date(2026, 8, 17),
                )
                if activate_managed:
                    self.activate(
                        Correspondence.Kind.ESTIMATE_REVISION,
                        body=TEMPLATE_SPECS[
                            Correspondence.Kind.ESTIMATE_REVISION
                        ].body_template,
                    )
                with self.assertRaisesMessage(
                    ValidationError,
                    "revision_reason",
                ):
                    services._render_template_draft(
                        request,
                        kind=Correspondence.Kind.ESTIMATE_REVISION,
                        context={
                            "previous_date": None,
                            "new_date": dt.date(2026, 10, 8),
                            "reason": "   ",
                        },
                        metadata={},
                        actor=self.officer,
                    )

    def test_activation_still_requires_a_records_officer(self):
        liaison = get_user_model().objects.create_user(
            "editable-liaison",
            "editable-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        spec = TEMPLATE_SPECS[Correspondence.Kind.CLOSURE]
        with self.assertRaises(Exception):
            services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.CLOSURE,
                subject_template=spec.subject_template,
                body_template=spec.body_template,
                change_reason="Try to change closure wording without authority.",
                actor=liaison,
            )


class FreeFormTemplateTests(StructuredTemplateBase):
    def test_prefill_uses_the_activated_version(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.GENERAL]
        self.activate(
            Correspondence.Kind.GENERAL,
            body=spec.body_template + "\nDISTRICT GENERAL FOOTER\n",
        )
        rendered = services.render_free_form_prefill(
            self.request,
            kind=Correspondence.Kind.GENERAL,
        )
        self.assertIn("DISTRICT GENERAL FOOTER", rendered.body)
        self.assertIn(self.request.number, rendered.subject)

    def test_prefill_falls_back_to_the_built_in_starting_point(self):
        rendered = render_prefill(
            Correspondence.Kind.DENIAL,
            {"request": self.request},
        )
        self.assertIn(self.request.number, rendered.subject)
        self.assertIn("Public Records Officer", rendered.body)

    def test_prefill_refuses_a_structured_kind(self):
        with self.assertRaises(ValidationError):
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
            )

    def test_authored_draft_stays_human_authored_not_template_rendered(self):
        """A starting point never becomes the legal snapshot on its own."""
        spec = TEMPLATE_SPECS[Correspondence.Kind.GENERAL]
        self.activate(
            Correspondence.Kind.GENERAL,
            body=spec.body_template + "\nDISTRICT GENERAL FOOTER\n",
        )
        correspondence = services.create_authored_draft(
            self.request,
            kind=Correspondence.Kind.GENERAL,
            subject=f"Public Records Request {self.request.number}",
            body="The officer wrote exactly this and nothing else.",
            actor=self.officer,
        )
        self.assertEqual(correspondence.template_identifier, "")
        self.assertEqual(correspondence.template_version, "")
        self.assertNotIn("DISTRICT GENERAL FOOTER", correspondence.draft_body)
        self.assertEqual(correspondence.created_by, self.officer)
        self.assertFalse(correspondence.automated)


class OfficerSubmissionReceiptTests(StructuredTemplateBase):
    def test_officer_receipt_is_attributed_and_non_statutory(self):
        correspondence = services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.DRAFT)
        self.assertEqual(correspondence.created_by, self.officer)
        self.assertFalse(correspondence.automated)
        self.assertNotIn(
            Correspondence.Kind.SUBMISSION_RECEIPT,
            services.REQUEST_LIFECYCLE_EFFECT_KINDS,
        )
        self.request.refresh_from_db()
        self.assertFalse(self.request.initial_response_delivered)
        self.assertEqual(self.request.events.count(), 0)
        self.assertIn("RCW 42.56.520", correspondence.draft_body)

    def test_only_one_receipt_may_exist_per_request(self):
        services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "already has a submission receipt",
        ):
            services.create_officer_submission_receipt_draft(
                self.request,
                actor=self.officer,
            )

    def test_a_cancelled_receipt_may_be_replaced(self):
        first = services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        approved = services.approve_draft(
            first,
            actor=self.officer,
            expected_version=first.version,
        )
        services.cancel_unsent(
            approved,
            actor=self.officer,
            expected_version=approved.version,
            reason="Wrong requester address on file; recreate the receipt.",
        )
        second = services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_an_approved_receipt_still_blocks_a_second_one(self):
        first = services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        services.approve_draft(
            first,
            actor=self.officer,
            expected_version=first.version,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "already has a submission receipt",
        ):
            services.create_officer_submission_receipt_draft(
                self.request,
                actor=self.officer,
            )

    def test_the_receipt_requires_a_current_human_staff_identity(self):
        with self.assertRaises(Exception):
            services.create_officer_submission_receipt_draft(
                self.request,
                actor=None,
            )

    def test_officer_receipt_uses_the_activated_district_wording(self):
        spec = TEMPLATE_SPECS[Correspondence.Kind.SUBMISSION_RECEIPT]
        managed = self.activate(
            Correspondence.Kind.SUBMISSION_RECEIPT,
            body=spec.body_template + "\nDISTRICT RECEIPT FOOTER\n",
        )
        correspondence = services.create_officer_submission_receipt_draft(
            self.request,
            actor=self.officer,
        )
        self.assertEqual(
            correspondence.template_version,
            f"managed:{managed.pk}",
        )
        self.assertIn("DISTRICT RECEIPT FOOTER", correspondence.draft_body)


class InstallmentAndSearchTokenTests(StructuredTemplateBase):
    """The pre-rendered tokens that replace built-in template logic."""

    def test_out_of_retention_paragraph_only_appears_for_that_determination(self):
        from apps.correspondence.automatic_templates import _values

        base = {"request": self.request}
        self.assertEqual(_values(base)["retention_paragraph"], "")
        outside = _values(
            base
            | {
                "determination": "outside_retention",
                "retention_schedule_reference": "GS50-01A-11",
            }
        )
        self.assertIn("outside the verified retention", outside["retention_paragraph"])
        self.assertIn("GS50-01A-11", outside["retention_paragraph"])

    def test_installment_paragraphs_reflect_exemptions_and_finality(self):
        from apps.correspondence.automatic_templates import _values

        base = {"request": self.request}
        neither = _values(base)
        self.assertEqual(neither["exemption_log_paragraph"], "")
        self.assertIn("Additional installments", neither["final_installment_paragraph"])
        both = _values(base | {"has_exemptions": True, "is_final": True})
        self.assertIn("RCW 42.56.210(3)", both["exemption_log_paragraph"])
        self.assertIn("final installment", both["final_installment_paragraph"])

    def test_search_step_block_renders_every_step(self):
        from apps.correspondence.automatic_templates import _values

        values = _values(
            {
                "request": self.request,
                "search_completed_at": timezone.now(),
                "search_steps": [
                    {
                        "location": "District email",
                        "custodian": "CIO",
                        "method": "Vault export",
                        "date_range": "2026",
                        "result": "None",
                    },
                    {
                        "location": "Shared drive",
                        "custodian": "Facilities",
                        "method": "Filename search",
                        "date_range": "2026",
                        "result": "None",
                    },
                ],
            }
        )
        # Two steps, each emitting its leading newline, a two-line entry, and
        # its trailing newline - the built-in loop's exact shape.
        self.assertEqual(values["search_steps"].count("\n"), 6)
        self.assertIn("- District email (custodian: CIO): Vault export;\n", values["search_steps"])
        self.assertIn("  date range 2026; result: None.\n", values["search_steps"])
        self.assertIn("Shared drive", values["search_steps"])

class ManagedTemplateFidelityTests(TestCase):
    """A managed copy of a built-in template must render byte-identically.

    This is the load-bearing claim of the structured family: the token-only
    managed source, including the pre-rendered paragraph tokens that stand in
    for the built-in Django templates' loops and conditionals, produces the
    same requester-facing message as the source-controlled template it
    replaces. If a token substitution drifts, this fails.
    """

    maxDiff = None

    def setUp(self):
        self.request = SimpleNamespace(
            requester_id="sample",
            requester=SimpleNamespace(name="Jane Public"),
            number="PRR-2026-0001",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            description=(
                "Emails and attachments concerning the capital project."
            ),
        )

    def contexts(self, kind):
        """Every context variant whose conditional paragraphs differ."""
        # Mirrors services._base_context, which the built-in templates read.
        base = {
            "request": self.request,
            "pra_officer_email": runtime.get("PRA_OFFICER_EMAIL"),
        }
        if kind == Correspondence.Kind.ACKNOWLEDGMENT:
            return [base | {"estimated_completion": dt.date(2026, 9, 10)}]
        if kind == Correspondence.Kind.SUBMISSION_RECEIPT:
            return [base]
        if kind == Correspondence.Kind.CLARIFICATION_REQUEST:
            return [
                base
                | {
                    "clarification_questions": (
                        "Which schools and which date range?"
                    ),
                    "clarification_due": dt.date(2026, 8, 24),
                }
            ]
        if kind == Correspondence.Kind.ESTIMATE_REVISION:
            revision = base | {
                "new_date": dt.date(2026, 10, 8),
                "reason": "A third-party notice added review time.",
            }
            # With and without a previous estimate.
            return [
                revision | {"previous_date": dt.date(2026, 9, 10)},
                revision | {"previous_date": None},
            ]
        if kind == Correspondence.Kind.SCOPE_CLARIFICATION:
            scope = base | {
                "match_count_phrase": "29,380 items",
                "query_summary": (
                    "district Gmail for the named employee and the budget "
                    "terms you used"
                ),
                "response_by": dt.date(2026, 9, 18),
            }
            # With and without a narrowing menu, and with and without an
            # adopted fee schedule's paragraph, in both forms.
            return [
                scope
                | {
                    "narrowing_options": options,
                    "fee_paragraph": fee,
                    "scope_response_paragraph": paragraph,
                }
                for options in (
                    "",
                    automatic_templates.narrowing_options_block(
                        [
                            {
                                "terms": ["budget", "2025"],
                                "rationale": "Only the 2025 budget thread.",
                            }
                        ]
                    ),
                )
                for fee in ("", "\nA fee paragraph.\n")
                for paragraph in (
                    automatic_templates.SCOPE_RESPONSE_PARAGRAPHS.values()
                )
            ]
        if kind == Correspondence.Kind.INSTALLMENT_NOTICE:
            notice = base | {
                "installment": SimpleNamespace(sequence=2),
                "download_url": "https://prr.psd401.net/downloads/sample",
                "download_expires_at": dt.datetime(
                    2026, 9, 24, 17, 0, tzinfo=dt.UTC
                ),
            }
            # Every exemption/final/pluralization combination.
            return [
                notice
                | {
                    "item_count": count,
                    "has_exemptions": has_exemptions,
                    "is_final": is_final,
                }
                for count in (1, 14)
                for has_exemptions in (True, False)
                for is_final in (True, False)
            ]
        if kind == Correspondence.Kind.COMPLETION_CLOSURE:
            return [
                base
                | {
                    "production_summary": (
                        "The District produced 214 pages across three "
                        "installments and withheld nothing."
                    )
                }
            ]
        if kind == Correspondence.Kind.NONPAYMENT_CLOSURE:
            return [
                base
                | {
                    "amount_due": "$125.00",
                    "payment_requested_on": dt.date(2026, 8, 12),
                }
            ]
        if kind == Correspondence.Kind.FEE_ESTIMATE:
            # With and without the optional deposit paragraph.
            return [
                base
                | {
                    "estimated_amount": "$367.25",
                    "estimate_basis": (
                        "29,380 electronic files at five cents per four "
                        "files under the adopted schedule."
                    ),
                    "deposit_amount": deposit,
                }
                for deposit in ("$36.72", "")
            ]
        if kind == Correspondence.Kind.THIRD_PARTY_NOTICE:
            return [
                base
                | {
                    "employee_name": "Alex Doe",
                    "disclosed_requesters": "Jane Public",
                    "response_by": dt.date(2026, 9, 24),
                }
            ]
        closure = base | {
            "search_completed_at": dt.datetime(
                2026, 9, 2, 16, 0, tzinfo=dt.UTC
            ),
            "adequacy_reason": "Every registry system was searched.",
            "retention_schedule_reference": "GS50-01A-11",
        }
        steps = [
            {
                "location": "District email (Google Vault)",
                "custodian": "Chief Information Officer",
                "method": "Vault matter export by keyword",
                "date_range": "2026-01-01 to 2026-06-30",
                "result": "No responsive messages",
            },
            {
                "location": "Facilities shared drive",
                "custodian": "Director of Facilities",
                "method": "Filename and full-text search",
                "date_range": "2026-01-01 to 2026-06-30",
                "result": "No responsive files",
            },
        ]
        # One and many steps, in and out of retention.
        return [
            closure | {"search_steps": steps[:count], "determination": determination}
            for count in (1, 2)
            for determination in (
                "no_responsive_records",
                "outside_retention",
                "not_maintained",
            )
        ]

    def test_managed_copy_matches_the_built_in_it_replaces(self):
        for kind in sorted(MANAGED_TEMPLATE_KINDS):
            spec = TEMPLATE_SPECS[kind]
            for index, context in enumerate(self.contexts(kind)):
                with self.subTest(kind=kind, context=index):
                    builtin = render_versioned_template(
                        spec.identifier,
                        spec.builtin_version,
                        context,
                    )
                    managed = render_source(
                        kind,
                        spec.subject_template,
                        spec.body_template,
                        context,
                    )
                    self.assertEqual(builtin.subject, managed.subject)
                    self.assertEqual(builtin.body, managed.body)
