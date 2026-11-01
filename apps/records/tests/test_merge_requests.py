# ABOUTME: Merging a duplicate case into the case of record, on the record.
# ABOUTME: Covers moved emails, the duplicate closure, its guards, and matching afterwards.
import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.inbound import match_inbound_evidence
from apps.correspondence.models import InboundCorrespondenceReceipt
from apps.records import services
from apps.records.models import PublicRecordsRequest, Requester, RequestStatus
from apps.work_queue.models import WorkItem

from .inbox_fixtures import matched_email, unmatched_email

REASON = "Same requester and request; the web form arrived first."


class MergeRequestsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.clerk = User.objects.create_user(
            "clerk", "clerk@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.clerk, {StaffRole.INTAKE_CLERK})
        self.requester = Requester.objects.create(
            email="jane@example.org", name="Jane Q. Public"
        )
        self.target = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Bond levy emails (web form)",
            channel="portal",
            received_at=timezone.now() - dt.timedelta(days=3),
        )
        self.source = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Following up on my request",
            channel="email",
        )
        (
            self.intake_receipt,
            self.intake_message,
            self.intake_row,
        ) = matched_email(self.source, officer=self.officer, intake=True)
        (
            self.reply_receipt,
            self.reply_message,
            self.reply_row,
        ) = matched_email(self.source, officer=self.officer)

    def test_merge_moves_every_requester_email_and_closes_the_duplicate(self):
        before_due = self.target.statutory_response_due
        before_received = self.target.received_at

        target = services.merge_requests(
            self.source, self.target, actor=self.officer, reason=REASON
        )

        self.assertEqual(target.pk, self.target.pk)
        for receipt in (self.intake_receipt, self.reply_receipt):
            receipt.refresh_from_db()
            self.assertEqual(receipt.matched_request_id, self.target.pk)
            self.assertEqual(
                receipt.match_method,
                InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            )
        for message in (self.intake_message, self.reply_message):
            message.refresh_from_db()
            self.assertEqual(message.request_id, self.target.pk)
        # The Inbox rows moved with the emails and are still waiting: a merge
        # is not a claim that anyone has read them.
        for row in (self.intake_row, self.reply_row):
            row.refresh_from_db()
            self.assertEqual(row.request_id, self.target.pk)
            self.assertEqual(
                row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
            )
        self.assertFalse(WorkItem.objects.filter(request=self.source).exists())

        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.CLOSED_DUPLICATE)
        self.assertIsNotNone(self.source.closed_at)
        self.assertEqual(self.source.merged_into_id, self.target.pk)
        closure = self.source.events.get(to_status=RequestStatus.CLOSED_DUPLICATE)
        self.assertEqual(closure.actor, self.officer)
        self.assertEqual(closure.note, REASON)

        # The case of record is untouched except for the note on its timeline.
        self.target.refresh_from_db()
        self.assertEqual(self.target.status, RequestStatus.RECEIVED)
        self.assertEqual(self.target.received_at, before_received)
        self.assertEqual(self.target.statutory_response_due, before_due)
        merge_note = self.target.events.get(note__contains=self.source.number)
        self.assertEqual(merge_note.actor, self.officer)
        self.assertIn(REASON, merge_note.note)
        self.assertEqual(list(self.target.merged_sources.all()), [self.source])

        entry = AuditLog.objects.get(
            action="request.merged", target=self.target.number
        )
        self.assertEqual(entry.actor, self.officer)
        self.assertEqual(entry.detail["source"], self.source.number)
        self.assertEqual(len(entry.detail["moved_receipts"]), 2)
        self.assertEqual(entry.detail["unmoved_receipts"], [])

    def test_merge_requires_a_records_officer(self):
        with self.assertRaises(PermissionDenied):
            services.merge_requests(
                self.source, self.target, actor=self.clerk, reason=REASON
            )
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)
        self.intake_receipt.refresh_from_db()
        self.assertEqual(self.intake_receipt.matched_request_id, self.source.pk)

    def test_merge_requires_a_substantive_reason(self):
        with self.assertRaises(services.TerminalActionRequired):
            services.merge_requests(
                self.source, self.target, actor=self.officer, reason="dup"
            )

    def test_merge_refuses_a_closed_source_or_target(self):
        closed = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Already fulfilled",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )
        with self.assertRaises(services.ClosedRequestTransition):
            services.merge_requests(
                self.source, closed, actor=self.officer, reason=REASON
            )
        with self.assertRaises(services.ClosedRequestTransition):
            services.merge_requests(
                closed, self.target, actor=self.officer, reason=REASON
            )

    def test_merge_refuses_a_case_into_itself(self):
        with self.assertRaises(ValidationError):
            services.merge_requests(
                self.source, self.source, actor=self.officer, reason=REASON
            )

    def test_duplicate_status_is_reachable_only_through_a_merge(self):
        with self.assertRaises(services.TerminalActionRequired):
            services.transition_manually(
                self.source,
                RequestStatus.CLOSED_DUPLICATE,
                actor=self.officer,
                note=REASON,
            )
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_a_merged_duplicate_cannot_be_reopened(self):
        services.merge_requests(
            self.source, self.target, actor=self.officer, reason=REASON
        )
        with self.assertRaises(ValidationError) as caught:
            services.reopen_request(
                self.source,
                RequestStatus.GATHERING,
                actor=self.officer,
                reason="Requester followed up once more.",
            )
        self.assertIn(self.target.number, str(caught.exception))
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.CLOSED_DUPLICATE)

    def test_automatic_matching_follows_the_merge(self):
        services.merge_requests(
            self.source, self.target, actor=self.officer, reason=REASON
        )
        # A later email citing the retired number belongs with the rest of the
        # requester's exchange, not on a closed case nobody is watching.
        receipt, _ = unmatched_email(
            officer=self.officer, subject=f"Re: {self.source.number}"
        )
        with transaction.atomic():
            matched = match_inbound_evidence(receipt)
        self.assertEqual(
            matched.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
        )
        self.assertEqual(matched.candidate_request_id, self.target.pk)
