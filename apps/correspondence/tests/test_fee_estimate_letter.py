"""The RCW 42.56.120 copying estimate and its optional deposit line.

A fee estimate is the first time a requester hears a number, so what it says
has to be reconstructable from the case rather than from whatever the officer
typed that day: the amount, how it was reached, and the deposit if there is
one. Delivering it changes no status and starts no clock; it only tells the
requester what the copies cost and what the District is waiting for.
"""

import datetime as dt
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError

from apps.correspondence import services
from apps.correspondence.models import Correspondence
from apps.correspondence.tests.test_outbound_delivery import (
    OutboundDeliveryServiceTestCase,
)
from apps.records.fees import activate_fee_schedule
from apps.records.models import FeeSchedule, RequestStatus

BASIS = (
    "29,380 electronic files at five cents per four files under the adopted "
    "schedule, plus $2.00 for the transfer media."
)


class FeeEstimateLetterTests(OutboundDeliveryServiceTestCase):
    def adopt_schedule(self):
        return activate_fee_schedule(
            actor=self.officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=dt.date(2026, 1, 2),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )

    def draft(self, **overrides):
        data = {
            "estimated_amount": Decimal("367.25"),
            "estimate_basis": BASIS,
            "deposit_amount": Decimal("36.72"),
            "actor": self.officer,
        }
        data.update(overrides)
        return services.create_fee_estimate_draft(self.prr, **data)

    def test_the_district_cannot_quote_a_charge_it_has_not_adopted(self):
        with self.assertRaisesMessage(ValidationError, "adopted fee schedule"):
            self.draft()

    def test_the_letter_carries_the_amount_the_basis_and_the_deposit(self):
        schedule = self.adopt_schedule()

        draft = self.draft()

        self.assertEqual(draft.kind, Correspondence.Kind.FEE_ESTIMATE)
        self.assertIn("$367.25", draft.draft_body)
        self.assertIn(BASIS, draft.draft_body)
        self.assertIn("$36.72", draft.draft_body)
        self.assertIn("RCW 42.56.120", draft.draft_body)
        # RCW 42.56.120(1) forbids charging for search time, and the letter
        # says so in its own words rather than leaving it implied.
        self.assertIn("locating", draft.draft_body)
        self.assertEqual(
            draft.draft_metadata,
            {
                "estimated_amount": "$367.25",
                "deposit_amount": "$36.72",
                "estimate_basis": BASIS,
                "schedule_id": str(schedule.pk),
            },
        )

    def test_an_estimate_with_no_deposit_renders_no_deposit_paragraph(self):
        self.adopt_schedule()

        draft = self.draft(deposit_amount=None)

        self.assertIn("$367.25", draft.draft_body)
        self.assertNotIn("deposit", draft.draft_body)
        self.assertEqual(draft.draft_metadata["deposit_amount"], "")

    def test_drafting_needs_an_officer_a_basis_and_a_lawful_deposit(self):
        self.adopt_schedule()

        with self.assertRaises(PermissionDenied):
            self.draft(actor=self.integration)
        with self.assertRaisesMessage(ValidationError, "20-2000 character"):
            self.draft(estimate_basis="Too short.")
        with self.assertRaisesMessage(ValidationError, "positive amount"):
            self.draft(estimated_amount=Decimal(0))
        with self.assertRaisesMessage(ValidationError, "ten percent"):
            self.draft(deposit_amount=Decimal("36.73"))

    def test_sending_the_estimate_moves_no_status_and_starts_no_clock(self):
        self.adopt_schedule()
        draft = self.draft()
        before_due = self.prr.statutory_response_due

        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        claim = self.claim(delivery)
        self.report(
            delivery,
            claim,
            provider_message_id=f"gmail-{delivery.id}",
            provider_thread_id=f"thread-{delivery.id}",
            provider_timestamp=self.now + dt.timedelta(seconds=5),
            provider_metadata={"http_status": 200},
        )
        approved.refresh_from_db()
        self.prr.refresh_from_db()

        self.assertEqual(approved.status, Correspondence.Status.SENT)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertEqual(self.prr.statutory_response_due, before_due)
        self.assertIsNone(approved.resulting_status_event)

    def test_a_closed_case_takes_no_fee_estimate(self):
        self.adopt_schedule()
        self.prr.status = RequestStatus.FULFILLED
        self.prr.save(update_fields=["status"])

        with self.assertRaisesMessage(ValidationError, "closed request"):
            self.draft()

    def test_an_estimate_drafted_under_a_superseded_schedule_cannot_be_approved(self):
        self.adopt_schedule()
        draft = self.draft()
        activate_fee_schedule(
            actor=self.officer,
            name="PSD corrected PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=dt.date(2026, 2, 2),
            counsel_approval_reference="Legal memo PRA-fees-2026-09-01",
            adoption_reference="Board policy 4040 resolution 2026-09-01",
        )

        with self.assertRaisesMessage(ValidationError, "superseded"):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )
