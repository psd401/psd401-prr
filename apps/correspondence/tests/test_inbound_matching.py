import uuid
from importlib import import_module

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


def inbound_domain():
    """Load the not-yet-implemented inbound domain at test execution time."""
    return import_module("apps.correspondence.inbound")


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class InboundMatchingContractTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "inbound-officer",
            "inbound-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "inbound-liaison",
            "inbound-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.admin = User.objects.create_user(
            "inbound-admin",
            "inbound-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.integration = User.objects.create_user(
            "automation-correspondence-inbound",
            password=None,
        )
        self.prr_a = self.make_request("alpha")
        self.prr_b = self.make_request("bravo")
        self.prr_c = self.make_request("charlie")
        self._sequence = 0

    @staticmethod
    def make_request(label):
        return PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name=f"{label.title()} Requester",
                email=f"{label}@example.org",
            ),
            description=f"Records for the {label} request.",
        )

    def make_outbound(
        self,
        prr,
        *,
        kind=Correspondence.Kind.GENERAL,
        provider_account="records-inbox",
        provider_thread_id="",
        reply_token=None,
    ):
        self._sequence += 1
        now = timezone.now()
        values = {
            "request": prr,
            "direction": Correspondence.Direction.OUTBOUND,
            "kind": kind,
            "status": Correspondence.Status.SENT,
            "draft_subject": f"Outbound {self._sequence}",
            "draft_body": "Outbound body",
            "approved_subject": f"Outbound {self._sequence}",
            "approved_body": "Outbound body",
            "created_by": self.officer,
            "approved_by": self.officer,
            "queued_by": self.officer,
            "approved_at": now,
            "queued_at": now,
            "sent_at": now,
            "provider": "gmail",
            "provider_account": provider_account,
            "provider_message_id": f"gmail-outbound-{self._sequence}",
            "provider_thread_id": provider_thread_id,
        }
        if reply_token is not None:
            values["reply_token"] = reply_token
        return Correspondence.objects.create(**values)

    def make_receipt(
        self,
        *,
        subject="Unrelated message",
        body="Inbound requester message body.",
        envelope_recipients=None,
        in_reply_to="",
        references=None,
        provider_account="records-inbox",
        provider_thread_id="",
        received_at=None,
    ):
        self._sequence += 1
        received_at = received_at or timezone.now()
        correspondence = Correspondence.objects.create(
            request=None,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject=subject,
            received_body=body,
            received_at=received_at,
            provider="gmail",
            provider_account=provider_account,
            provider_message_id=f"gmail-inbound-correspondence-{self._sequence}",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account=provider_account,
            provider_message_id=f"gmail-inbound-receipt-{self._sequence}",
            provider_thread_id=provider_thread_id,
            in_reply_to=in_reply_to,
            references=references or [],
            received_at=received_at,
            envelope_sender="requester@example.org",
            envelope_recipients=(
                envelope_recipients
                if envelope_recipients is not None
                else ["records@psd401.net"]
            ),
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
            ),
            matched_correspondence=correspondence,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256=f"{self._sequence:064x}",
            submitted_by=self.integration,
            idempotency_key=f"gmail:inbound:{self._sequence}",
            payload_sha256=f"{self._sequence + 1000:064x}",
            completed_at=received_at,
        )
        return receipt, correspondence

    @staticmethod
    def reply_address(outbound):
        return (
            f"{runtime.get('PRR_CORRESPONDENCE_REPLY_LOCAL_PART')}"
            f"+{outbound.reply_token}"
            f"@{runtime.get('PRR_CORRESPONDENCE_REPLY_DOMAIN')}"
        )

    def automatically_match(self, receipt):
        return inbound_domain().match_inbound_evidence(receipt)

    def test_receipt_lock_query_does_not_join_nullable_relations(self):
        receipt, _ = self.make_receipt()

        with (
            transaction.atomic(),
            CaptureQueriesContext(connection) as queries,
        ):
            inbound_domain()._locked_receipt(receipt)

        receipt_queries = [
            query["sql"]
            for query in queries.captured_queries
            if "correspondence_inboundcorrespondencereceipt" in query["sql"]
        ]
        self.assertEqual(len(receipt_queries), 1)
        self.assertNotIn(" JOIN ", receipt_queries[0].upper())

    def assert_matched(self, receipt, correspondence, prr, method):
        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        )
        self.assertEqual(receipt.match_method, method)
        self.assertEqual(receipt.matched_request, prr)
        self.assertEqual(receipt.matched_correspondence, correspondence)
        self.assertIsNotNone(receipt.matched_at)
        self.assertEqual(correspondence.request, prr)
        self.assertEqual(
            correspondence.status,
            Correspondence.Status.RECEIVED,
        )
        self.assertEqual(correspondence.matched_at, receipt.matched_at)

    def test_reply_token_has_priority_over_every_lower_signal(self):
        token_target = self.make_outbound(
            self.prr_a,
            reply_token=uuid.UUID("10000000-0000-4000-8000-000000000001"),
        )
        in_reply_to_target = self.make_outbound(self.prr_b)
        thread_target = self.make_outbound(
            self.prr_c,
            provider_thread_id="conflicting-thread",
        )
        receipt, correspondence = self.make_receipt(
            subject=f"Re: {self.prr_b.number}",
            envelope_recipients=[self.reply_address(token_target)],
            in_reply_to=in_reply_to_target.rfc_message_id,
            references=[thread_target.rfc_message_id],
            provider_thread_id="conflicting-thread",
        )

        self.automatically_match(receipt)

        self.assert_matched(
            receipt,
            correspondence,
            self.prr_a,
            InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
        )

    def test_submission_receipt_reply_token_matches_originating_request(self):
        outbound = self.make_outbound(
            self.prr_a,
            kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        receipt, correspondence = self.make_receipt(
            envelope_recipients=[self.reply_address(outbound)],
        )

        self.automatically_match(receipt)

        self.assert_matched(
            receipt,
            correspondence,
            self.prr_a,
            InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
        )

    def test_in_reply_to_has_priority_over_thread_references_and_subject(self):
        in_reply_to_target = self.make_outbound(self.prr_a)
        _thread_target = self.make_outbound(
            self.prr_b,
            provider_thread_id="lower-priority-thread",
        )
        references_target = self.make_outbound(self.prr_c)
        receipt, correspondence = self.make_receipt(
            subject=f"Re: {self.prr_b.number}",
            in_reply_to=in_reply_to_target.rfc_message_id,
            provider_thread_id="lower-priority-thread",
            references=[references_target.rfc_message_id],
        )

        self.automatically_match(receipt)

        self.assert_matched(
            receipt,
            correspondence,
            self.prr_a,
            InboundCorrespondenceReceipt.MatchMethod.IN_REPLY_TO,
        )

    def test_provider_thread_match_is_scoped_to_provider_account(self):
        expected = self.make_outbound(
            self.prr_a,
            provider_account="records-inbox",
            provider_thread_id="shared-provider-thread",
        )
        self.make_outbound(
            self.prr_b,
            provider_account="secondary-inbox",
            provider_thread_id="shared-provider-thread",
        )
        receipt, correspondence = self.make_receipt(
            provider_account=expected.provider_account,
            provider_thread_id="shared-provider-thread",
        )

        self.automatically_match(receipt)

        self.assert_matched(
            receipt,
            correspondence,
            self.prr_a,
            InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        )

    def test_references_match_precedes_a_subject_candidate(self):
        references_target = self.make_outbound(self.prr_a)
        second_reference_for_same_request = self.make_outbound(self.prr_a)
        receipt, correspondence = self.make_receipt(
            subject=f"Re: {self.prr_b.number}",
            references=[
                references_target.rfc_message_id,
                second_reference_for_same_request.rfc_message_id,
            ],
        )

        self.automatically_match(receipt)

        self.assert_matched(
            receipt,
            correspondence,
            self.prr_a,
            InboundCorrespondenceReceipt.MatchMethod.REFERENCES,
        )

    def test_unique_subject_request_number_is_only_a_staff_candidate(self):
        receipt, correspondence = self.make_receipt(
            subject=f"Additional details for {self.prr_a.number}",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        )
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
        )
        self.assertEqual(receipt.candidate_request, self.prr_a)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(receipt.matched_at)
        self.assertIsNone(correspondence.request_id)
        self.assertEqual(
            correspondence.status,
            Correspondence.Status.UNMATCHED,
        )

    def test_unique_body_request_number_is_only_a_staff_candidate(self):
        # A follow-up from a fresh thread: no token, no headers, and the
        # number the requester typed sits in the body, not the subject.
        receipt, correspondence = self.make_receipt(
            subject="Records request",
            body=(
                "Good morning, I submitted a records request "
                f"({self.prr_a.number}) on Monday and have not received an "
                "acknowledgement."
            ),
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        )
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.BODY,
        )
        self.assertEqual(receipt.candidate_request, self.prr_a)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(correspondence.request_id)
        self.assertEqual(
            correspondence.status,
            Correspondence.Status.UNMATCHED,
        )

    def test_subject_request_number_outranks_the_body(self):
        receipt, _ = self.make_receipt(
            subject=f"Re: {self.prr_a.number}",
            body=f"See also my other request {self.prr_b.number}.",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        )
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
        )
        self.assertEqual(receipt.candidate_request, self.prr_a)

    def test_multiple_body_request_numbers_are_ambiguous(self):
        receipt, correspondence = self.make_receipt(
            subject="Records request",
            body=(
                f"This concerns {self.prr_a.number}.\n\n> Quoted footer "
                f"about {self.prr_b.number}"
            ),
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
        )
        self.assertIsNone(receipt.candidate_request_id)
        self.assertIsNone(correspondence.request_id)

    def test_body_request_number_of_a_merged_duplicate_names_the_case_of_record(self):
        self.prr_b.merged_into = self.prr_a
        self.prr_b.save(update_fields=["merged_into"])
        receipt, _ = self.make_receipt(
            subject="Following up",
            body=f"Any update on {self.prr_b.number}?",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        )
        self.assertEqual(receipt.candidate_request, self.prr_a)

    def test_multiple_subject_request_numbers_are_ambiguous(self):
        receipt, correspondence = self.make_receipt(
            subject=(
                f"Possible replies for {self.prr_a.number} "
                f"and {self.prr_b.number}"
            ),
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
        )
        self.assertIsNone(receipt.candidate_request_id)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(correspondence.request_id)
        self.assertEqual(
            correspondence.status,
            Correspondence.Status.UNMATCHED,
        )

    def test_message_without_matching_evidence_remains_in_unmatched_queue(self):
        receipt, correspondence = self.make_receipt(
            subject="No request reference",
            in_reply_to="<unknown-message@example.org>",
            references=["<also-unknown@example.org>"],
            provider_thread_id="unknown-provider-thread",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        self.assertEqual(receipt.match_method, "")
        self.assertIsNone(receipt.candidate_request_id)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(correspondence.request_id)

    def test_ambiguous_strong_tier_stops_before_lower_priority_subject(self):
        first = self.make_outbound(self.prr_a)
        second = self.make_outbound(self.prr_b)
        receipt, correspondence = self.make_receipt(
            subject=f"Re: {self.prr_c.number}",
            in_reply_to=f"{first.rfc_message_id} {second.rfc_message_id}",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
        )
        self.assertIsNone(receipt.candidate_request_id)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(correspondence.request_id)

    def test_manual_match_requires_officer_reason_and_current_version(self):
        receipt, correspondence = self.make_receipt(
            subject=f"Additional details for {self.prr_a.number}",
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        starting_version = receipt.version
        domain = inbound_domain()
        reason = "The message headers and case history were reviewed."

        for unauthorized_actor in (self.liaison, self.admin):
            with self.subTest(actor=unauthorized_actor.username):
                with self.assertRaises(PermissionDenied):
                    domain.match_inbound_receipt(
                        receipt,
                        request=self.prr_a,
                        actor=unauthorized_actor,
                        reason=reason,
                        expected_version=starting_version,
                    )

        with self.assertRaises(ValidationError):
            domain.match_inbound_receipt(
                receipt,
                request=self.prr_a,
                actor=self.officer,
                reason="too short",
                expected_version=starting_version,
            )
        with self.assertRaises(ValidationError):
            domain.match_inbound_receipt(
                receipt,
                request=self.prr_a,
                actor=self.officer,
                reason=reason,
                expected_version=starting_version - 1,
            )

        domain.match_inbound_receipt(
            receipt,
            request=self.prr_a,
            actor=self.officer,
            reason=reason,
            expected_version=starting_version,
        )

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(receipt.version, starting_version + 1)
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.MANUAL,
        )
        self.assertEqual(receipt.matched_by, self.officer)
        self.assertEqual(correspondence.request, self.prr_a)
        event = AuditLog.objects.get(action="correspondence.inbound_matched")
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.target, self.prr_a.number)
        self.assertEqual(event.detail["receipt_id"], str(receipt.id))
        self.assertIsNone(event.detail["previous_request"])
        self.assertEqual(event.detail["new_request"], self.prr_a.number)
        self.assertEqual(event.detail["reason"], reason)

    def test_stale_deactivated_officer_cannot_manually_match_receipt(self):
        receipt, correspondence = self.make_receipt(
            subject=f"Additional details for {self.prr_a.number}",
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        starting_version = receipt.version
        get_user_model().objects.filter(pk=self.officer.pk).update(
            is_active=False
        )

        with self.assertRaisesMessage(
            PermissionDenied,
            "Records Officer approval is required.",
        ):
            inbound_domain().match_inbound_receipt(
                receipt,
                request=self.prr_a,
                actor=self.officer,
                reason="The message headers and case history were reviewed.",
                expected_version=starting_version,
            )

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(receipt.version, starting_version)
        self.assertIsNone(receipt.matched_request_id)
        self.assertIsNone(receipt.matched_by_id)
        self.assertIsNone(correspondence.request_id)
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.inbound_matched",
                detail__receipt_id=str(receipt.pk),
            ).exists()
        )

    def test_rematch_moves_only_association_and_audits_old_and_new_request(self):
        outbound = self.make_outbound(self.prr_a)
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
        )
        original_subject = correspondence.received_subject
        original_body = correspondence.received_body
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        starting_version = receipt.version
        reason = "The original match used a forwarded header from another case."

        inbound_domain().rematch_inbound_receipt(
            receipt,
            request=self.prr_b,
            actor=self.officer,
            reason=reason,
            expected_version=starting_version,
        )

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(receipt.version, starting_version + 1)
        self.assertEqual(receipt.matched_request, self.prr_b)
        self.assertEqual(receipt.matched_by, self.officer)
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.MANUAL,
        )
        self.assertEqual(correspondence.request, self.prr_b)
        self.assertEqual(correspondence.received_subject, original_subject)
        self.assertEqual(correspondence.received_body, original_body)
        event = AuditLog.objects.get(action="correspondence.inbound_rematched")
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.target, self.prr_b.number)
        self.assertEqual(event.detail["previous_request"], self.prr_a.number)
        self.assertEqual(event.detail["new_request"], self.prr_b.number)
        self.assertEqual(event.detail["reason"], reason)

    def open_clarification_cycle(self):
        outbound = self.make_outbound(
            self.prr_a,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            provider_thread_id="clarification-thread",
        )
        PublicRecordsRequest.objects.filter(pk=self.prr_a.pk).update(
            status=RequestStatus.CLARIFICATION,
        )
        self.prr_a.refresh_from_db()
        opened_event = StatusEvent.objects.create(
            request=self.prr_a,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered for inbound matching test.",
            visible_to_requester=True,
            occurred_at=outbound.sent_at,
        )
        cycle = ClarificationCycle.objects.create(
            request=self.prr_a,
            outbound_correspondence=outbound,
            opened_at=outbound.sent_at,
            opened_status_event=opened_event,
        )
        return outbound, cycle

    def test_strong_clarification_evidence_associates_without_resolving(self):
        outbound, cycle = self.open_clarification_cycle()
        response_received_at = timezone.now()
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
            received_at=response_received_at,
        )
        status_events_before = StatusEvent.objects.filter(
            request=self.prr_a
        ).count()

        self.automatically_match(receipt)

        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.prr_a.refresh_from_db()
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        self.assertEqual(
            cycle.inbound_response_correspondence,
            correspondence,
        )
        self.assertEqual(cycle.responded_at, response_received_at)
        self.assertIsNone(cycle.resolved_at)
        self.assertIsNone(cycle.resolved_by_id)
        self.assertEqual(cycle.resolution_notes, "")
        self.assertEqual(self.prr_a.status, RequestStatus.CLARIFICATION)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr_a).count(),
            status_events_before,
        )

    def test_pre_cycle_message_matches_request_without_becoming_a_clarification_response(
        self,
    ):
        outbound, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
            received_at=cycle.opened_at - timezone.timedelta(seconds=1),
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(receipt.matched_request, self.prr_a)
        self.assertEqual(correspondence.kind, Correspondence.Kind.GENERAL)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)

    def test_future_dated_message_matches_request_without_becoming_a_clarification_response(
        self,
    ):
        outbound, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
            received_at=timezone.now() + timezone.timedelta(days=1),
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(receipt.matched_request, self.prr_a)
        self.assertEqual(correspondence.kind, Correspondence.Kind.GENERAL)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)

    def test_subject_confirmation_does_not_infer_clarification_response(self):
        _, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            subject=f"Requested details for {self.prr_a.number}",
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()

        inbound_domain().match_inbound_receipt(
            receipt,
            request=self.prr_a,
            actor=self.officer,
            reason="The subject and sender were reviewed against the case.",
            expected_version=receipt.version,
        )

        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.GENERAL,
        )
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)
        self.assertIsNone(cycle.resolved_at)

    def test_officer_can_explicitly_associate_a_manually_matched_reply_with_cycle(
        self,
    ):
        _, cycle = self.open_clarification_cycle()
        response_received_at = timezone.now()
        receipt, correspondence = self.make_receipt(
            subject=f"Requested details for {self.prr_a.number}",
            received_at=response_received_at,
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        inbound_domain().match_inbound_receipt(
            receipt,
            request=self.prr_a,
            actor=self.officer,
            reason="The sender and subject identify the correct request.",
            expected_version=receipt.version,
        )
        receipt.refresh_from_db()
        starting_version = receipt.version
        status_events_before = StatusEvent.objects.filter(
            request=self.prr_a,
        ).count()
        reason = (
            "The message answers the open clarification despite missing "
            "reply headers."
        )

        associated = inbound_domain().associate_clarification_response(
            receipt,
            clarification_cycle=cycle,
            actor=self.officer,
            reason=reason,
            expected_version=starting_version,
            ip_address="192.0.2.42",
        )

        associated.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.prr_a.refresh_from_db()
        self.assertEqual(associated.version, starting_version + 1)
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        self.assertEqual(
            cycle.inbound_response_correspondence,
            correspondence,
        )
        self.assertEqual(cycle.responded_at, response_received_at)
        self.assertIsNone(cycle.resolved_at)
        self.assertEqual(self.prr_a.status, RequestStatus.CLARIFICATION)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr_a).count(),
            status_events_before,
        )
        event = AuditLog.objects.get(
            action="correspondence.clarification_response_associated",
        )
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.target, self.prr_a.number)
        self.assertEqual(event.detail["receipt_id"], str(receipt.pk))
        self.assertEqual(
            event.detail["clarification_cycle_id"],
            str(cycle.pk),
        )
        self.assertEqual(event.detail["reason"], reason)
        self.assertEqual(str(event.ip_address), "192.0.2.42")

    def test_clarification_association_requires_current_officer_and_current_version(
        self,
    ):
        _, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            subject=f"Requested details for {self.prr_a.number}",
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        inbound_domain().match_inbound_receipt(
            receipt,
            request=self.prr_a,
            actor=self.officer,
            reason="The sender and subject identify the correct request.",
            expected_version=receipt.version,
        )
        receipt.refresh_from_db()
        starting_version = receipt.version

        for actor in (None, self.integration, self.liaison, self.admin):
            with self.subTest(actor=getattr(actor, "username", None)):
                with self.assertRaises(PermissionDenied):
                    inbound_domain().associate_clarification_response(
                        receipt,
                        clarification_cycle=cycle,
                        actor=actor,
                        reason=(
                            "The retained message answers the open "
                            "clarification."
                        ),
                        expected_version=starting_version,
                    )

        with self.assertRaises(correspondence_services.VersionConflict):
            inbound_domain().associate_clarification_response(
                receipt,
                clarification_cycle=cycle,
                actor=self.officer,
                reason="The retained message answers the open clarification.",
                expected_version=starting_version - 1,
            )

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(receipt.version, starting_version)
        self.assertEqual(correspondence.kind, Correspondence.Kind.GENERAL)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)

    def test_shared_thread_is_not_an_exact_clarification_association(self):
        _, cycle = self.open_clarification_cycle()
        self.make_outbound(
            self.prr_a,
            provider_thread_id="clarification-thread",
        )
        receipt, correspondence = self.make_receipt(
            provider_thread_id="clarification-thread",
        )

        self.automatically_match(receipt)

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(receipt.matched_request, self.prr_a)
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        )
        self.assertEqual(correspondence.kind, Correspondence.Kind.GENERAL)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)

    def test_rematch_clears_an_unresolved_clarification_association(self):
        outbound, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()

        inbound_domain().rematch_inbound_receipt(
            receipt,
            request=self.prr_b,
            actor=self.officer,
            reason="The requester confirmed that the reply belongs to another case.",
            expected_version=receipt.version,
        )

        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(correspondence.request, self.prr_b)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)
        self.assertIsNone(cycle.resolved_at)
        event = AuditLog.objects.get(action="correspondence.inbound_rematched")
        self.assertEqual(
            event.detail["previous_kind"],
            Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        self.assertEqual(
            event.detail["previous_clarification_cycle_id"],
            str(cycle.id),
        )
        self.assertEqual(
            event.detail["previous_responded_at"],
            receipt.received_at.isoformat(),
        )

    def test_rematch_rejects_a_resolved_clarification_cycle(self):
        outbound, cycle = self.open_clarification_cycle()
        receipt, correspondence = self.make_receipt(
            in_reply_to=outbound.rfc_message_id,
        )
        self.automatically_match(receipt)
        receipt.refresh_from_db()
        cycle.refresh_from_db()
        cycle.resolved_at = timezone.now()
        cycle.resolved_by = self.officer
        cycle.resolution_notes = (
            "The Records Officer resolved the clarified scope."
        )
        cycle.save()

        with self.assertRaises(ValidationError):
            inbound_domain().rematch_inbound_receipt(
                receipt,
                request=self.prr_b,
                actor=self.officer,
                reason="Later review suggested that the reply belongs elsewhere.",
                expected_version=receipt.version,
            )

        receipt.refresh_from_db()
        correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(receipt.matched_request, self.prr_a)
        self.assertEqual(correspondence.request, self.prr_a)
        self.assertEqual(
            cycle.inbound_response_correspondence,
            correspondence,
        )
        self.assertIsNotNone(cycle.resolved_at)
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.inbound_rematched",
                detail__receipt_id=str(receipt.id),
            ).exists()
        )
