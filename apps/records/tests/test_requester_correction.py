"""Correcting who the record says asked, when intake recorded a forwarder.

A request that arrives by forward -- the district info mailbox relaying an
email, a clerk typing up a phone call -- lands with the forwarder's contact
details on the legal record. Every statutory notice then addresses the wrong
person, and the RCW 42.56.520 five-day response is strict liability. So the
identity is correctable, by a named Records Officer, in writing, on the
record -- and nothing already sent, and no statutory timestamp, is rewritten
to make the timeline look tidier than it was.
"""

import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceRecipient,
)
from apps.records import services
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)

FORWARDED_REASON = (
    "Submitted by the district info mailbox on behalf of the actual "
    "requester, who is named in the forwarded email."
)


class RequesterCorrectionTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, [StaffRole.LIAISON])
        self.requester = Requester.objects.create(
            name="District Info Mailbox",
            email="info@psd401.net",
            phone="253-555-0100",
            mailing_address="14015 62nd Ave NW",
            city="Gig Harbor",
            state="WA",
            postal_code="98332",
            company="Peninsula School District",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="All emails about the stadium project",
            estimated_completion=timezone.localdate() + dt.timedelta(days=30),
        )

    def _correct(self, **overrides):
        payload = {
            "actor": self.officer,
            "name": "Jane Rivera",
            "email": "jane@example.org",
            "reason": FORWARDED_REASON,
        }
        payload.update(overrides)
        return services.correct_requester_identity(self.prr, **payload)

    def _acknowledgment_draft(self):
        return correspondence_services.create_acknowledgment_draft(
            self.prr,
            actor=self.officer,
        )

    # --- authorization and input ---------------------------------------

    def test_an_officer_corrects_the_identity(self):
        correction = self._correct()
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "jane@example.org")
        self.assertEqual(self.prr.requester.name, "Jane Rivera")
        self.assertEqual(correction.previous_requester.pk, self.requester.pk)

    def test_a_liaison_may_not_correct_the_identity(self):
        with self.assertRaises(PermissionDenied):
            self._correct(actor=self.liaison)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "info@psd401.net")

    def test_an_unauthenticated_actor_may_not_correct_the_identity(self):
        with self.assertRaises(PermissionDenied):
            self._correct(actor=None)

    def test_a_non_staff_agent_identity_may_not_correct_the_identity(self):
        agent = get_user_model().objects.create_user(
            "mcp-nonstaff", "mcp@psd401.net", "pw", is_staff=False
        )
        with self.assertRaises(PermissionDenied):
            self._correct(actor=agent)

    def test_a_short_reason_is_refused(self):
        with self.assertRaises(ValidationError):
            self._correct(reason="typo")

    def test_a_malformed_email_is_refused(self):
        with self.assertRaises(ValidationError):
            self._correct(email="not-an-address")
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "info@psd401.net")

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(ValidationError):
            self._correct(name="   ")

    def test_an_identical_identity_is_refused(self):
        with self.assertRaises(ValidationError):
            self._correct(
                name="District Info Mailbox",
                email="INFO@psd401.net",
            )

    # --- what the correction preserves ---------------------------------

    def test_the_submitted_requester_row_is_left_untouched(self):
        self._correct()
        self.requester.refresh_from_db()
        self.assertEqual(self.requester.email, "info@psd401.net")
        self.assertEqual(self.requester.name, "District Info Mailbox")

    def test_the_legal_description_is_untouched(self):
        self._correct()
        self.prr.refresh_from_db()
        self.assertEqual(
            self.prr.description,
            "All emails about the stadium project",
        )

    def test_the_remaining_contact_record_carries_forward(self):
        correction = self._correct()
        self.assertEqual(correction.requester.phone, "253-555-0100")
        self.assertEqual(correction.requester.city, "Gig Harbor")
        self.assertEqual(correction.requester.postal_code, "98332")

    def test_the_correction_is_audited_with_before_and_after(self):
        self._correct()
        entry = AuditLog.objects.get(action="request.requester_corrected")
        self.assertEqual(entry.actor, self.officer)
        self.assertEqual(entry.target, self.prr.number)
        self.assertEqual(entry.detail["reason"], FORWARDED_REASON)
        self.assertEqual(entry.detail["previous_email"], "info@psd401.net")
        self.assertEqual(entry.detail["corrected_email"], "jane@example.org")
        self.assertEqual(
            entry.detail["previous_name"], "District Info Mailbox"
        )
        self.assertEqual(entry.detail["corrected_name"], "Jane Rivera")

    # --- what it does to the open acknowledgment draft ------------------

    def test_the_open_draft_greets_the_corrected_requester(self):
        draft = self._acknowledgment_draft()
        self.assertIn("Dear District Info Mailbox,", draft.draft_body)

        correction = self._correct()

        draft.refresh_from_db()
        self.assertIn("Dear Jane Rivera,", draft.draft_body)
        self.assertNotIn("District Info Mailbox", draft.draft_body)
        self.assertEqual(
            [item.pk for item in correction.refresh.refreshed],
            [draft.pk],
        )

    def test_the_open_draft_is_addressed_to_the_corrected_email(self):
        draft = self._acknowledgment_draft()
        self._correct()

        recipients = list(draft.recipients.all())
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].email, "jane@example.org")
        self.assertEqual(recipients[0].display_name, "Jane Rivera")
        self.prr.refresh_from_db()
        self.assertEqual(recipients[0].requester_id, self.prr.requester_id)

    def test_the_refreshed_draft_can_be_approved_and_sent(self):
        draft = self._acknowledgment_draft()
        self._correct()
        draft.refresh_from_db()

        delivery = correspondence_services.approve_and_send(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.QUEUED)
        self.assertIn("Dear Jane Rivera,", draft.approved_body)
        self.assertEqual(delivery.correspondence_id, draft.pk)

    def test_a_draft_edit_is_audited(self):
        draft = self._acknowledgment_draft()
        self._correct()
        entry = AuditLog.objects.get(
            action="correspondence.requester_corrected"
        )
        self.assertEqual(entry.detail["correspondence_id"], str(draft.id))
        self.assertTrue(entry.detail["content_refreshed"])
        # The identities are recorded once, on the request-level entry;
        # the permanent per-draft rows carry no requester PII.
        for key in (
            "previous_email", "previous_name",
            "corrected_email", "corrected_name",
        ):
            self.assertNotIn(key, entry.detail)

    def test_an_authored_draft_is_repointed_but_flagged_for_a_human_read(self):
        """This service will not rewrite the officer's own wording.

        A general message may name the requester anywhere in its body, and
        there is no template to re-render it from. So the To line is
        corrected, the content is left exactly as written, and the draft is
        reported back as one the officer must re-read before sending.
        """
        draft = correspondence_services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="About your request",
            body=(
                "District Info Mailbox, we are still gathering the emails "
                "you asked for."
            ),
            actor=self.officer,
        )

        correction = self._correct()

        draft.refresh_from_db()
        [(flagged, detail)] = correction.refresh.needs_review
        self.assertEqual(flagged.pk, draft.pk)
        self.assertIn("authored by hand", detail)
        self.assertEqual(correction.refresh.refreshed, ())
        # The To line is corrected; the officer's wording is not.
        self.assertEqual(
            [item.email for item in draft.recipients.all()],
            ["jane@example.org"],
        )
        self.assertIn("District Info Mailbox, we are still", draft.draft_body)
        entry = AuditLog.objects.get(
            action="correspondence.requester_corrected",
            detail__correspondence_id=str(draft.id),
        )
        self.assertFalse(entry.detail["content_refreshed"])

    def test_a_draft_whose_inputs_no_longer_validate_is_flagged_with_why(self):
        """A template draft that can no longer be re-rendered is left alone.

        Approval re-renders and refuses the mismatch, so the correction
        fails closed here -- but it says why, rather than leaving the
        officer to guess what "re-read before sending" means.
        """
        draft = self._acknowledgment_draft()
        Correspondence.objects.filter(pk=draft.pk).update(draft_metadata={})

        correction = self._correct()

        draft.refresh_from_db()
        self.assertEqual(correction.refresh.refreshed, ())
        [(flagged, detail)] = correction.refresh.needs_review
        self.assertEqual(flagged.pk, draft.pk)
        self.assertIn("does not match the versioned contract", detail)
        self.assertIn("Dear District Info Mailbox,", draft.draft_body)
        self.assertEqual(
            [item.email for item in draft.recipients.all()],
            ["jane@example.org"],
        )
        entry = AuditLog.objects.get(
            action="correspondence.requester_corrected",
            detail__correspondence_id=str(draft.id),
        )
        self.assertFalse(entry.detail["content_refreshed"])
        self.assertIn(
            "does not match the versioned contract", entry.detail["detail"]
        )

    # --- what it does to already-approved and already-sent messages -----

    def test_an_approved_but_unsent_message_is_cancelled(self):
        draft = self._acknowledgment_draft()
        correspondence_services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        draft.refresh_from_db()

        correction = self._correct()

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.CANCELLED)
        self.assertEqual(
            [item.pk for item in correction.refresh.cancelled],
            [draft.pk],
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.cancelled",
                detail__correspondence_id=str(draft.id),
            ).exists()
        )

    def test_a_message_already_in_delivery_is_reported_not_recalled(self):
        """A worker holding the delivery lease cannot be interrupted.

        Refusing the whole correction would not stop that send, so the
        record is still corrected and the officer is told, on the record,
        which message is on its way to the wrong address.
        """
        draft = self._acknowledgment_draft()
        correspondence_services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        draft.refresh_from_db()
        delivery = correspondence_services.queue_approved(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            status=CorrespondenceDelivery.Status.CLAIMED,
            claim_owner="worker-1",
            claim_expires_at=timezone.now() + dt.timedelta(minutes=5),
        )

        correction = self._correct()

        draft.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.QUEUED)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correction.refresh.cancelled, ())
        [(reported, detail)] = correction.refresh.uncancelled
        self.assertEqual(reported.pk, draft.pk)
        self.assertIn("cannot be cancelled", detail)
        entry = AuditLog.objects.get(
            action="correspondence.requester_correction_cancel_refused",
            detail__correspondence_id=str(draft.id),
        )
        self.assertIn("cannot be cancelled", entry.detail["detail"])
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.requester.email, "jane@example.org")

    def test_the_approved_snapshot_of_a_cancelled_message_is_unchanged(self):
        draft = self._acknowledgment_draft()
        correspondence_services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        draft.refresh_from_db()
        frozen_body = draft.approved_body

        self._correct()

        draft.refresh_from_db()
        self.assertEqual(draft.approved_body, frozen_body)
        self.assertIn("Dear District Info Mailbox,", draft.approved_body)

    def test_a_correction_after_a_sent_acknowledgment_keeps_the_timestamp(self):
        sent_at = timezone.now() - dt.timedelta(days=1)
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            acknowledgment_sent_at=sent_at,
            status=RequestStatus.ACKNOWLEDGED,
        )
        self.prr.refresh_from_db()

        self._correct()

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.acknowledgment_sent_at, sent_at)
        self.assertTrue(self.prr.initial_response_delivered)
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)

    def test_a_cc_of_the_old_address_is_kept_not_replaced(self):
        """Copying the forwarding mailbox is a choice, not the requester slot.

        Officers routinely keep the mailbox that relayed a request on the
        thread. Treating that CC as "addressed to the old requester" would
        quietly drop it, and would make the message look like it needed
        cancelling when its To line was already right.
        """
        draft = self._acknowledgment_draft()
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.CC,
            email="info@psd401.net",
            display_name="District Info Mailbox",
        )

        self._correct()

        recipients = {
            (item.recipient_type, item.email)
            for item in draft.recipients.all()
        }
        self.assertEqual(
            recipients,
            {
                (CorrespondenceRecipient.RecipientType.TO, "jane@example.org"),
                (CorrespondenceRecipient.RecipientType.CC, "info@psd401.net"),
            },
        )

    def test_a_message_only_copying_the_old_address_is_left_alone(self):
        """Its To line is already right, so there is nothing to correct.

        Treating the CC as the requester slot would have rewritten a correct
        message -- and, once approved, cancelled it outright.
        """
        # A message whose To line is already the real requester, which merely
        # keeps the forwarding mailbox copied.
        draft = self._acknowledgment_draft()
        draft.recipients.all().delete()
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="jane@example.org",
            display_name="Jane Rivera",
        )
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.CC,
            email="info@psd401.net",
            display_name="District Info Mailbox",
        )

        correction = self._correct()

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(correction.refresh.cancelled, ())
        self.assertEqual(correction.refresh.refreshed, ())
        self.assertEqual(correction.refresh.needs_review, ())

    def test_a_copied_recipient_is_preserved(self):
        draft = self._acknowledgment_draft()
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.CC,
            email="attorney@example.org",
            display_name="Counsel",
        )

        self._correct()

        emails = sorted(item.email for item in draft.recipients.all())
        self.assertEqual(emails, ["attorney@example.org", "jane@example.org"])
