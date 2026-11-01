"""The scope conversation: a count, options, and one clock-stopping choice.

The whole point of this letter is to make a large request tractable without
denying any part of it. Two things therefore have to be true at once: an
officer must be able to say "your request matched 29,380 items, here are three
narrower options", and nothing in the system may say that on its own.
"""

import datetime as dt

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings

from apps.correspondence import services
from apps.records.models import FeeSchedule, RequestStatus

from .test_services import CorrespondenceServiceTestCase

OPTIONS = [
    {
        "terms": ["transportation plan", "2025"],
        "rationale": "Only the 2025 transportation plan and its revisions.",
    },
    {
        "terms": ["bus route"],
        "rationale": "Only route-level correspondence, not the plan itself.",
    },
]


@override_settings(PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True)
class ScopeClarificationDraftTests(CorrespondenceServiceTestCase):
    def draft(self, *, form="informational", actor=None, **overrides):
        payload = {
            "form": form,
            "match_count": 29380,
            "query_summary": (
                "district Gmail for the transportation-plan terms, through "
                "the date the request arrived"
            ),
            "narrowing_options": OPTIONS,
            "response_by": dt.date(2026, 8, 24),
        }
        payload.update(overrides)
        return services.create_scope_clarification_draft(
            self.prr,
            actor=actor or self.officer,
            **payload,
        )

    def test_the_letter_states_the_count_and_every_chosen_option(self):
        correspondence = self.draft()

        body = correspondence.draft_body
        self.assertIn("29,380 items", body)
        self.assertIn("Only the 2025 transportation plan", body)
        self.assertIn("Only route-level correspondence", body)
        self.assertIn("transportation plan, 2025", body)
        self.assertIn("August 24, 2026", body)
        self.assertIn("not a denial of any part of your request", body)

    def test_the_informational_form_says_the_clock_keeps_running(self):
        body = self.draft(form="informational").draft_body

        self.assertIn("This letter is informational", body)
        self.assertIn("is not requesting clarification", body)
        self.assertIn("time for the District to respond is unchanged", body)

    def test_the_clarification_form_says_it_stops_the_clock(self):
        body = self.draft(form="clarification").draft_body

        self.assertIn("requesting clarification of this request under", body)
        self.assertIn("RCW 42.56.520(3)", body)
        self.assertIn("receipt of your clarification", body)
        self.assertIn("it is not obligated to respond further", body)

    def test_a_count_with_no_options_is_still_a_lawful_letter(self):
        body = self.draft(narrowing_options=[]).draft_body

        self.assertIn("29,380 items", body)
        self.assertNotIn("Narrower alternatives", body)

    def test_no_fee_line_appears_until_the_district_adopts_a_schedule(self):
        self.assertIsNone(FeeSchedule.objects.first())

        body = self.draft().draft_body

        self.assertNotIn("fee schedule", body)
        self.assertNotIn("RCW 42.56.120", body)

    def test_the_fee_line_appears_once_a_schedule_is_active(self):
        FeeSchedule.objects.create(
            effective_on=dt.date(2026, 1, 1),
            counsel_approval_reference="Counsel memo 2026-03",
            adoption_reference="Board resolution 26-04",
            activated_by=self.officer,
        )

        body = self.draft().draft_body

        self.assertIn("adopted fee schedule", body)
        self.assertIn("RCW 42.56.120", body)

    def test_the_form_must_be_chosen_explicitly(self):
        with self.assertRaisesMessage(
            ValidationError,
            "informational or a clarification request",
        ):
            self.draft(form="")

    def test_more_than_four_options_is_a_menu_not_a_conversation(self):
        with self.assertRaisesMessage(ValidationError, "at most 4"):
            self.draft(
                narrowing_options=[
                    {"terms": [f"term {index}"], "rationale": "A rationale."}
                    for index in range(5)
                ],
            )

    def test_an_option_without_a_rationale_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "would cover"):
            self.draft(
                narrowing_options=[{"terms": ["budget"], "rationale": "no"}],
            )

    def test_an_option_without_terms_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "one to ten terms"):
            self.draft(
                narrowing_options=[
                    {"terms": [], "rationale": "Only the budget thread."}
                ],
            )

    def test_a_negative_count_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "non-negative"):
            self.draft(match_count=-1)

    def test_only_a_records_officer_may_draft_it(self):
        with self.assertRaises(PermissionDenied):
            self.draft(actor=self.liaison)

    def test_editing_re_renders_the_letter_from_the_new_inputs(self):
        correspondence = self.draft(form="informational")

        edited = services.edit_structured_draft(
            correspondence,
            structured_data={
                "form": "clarification",
                "match_count": 12,
                "query_summary": "district Gmail for the bus-route terms",
                "narrowing_options": [],
                "response_by": "2026-09-01",
            },
            actor=self.officer,
            expected_version=correspondence.version,
        )

        self.assertIn("12 items", edited.draft_body)
        self.assertIn("RCW 42.56.520(3)", edited.draft_body)
        self.assertEqual(edited.draft_metadata["form"], "clarification")


@override_settings(PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True)
class ScopeClarificationDeliveryEffectTests(CorrespondenceServiceTestCase):
    def send(self, form):
        correspondence = services.create_scope_clarification_draft(
            self.prr,
            form=form,
            match_count=29380,
            query_summary="district Gmail for the transportation-plan terms",
            narrowing_options=OPTIONS,
            response_by=dt.date(2026, 8, 24),
            actor=self.officer,
        )
        correspondence, delivery = self.approve_and_queue(correspondence)
        self.mark_sent(correspondence, delivery)
        correspondence.refresh_from_db()
        services.apply_successful_delivery_effects(correspondence)
        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        return correspondence

    def test_the_clarification_form_tolls_the_statutory_clock(self):
        correspondence = self.send("clarification")

        self.assertEqual(self.prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNotNone(correspondence.resulting_status_event_id)
        self.assertTrue(
            self.prr.clarification_cycles.filter(
                outbound_correspondence=correspondence,
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            ).exists()
        )

    def test_the_informational_form_leaves_the_request_exactly_as_it_was(self):
        before = self.prr.status

        correspondence = self.send("informational")

        self.assertEqual(self.prr.status, before)
        self.assertIsNone(correspondence.resulting_status_event_id)
        self.assertFalse(self.prr.clarification_cycles.exists())

    def test_an_informational_letter_cannot_be_used_to_toll_the_clock(self):
        """The refusal that keeps the two forms from being interchangeable.

        A caller holding a delivered informational letter must not be able to
        hand it to ``transition`` and toll a clock the requester was told was
        still running.
        """
        from apps.records.services import (
            CorrespondenceDeliveryRequired,
            transition,
        )

        correspondence = self.send("informational")

        with self.assertRaisesMessage(
            CorrespondenceDeliveryRequired,
            "does not request clarification",
        ):
            transition(
                self.prr,
                RequestStatus.CLARIFICATION,
                actor=self.officer,
                note="Attempting to toll from an informational letter.",
                visible_to_requester=True,
                occurred_at=correspondence.sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )

    def test_only_one_clarification_conversation_may_be_outstanding(self):
        self.send("clarification")

        second = services.create_scope_clarification_draft(
            self.prr,
            form="clarification",
            match_count=10,
            query_summary="district Gmail for the bus-route terms",
            narrowing_options=[],
            response_by=dt.date(2026, 9, 1),
            actor=self.officer,
        )
        approved = services.approve_draft(
            second,
            actor=self.officer,
            expected_version=second.version,
        )

        with self.assertRaisesMessage(ValidationError, "outstanding"):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )
