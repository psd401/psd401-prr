"""Completion, non-payment, and third-party-notice letters.

Three letters the district previously wrote by hand in a free-form "general"
message, which meant a closure's basis lived in prose nothing could read back.
Each is now structured, so the case records why it closed and the closure only
happens when a provider confirms the letter actually went out.
"""

import datetime as dt

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceRecipient,
)
from apps.correspondence.tests.test_outbound_delivery import (
    OutboundDeliveryServiceTestCase,
)
from apps.records.deadlines import add_business_days, business_days_between
from apps.records.models import RequestStatus


class Phase4LetterTestCase(OutboundDeliveryServiceTestCase):
    """Deliver a draft the way the real pipeline does and read the effect."""

    def deliver(self, draft):
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
        return approved


SUMMARY = (
    "The District produced 214 pages across three installments and withheld "
    "nothing."
)


class CompletionClosureTests(Phase4LetterTestCase):
    def test_delivery_closes_the_case_as_fulfilled_and_records_why(self):
        draft = services.create_completion_closure_draft(
            self.prr,
            production_summary=SUMMARY,
            actor=self.officer,
        )
        self.assertEqual(draft.kind, Correspondence.Kind.COMPLETION_CLOSURE)
        self.assertIn(SUMMARY, draft.draft_body)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

        sent = self.deliver(draft)

        self.assertEqual(self.prr.status, RequestStatus.FULFILLED)
        self.assertEqual(
            sent.resulting_status_event.to_status,
            RequestStatus.FULFILLED,
        )
        self.assertIn(SUMMARY, sent.resulting_status_event.note)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.closed_complete",
                target=self.prr.number,
                detail__correspondence_id=str(sent.pk),
            ).exists()
        )

    def test_drafting_requires_an_officer_a_summary_and_an_open_case(self):
        with self.assertRaises(PermissionDenied):
            services.create_completion_closure_draft(
                self.prr,
                production_summary=SUMMARY,
                actor=self.integration,
            )
        with self.assertRaisesMessage(ValidationError, "20-2000 character"):
            services.create_completion_closure_draft(
                self.prr,
                production_summary="Done.",
                actor=self.officer,
            )

        self.deliver(
            services.create_completion_closure_draft(
                self.prr,
                production_summary=SUMMARY,
                actor=self.officer,
            )
        )
        with self.assertRaisesMessage(ValidationError, "closed request"):
            services.create_completion_closure_draft(
                self.prr,
                production_summary=SUMMARY,
                actor=self.officer,
            )


class NonpaymentClosureTests(Phase4LetterTestCase):
    def draft(self, **overrides):
        values = {
            "amount_due": "125.00",
            "payment_requested_on": timezone.localdate(),
            "actor": self.officer,
        }
        values.update(overrides)
        return services.create_nonpayment_closure_draft(self.prr, **values)

    def test_delivery_closes_as_abandoned_with_the_fee_unpaid_reason_code(self):
        draft = self.draft()
        self.assertEqual(
            draft.draft_metadata["reason_code"],
            services.NONPAYMENT_CLOSURE_REASON_CODE,
        )
        self.assertIn("$125.00", draft.draft_body)
        self.assertIn("RCW 42.56.120(2)", draft.draft_body)

        sent = self.deliver(draft)

        self.assertEqual(self.prr.status, RequestStatus.CLOSED_ABANDONED)
        self.assertEqual(
            sent.resulting_status_event.to_status,
            RequestStatus.CLOSED_ABANDONED,
        )
        self.assertIn("fee_unpaid", sent.resulting_status_event.note)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.closed_nonpayment",
                target=self.prr.number,
                detail__reason_code="fee_unpaid",
                detail__amount_due="$125.00",
            ).exists()
        )

    def test_the_amount_and_the_request_date_must_both_be_real(self):
        with self.assertRaisesMessage(ValidationError, "amount"):
            self.draft(amount_due="0")
        with self.assertRaisesMessage(ValidationError, "amount"):
            self.draft(amount_due="not money")
        with self.assertRaisesMessage(ValidationError, "not yet requested"):
            self.draft(
                payment_requested_on=timezone.localdate()
                + dt.timedelta(days=1)
            )
        with self.assertRaisesMessage(ValidationError, "predate"):
            self.draft(
                payment_requested_on=timezone.localtime(
                    self.prr.received_at
                ).date()
                - dt.timedelta(days=1)
            )

    def test_a_dollar_sign_or_thousands_separator_is_accepted_once(self):
        draft = self.draft(amount_due="$1,250.5")
        self.assertIn("$1250.50", draft.draft_body)


class ThirdPartyNoticeTests(Phase4LetterTestCase):
    def draft(self, **overrides):
        values = {
            "employee_name": "Alex Doe",
            "employee_email": "alex.doe@psd401.net",
            "actor": self.officer,
        }
        values.update(overrides)
        return services.create_third_party_notice_draft(self.prr, **values)

    def test_the_notice_goes_to_the_employee_and_never_to_the_requester(self):
        draft = self.draft()

        recipients = list(draft.recipients.all())
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].email, "alex.doe@psd401.net")
        self.assertEqual(
            recipients[0].recipient_type,
            CorrespondenceRecipient.RecipientType.TO,
        )
        self.assertNotIn(self.prr.requester.email, draft.draft_body)
        self.assertIn("RCW 42.56.540", draft.draft_body)
        # The disclosed requester is named, which is the point of the notice.
        self.assertIn(self.prr.requester.name, draft.draft_body)

    def test_a_requester_who_gave_no_name_is_not_identified_by_email(self):
        # An address is contact information, not a name. A third party gets
        # told that a request exists, never how to reach the person who made it.
        self.prr.requester.name = ""
        self.prr.requester.save(update_fields=["name"])

        draft = self.draft()

        self.assertNotIn(self.prr.requester.email, draft.draft_body)
        self.assertEqual(
            draft.draft_metadata["disclosed_requesters"],
            services.UNNAMED_REQUESTER,
        )
        self.assertIn(services.UNNAMED_REQUESTER, draft.draft_body)
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.third_party_notice_drafted",
                detail__disclosed_requesters=self.prr.requester.email,
            ).exists()
        )

    def test_an_attorney_is_copied_when_one_is_given(self):
        draft = self.draft(
            attorney_name="Pat Counsel",
            attorney_email="pat@counsel.example",
        )

        by_type = {
            recipient.recipient_type: recipient
            for recipient in draft.recipients.all()
        }
        self.assertEqual(
            by_type[CorrespondenceRecipient.RecipientType.CC].email,
            "pat@counsel.example",
        )
        with self.assertRaisesMessage(ValidationError, "an address"):
            self.draft(attorney_name="Pat Counsel")

    def test_the_response_by_date_is_ten_business_days_out(self):
        draft = self.draft()

        response_by = dt.date.fromisoformat(
            draft.draft_metadata["response_by"]
        )
        self.assertEqual(
            business_days_between(timezone.localdate(), response_by),
            services.THIRD_PARTY_NOTICE_RESPONSE_BUSINESS_DAYS,
        )
        self.assertEqual(
            response_by,
            add_business_days(
                timezone.localdate(),
                services.THIRD_PARTY_NOTICE_RESPONSE_BUSINESS_DAYS,
            ),
        )

    def test_approval_refuses_a_notice_addressed_to_the_requester(self):
        draft = self.draft()
        draft.recipients.all().delete()
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email=self.prr.requester.email,
            display_name="Jane Requester",
        )

        with self.assertRaisesMessage(ValidationError, "never to the requester"):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

    def test_delivery_leaves_the_request_status_alone(self):
        sent = self.deliver(self.draft())

        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(sent.resulting_status_event)
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.third_party_notice_drafted",
                target=self.prr.number,
                detail__notified_person="Alex Doe",
            ).exists()
        )

    def test_only_a_records_officer_may_notify_a_third_party(self):
        with self.assertRaises(PermissionDenied):
            self.draft(actor=self.integration)
