"""One officer action across several cases, without losing the case.

A batch is the point where a bulk convenience could quietly become fifty legal
acts nobody read. These tests pin the properties that keep it accountable: a
cap, an explicit confirmation, one independent delivery and one audit row per
case, a per-case report of what did not happen, and a hard refusal for the
kinds that are decided one case at a time.
"""

import datetime as dt
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError

from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
)
from apps.correspondence.tests.test_outbound_delivery import (
    OutboundDeliveryServiceTestCase,
)
from apps.records import services as records_services
from apps.records.models import PublicRecordsRequest, Requester, RequestStatus

SUMMARY = (
    "The District produced every responsive record it located and withheld "
    "nothing."
)


class BatchCorrespondenceTests(OutboundDeliveryServiceTestCase):
    def make_case(self, index):
        requester = Requester.objects.create(
            name=f"Requester {index}",
            email=f"requester{index}@example.org",
        )
        return PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"Records about project {index}.",
            estimated_completion=dt.date(2026, 8, 3),
        )

    def test_one_form_drafts_a_notice_for_every_selected_case(self):
        cases = [self.make_case(index) for index in range(3)]

        outcome = services.batch_create_closure_drafts(
            [case.number for case in cases],
            kind=Correspondence.Kind.COMPLETION_CLOSURE,
            production_summary=SUMMARY,
            actor=self.officer,
            confirmed=True,
        )

        self.assertEqual(len(outcome.succeeded), 3)
        self.assertEqual(outcome.failed, ())
        for case, (number, correspondence) in zip(
            cases,
            outcome.succeeded,
            strict=True,
        ):
            self.assertEqual(number, case.number)
            self.assertEqual(correspondence.request_id, case.pk)
            self.assertEqual(correspondence.status, Correspondence.Status.DRAFT)
            self.assertIn(case.number, correspondence.draft_body)
            self.assertTrue(
                AuditLog.objects.filter(
                    action="correspondence.batch_drafted",
                    target=case.number,
                    detail__batch_id=outcome.batch_id,
                ).exists()
            )
        # Drafting sends nothing.
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_one_bad_case_does_not_cost_the_officer_the_other_drafts(self):
        good = self.make_case(1)

        outcome = services.batch_create_closure_drafts(
            [good.number, "PRR-NOT-A-CASE"],
            kind=Correspondence.Kind.COMPLETION_CLOSURE,
            production_summary=SUMMARY,
            actor=self.officer,
            confirmed=True,
        )

        self.assertEqual(
            [number for number, _ in outcome.succeeded],
            [good.number],
        )
        self.assertEqual(
            outcome.failed,
            (("PRR-NOT-A-CASE", "No case with this number."),),
        )

    def test_a_batch_is_capped_confirmed_deduplicated_and_officer_only(self):
        case = self.make_case(1)
        common = {
            "kind": Correspondence.Kind.COMPLETION_CLOSURE,
            "production_summary": SUMMARY,
            "actor": self.officer,
            "confirmed": True,
        }

        with self.assertRaisesMessage(ValidationError, "at least one case"):
            services.batch_create_closure_drafts([], **common)
        with self.assertRaisesMessage(ValidationError, "at most 50"):
            services.batch_create_closure_drafts(
                [f"PRR-{index}" for index in range(51)],
                **common,
            )
        with self.assertRaisesMessage(ValidationError, "appears twice"):
            services.batch_create_closure_drafts(
                [case.number, case.number],
                **common,
            )
        with self.assertRaisesMessage(ValidationError, "Confirm"):
            services.batch_create_closure_drafts(
                [case.number],
                **(common | {"confirmed": False}),
            )
        with self.assertRaises(PermissionDenied):
            services.batch_create_closure_drafts(
                [case.number],
                **(common | {"actor": self.integration}),
            )

    def test_a_non_payment_closure_is_never_drafted_from_one_shared_amount(self):
        case = self.make_case(1)

        with self.assertRaisesMessage(ValidationError, "completion closures"):
            services.batch_create_closure_drafts(
                [case.number],
                kind=Correspondence.Kind.NONPAYMENT_CLOSURE,
                actor=self.officer,
                confirmed=True,
            )

    def test_sending_gives_each_case_its_own_delivery_and_audit_row(self):
        cases = [self.make_case(index) for index in range(3)]
        drafted = services.batch_create_closure_drafts(
            [case.number for case in cases],
            kind=Correspondence.Kind.COMPLETION_CLOSURE,
            production_summary=SUMMARY,
            actor=self.officer,
            confirmed=True,
        )

        outcome = services.batch_approve_and_send(
            [str(correspondence.pk) for _, correspondence in drafted.succeeded],
            actor=self.officer,
            confirmed=True,
        )

        self.assertEqual(len(outcome.succeeded), 3)
        self.assertEqual(outcome.failed, ())
        deliveries = CorrespondenceDelivery.objects.all()
        self.assertEqual(deliveries.count(), 3)
        self.assertEqual(
            len({delivery.idempotency_key for delivery in deliveries}),
            3,
        )
        for case in cases:
            self.assertEqual(
                AuditLog.objects.filter(
                    action="correspondence.batch_approve_and_send",
                    target=case.number,
                    detail__batch_id=outcome.batch_id,
                ).count(),
                1,
            )

    def test_a_denial_cannot_enter_a_batch(self):
        case = self.make_case(1)
        denial = services.create_authored_draft(
            case,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial",
            body="The responsive records are exempt in full.",
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )

        with self.assertRaisesMessage(ValidationError, "one case at a time"):
            services.batch_approve_and_send(
                [str(denial.pk)],
                actor=self.officer,
                confirmed=True,
            )

        denial.refresh_from_db()
        self.assertEqual(denial.status, Correspondence.Status.DRAFT)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_two_messages_for_one_case_are_refused_before_anything_sends(self):
        case = self.make_case(1)
        first = services.create_completion_closure_draft(
            case,
            production_summary=SUMMARY,
            actor=self.officer,
        )
        second = services.create_authored_draft(
            case,
            kind=Correspondence.Kind.GENERAL,
            subject="A second message for the same case",
            body="This should not ride along with the closure.",
            actor=self.officer,
        )

        with self.assertRaisesMessage(ValidationError, "one message per case"):
            services.batch_approve_and_send(
                [str(first.pk), str(second.pk)],
                actor=self.officer,
                confirmed=True,
            )

        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_sending_requires_confirmation_and_an_officer(self):
        case = self.make_case(1)
        draft = services.create_completion_closure_draft(
            case,
            production_summary=SUMMARY,
            actor=self.officer,
        )

        with self.assertRaisesMessage(ValidationError, "Confirm"):
            services.batch_approve_and_send(
                [str(draft.pk)],
                actor=self.officer,
                confirmed=False,
            )
        with self.assertRaises(PermissionDenied):
            services.batch_approve_and_send(
                [str(draft.pk)],
                actor=self.integration,
                confirmed=True,
            )
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_an_unknown_message_id_stops_the_whole_batch(self):
        case = self.make_case(1)
        draft = services.create_completion_closure_draft(
            case,
            production_summary=SUMMARY,
            actor=self.officer,
        )

        with self.assertRaisesMessage(ValidationError, "must exist"):
            services.batch_approve_and_send(
                [
                    str(draft.pk),
                    "00000000-0000-0000-0000-000000000000",
                ],
                actor=self.officer,
                confirmed=True,
            )

        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_a_closure_in_flight_holds_the_case_where_the_officer_left_it(self):
        # Delivery of a closure moves the case to its closed status. A manual
        # move in the meantime would be silently overwritten on delivery, so
        # it is refused until the closure is delivered or cancelled.
        case = self.make_case(1)
        draft = services.create_completion_closure_draft(
            case,
            production_summary=SUMMARY,
            actor=self.officer,
        )
        services.approve_and_send(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        with self.assertRaises(records_services.CorrespondenceDeliveryRequired):
            records_services.transition(
                case,
                RequestStatus.GATHERING,
                actor=self.officer,
                note="Back to gathering.",
            )

        case.refresh_from_db()
        self.assertEqual(case.status, RequestStatus.RECEIVED)

    def test_a_refusal_or_database_error_is_one_case_not_the_whole_batch(self):
        cases = [self.make_case(index) for index in range(3)]
        real = services.create_completion_closure_draft
        refusals = {
            cases[0].pk: PermissionDenied("Records Officer role required."),
            cases[1].pk: IntegrityError("duplicate key"),
        }

        def refuse_some(prr, **kwargs):
            if prr.pk in refusals:
                raise refusals[prr.pk]
            return real(prr, **kwargs)

        with mock.patch.object(
            services,
            "create_completion_closure_draft",
            side_effect=refuse_some,
        ):
            outcome = services.batch_create_closure_drafts(
                [case.number for case in cases],
                kind=Correspondence.Kind.COMPLETION_CLOSURE,
                production_summary=SUMMARY,
                actor=self.officer,
                confirmed=True,
            )

        self.assertEqual(
            [number for number, _ in outcome.succeeded],
            [cases[2].number],
        )
        self.assertEqual(
            dict(outcome.failed),
            {
                cases[0].number: "Records Officer role required.",
                cases[1].number: services.BATCH_DATABASE_FAILURE,
            },
        )

    def test_a_send_refused_mid_batch_is_reported_with_the_rest(self):
        cases = [self.make_case(index) for index in range(3)]
        drafted = services.batch_create_closure_drafts(
            [case.number for case in cases],
            kind=Correspondence.Kind.COMPLETION_CLOSURE,
            production_summary=SUMMARY,
            actor=self.officer,
            confirmed=True,
        )
        real = services.approve_and_send
        refusals = {
            cases[0].pk: PermissionDenied(),
            cases[1].pk: OperationalError("could not obtain lock"),
        }

        def refuse_some(draft, **kwargs):
            if draft.request_id in refusals:
                raise refusals[draft.request_id]
            return real(draft, **kwargs)

        with mock.patch.object(
            services,
            "approve_and_send",
            side_effect=refuse_some,
        ):
            outcome = services.batch_approve_and_send(
                [str(item.pk) for _, item in drafted.succeeded],
                actor=self.officer,
                confirmed=True,
            )

        self.assertEqual(
            [number for number, _ in outcome.succeeded],
            [cases[2].number],
        )
        self.assertEqual(
            dict(outcome.failed),
            {
                cases[0].number: services.BATCH_PERMISSION_FAILURE,
                cases[1].number: services.BATCH_DATABASE_FAILURE,
            },
        )
        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)

    def test_a_database_failure_is_logged_with_its_exception(self):
        """The batch tells the officer `BATCH_DATABASE_FAILURE`, which says
        nothing about the cause on purpose -- a vendor message can carry
        request material. The traceback has to reach the log instead, or the
        failure is unexplainable to anyone. `_batch_failure_detail` is called
        from an `except` block but is a plain function, so it takes the
        exception rather than reading it off the stack."""
        failure = OperationalError("could not serialize access")

        with self.assertLogs("apps.correspondence.services", level="ERROR") as logged:
            detail = services._batch_failure_detail(failure, case="PRR-2026-0003")

        record = next(
            entry
            for entry in logged.records
            if "Batch correspondence case failed" in entry.getMessage()
        )
        self.assertIsNotNone(record.exc_info)
        self.assertIs((record.exc_info or (None, None, None))[1], failure)
        self.assertEqual(getattr(record, "case", None), "PRR-2026-0003")
        self.assertEqual(detail, services.BATCH_DATABASE_FAILURE)

    def test_an_expected_refusal_is_not_logged_as_a_fault(self):
        """A ValidationError is the officer's answer, not an incident; it
        must not fill the error log or hide a real database failure."""
        with mock.patch.object(services.logger, "error") as logged:
            detail = services._batch_failure_detail(
                ValidationError("Pick a template first."), case="PRR-2026-0004"
            )

        logged.assert_not_called()
        self.assertEqual(detail, "Pick a template first.")
