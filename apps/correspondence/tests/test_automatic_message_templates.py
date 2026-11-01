"""Governed editable templates for the two permitted automatic messages."""

import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.correspondence import services
from apps.correspondence.automatic_templates import active_template_names
from apps.correspondence.models import AutomaticMessageTemplate, Correspondence
from apps.records.models import PublicRecordsRequest, Requester

ACK_BODY = """Dear {{ requester_name }},

The District received {{ request_number }} on {{ received_date }}.

You requested:
{{ request_description }}

Under RCW 42.56.520, records are estimated for {{ estimated_completion }}.

Contact {{ pra_officer_email }}.
"""


class AutomaticMessageTemplateTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "template-officer",
            "template-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(
            name="Jane Public",
            email="jane@example.org",
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All records about the capital project.",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            estimated_completion=dt.date(2026, 9, 10),
        )

    def test_active_version_renders_new_acknowledgments_and_old_drafts_stay_stable(self):
        first = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY + "\nFIRST VERSION\n",
            change_reason="Use the district-approved acknowledgment wording.",
            actor=self.officer,
        )
        first = services.activate_automatic_message_template(
            first,
            actor=self.officer,
            expected_version=first.revision,
            confirmed=True,
            reason="Activate the reviewed district acknowledgment wording.",
        )
        original = services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )

        second = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY + "\nSECOND VERSION\n",
            change_reason="Revise the acknowledgment footer for current practice.",
            actor=self.officer,
        )
        services.activate_automatic_message_template(
            second,
            actor=self.officer,
            expected_version=second.revision,
            confirmed=True,
            reason="Activate the updated acknowledgment wording after review.",
        )

        original = services.approve_draft(
            original,
            actor=self.officer,
            expected_version=original.version,
        )
        current = services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )

        first.refresh_from_db()
        self.assertEqual(first.state, AutomaticMessageTemplate.State.RETIRED)
        self.assertIn("FIRST VERSION", original.approved_body)
        self.assertNotIn("SECOND VERSION", original.approved_body)
        self.assertIn("SECOND VERSION", current.draft_body)
        self.assertEqual(
            current.template_version,
            f"managed:{second.pk}",
        )

    def test_acknowledgment_accepts_officer_approved_legal_copy(self):
        """The district owns the wording around the statutory content."""
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District response",
            body_template=(
                "Dear Requester,\n\n"
                "The District acknowledges receipt and will respond under "
                "the legal-approved terms below. Per RCW 42.56.520 we "
                "estimate {{ estimated_completion }}.\n"
            ),
            change_reason="Use the exact acknowledgment language approved by legal.",
            actor=self.officer,
        )

        self.assertEqual(
            template.body_template,
            (
                "Dear Requester,\n\n"
                "The District acknowledges receipt and will respond under "
                "the legal-approved terms below. Per RCW 42.56.520 we "
                "estimate {{ estimated_completion }}."
            ),
        )

    def test_acknowledgment_cannot_drop_its_statute(self):
        """This letter is the RCW 42.56.520 response, so it must cite it."""
        with self.assertRaises(ValidationError):
            services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                subject_template="{{ request_number }} — Acknowledgment",
                body_template=(
                    "Dear Requester,\n\nWe estimate "
                    "{{ estimated_completion }}.\n"
                ),
                change_reason=(
                    "Attempt to remove the statutory citation."
                ),
                actor=self.officer,
            )

    def test_acknowledgment_may_word_the_estimate_without_the_token(self):
        """A duration is a lawful estimate of the time required to respond.

        What this validator can check mechanically is that the statute is
        cited and that the case carries a real estimate value. Whether a given
        sentence *conveys* an estimate is a reading question, and the district
        answered it with counsel: its approved wording says "60 business days"
        and never renders a date. Compelling the token here would reject that
        letter, which is what happened in production between PR #186 and the
        fix -- see test_acknowledgment_wording. The remaining assurance is the
        activation workflow: an officer, a substantive reason, and review.
        """
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — Acknowledgment",
            body_template=(
                "Dear {{ requester_name }},\n\nUnder RCW 42.56.520 the "
                "District estimates it will respond within 60 business "
                "days.\n"
            ),
            change_reason="Use the counsel-approved duration wording.",
            actor=self.officer,
        )
        self.assertNotIn("estimated_completion", template.body_template)

    def test_template_validation_rejects_unknown_tokens(self):
        with self.assertRaises(ValidationError):
            services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                subject_template="{{ request_number }} — Acknowledgment",
                body_template=ACK_BODY.replace(
                    "{{ estimated_completion }}",
                    "{{ secret_value }}",
                ),
                change_reason="Attempt an invalid unmanaged template token.",
                actor=self.officer,
            )

    def test_activated_template_content_cannot_be_edited(self):
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY,
            change_reason="Create the initial managed acknowledgment version.",
            actor=self.officer,
        )
        template = services.activate_automatic_message_template(
            template,
            actor=self.officer,
            expected_version=template.revision,
            confirmed=True,
            reason="Activate the initial managed acknowledgment version.",
        )
        template.body_template += "changed"

        with self.assertRaises(ValidationError):
            template.save()

    def test_activated_template_content_survives_a_bulk_rewrite(self):
        """``QuerySet.update()`` never calls ``save()``, so it needs its own guard.

        Approved correspondence reproduces its snapshot from the pinned
        template row, so a bulk rewrite of an activated version would break
        that reproduction silently.
        """
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY,
            change_reason="Create the initial managed acknowledgment version.",
            actor=self.officer,
        )
        template = services.activate_automatic_message_template(
            template,
            actor=self.officer,
            expected_version=template.revision,
            confirmed=True,
            reason="Activate the initial managed acknowledgment version.",
        )
        activated = AutomaticMessageTemplate.objects.filter(pk=template.pk)

        with self.assertRaises(ValidationError):
            activated.update(body_template="Rewritten behind the snapshot.")
        template.body_template = "Rewritten behind the snapshot."
        with self.assertRaises(ValidationError):
            AutomaticMessageTemplate.objects.bulk_update(
                [template],
                ["body_template"],
            )
        with self.assertRaises(ValidationError):
            activated.delete()

        template.refresh_from_db()
        self.assertIn("RCW 42.56.520", template.body_template)

    def test_a_draft_version_still_accepts_a_bulk_rewrite(self):
        """The guard is scoped to activation, not to every bulk write."""
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY,
            change_reason="Create a draft acknowledgment version.",
            actor=self.officer,
        )

        updated = AutomaticMessageTemplate.objects.filter(
            pk=template.pk
        ).update(body_template=ACK_BODY + "\nDraft edit.")

        self.assertEqual(updated, 1)

    def test_managed_template_formats_received_date_in_district_timezone(self):
        self.request.received_at = dt.datetime(2026, 8, 11, 5, 30, tzinfo=dt.UTC)
        self.request.save(update_fields=["received_at"])
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            subject_template="{{ request_number }} — District acknowledgment",
            body_template=ACK_BODY,
            change_reason="Create a managed acknowledgment for timezone review.",
            actor=self.officer,
        )
        services.activate_automatic_message_template(
            template,
            actor=self.officer,
            expected_version=template.revision,
            confirmed=True,
            reason="Activate the timezone-reviewed acknowledgment wording.",
        )

        draft = services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )

        self.assertIn("August 10, 2026", draft.draft_body)
        self.assertNotIn("August 11, 2026", draft.draft_body)


class NamedGeneralTemplateTests(AutomaticMessageTemplateTests):
    """General correspondence is many situations, so it is many templates."""

    GENERAL_BODY = (
        "Dear {{ requester_name }},\n\n"
        "About request {{ request_number }}.\n\n"
        "Contact {{ pra_officer_email }}.\n"
    )

    def publish(self, *, name, marker):
        template = services.create_automatic_message_template_draft(
            kind=Correspondence.Kind.GENERAL,
            name=name,
            subject_template="{{ request_number }} — District message",
            body_template=f"{self.GENERAL_BODY}\n{marker}\n",
            change_reason=f"Publish the {name or 'default'} general wording.",
            actor=self.officer,
        )
        return services.activate_automatic_message_template(
            template,
            actor=self.officer,
            expected_version=template.revision,
            confirmed=True,
            reason=f"Activate the reviewed {name or 'default'} wording.",
        )

    def test_several_named_general_templates_stay_active_together(self):
        default = self.publish(name="", marker="DEFAULT WORDING")
        reminder = self.publish(name="Fee reminder", marker="FEE REMINDER")
        follow_up = self.publish(name="Follow-up", marker="FOLLOW UP")

        for template in (default, reminder, follow_up):
            template.refresh_from_db()
            self.assertEqual(
                template.state,
                AutomaticMessageTemplate.State.ACTIVE,
            )

        self.assertIn(
            "DEFAULT WORDING",
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
            ).body,
        )
        self.assertIn(
            "FEE REMINDER",
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                name="Fee reminder",
            ).body,
        )
        self.assertEqual(
            active_template_names(Correspondence.Kind.GENERAL),
            ["", "Fee reminder", "Follow-up"],
        )

    def test_a_named_template_versions_and_retires_within_its_own_name(self):
        first = self.publish(name="Fee reminder", marker="FIRST")
        other = self.publish(name="Follow-up", marker="OTHER")
        second = self.publish(name="Fee reminder", marker="SECOND")

        first.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(first.state, AutomaticMessageTemplate.State.RETIRED)
        self.assertEqual(other.state, AutomaticMessageTemplate.State.ACTIVE)
        self.assertEqual(second.template_version, 2)
        self.assertEqual(other.template_version, 1)
        self.assertIn(
            "SECOND",
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                name="Fee reminder",
            ).body,
        )

    def test_a_statutory_kind_stays_a_single_slot(self):
        with self.assertRaisesMessage(ValidationError, "Only general"):
            services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                name="Second acknowledgment",
                subject_template="{{ request_number }} — District response",
                body_template=ACK_BODY,
                change_reason="Attempt a second active statutory wording.",
                actor=self.officer,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AutomaticMessageTemplate.objects.create(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                name="Smuggled past the service",
                template_version=99,
                subject_template="x",
                body_template="y",
                change_reason="Database constraint fixture.",
                created_by=self.officer,
            )

    def test_asking_for_a_template_that_is_not_published_says_so(self):
        with self.assertRaisesMessage(ValidationError, "no active"):
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                name="Never published",
            )

    def test_a_name_that_differs_only_in_case_is_the_same_template(self):
        first = self.publish(name="Fee reminder", marker="FIRST")
        second = self.publish(name="FEE REMINDER", marker="SECOND")

        first.refresh_from_db()
        # The first spelling is the template's name; a later spelling is a
        # new version of it, not a second active template beside it.
        self.assertEqual(second.name, "Fee reminder")
        self.assertEqual(second.template_version, 2)
        self.assertEqual(first.state, AutomaticMessageTemplate.State.RETIRED)
        self.assertEqual(
            active_template_names(Correspondence.Kind.GENERAL),
            ["Fee reminder"],
        )
        self.assertIn(
            "SECOND",
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                name="fee Reminder",
            ).body,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AutomaticMessageTemplate.objects.create(
                kind=Correspondence.Kind.GENERAL,
                name="fee reminder",
                template_version=1,
                state=AutomaticMessageTemplate.State.ACTIVE,
                subject_template="x",
                body_template="y",
                change_reason="Database constraint fixture.",
                created_by=self.officer,
                activated_by=self.officer,
                activated_at=timezone.now(),
            )

    def test_names_are_normalized_so_two_spellings_are_not_two_templates(self):
        self.publish(name="  Fee   reminder  ", marker="NORMALIZED")

        self.assertEqual(
            active_template_names(Correspondence.Kind.GENERAL),
            ["Fee reminder"],
        )
        self.assertIn(
            "NORMALIZED",
            services.render_free_form_prefill(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                name="Fee reminder",
            ).body,
        )
