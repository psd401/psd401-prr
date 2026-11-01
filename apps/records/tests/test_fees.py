"""RCW 42.56.120 fee schedules, assessments, and payments are evidentiary."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.fees import (
    activate_fee_schedule,
    activate_fee_waiver_floor,
    apply_deposit_gate,
    assert_deposit_gate_clear,
    assess_installment_fee,
    assessment_is_below_waiver_floor,
    lift_deposit_gate,
    record_deposit_payment,
    record_fee_payment,
    unpaid_fee_attention_requests,
)
from apps.records.models import (
    FeeAssessment,
    FeeDepositGate,
    FeePayment,
    FeeSchedule,
    FeeWaiverFloor,
    Installment,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


class FeeFrameworkTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board records.",
        )
        self.installment = Installment.objects.create(
            request=self.request,
            sequence=1,
        )

    def activate_schedule(self, **overrides):
        data = {
            "name": "PSD adopted PRA copy schedule",
            "basis": FeeSchedule.Basis.STATUTORY_DEFAULT,
            "effective_on": date(2026, 8, 1),
            "counsel_approval_reference": "Legal memo PRA-fees-2026-07-28",
            "adoption_reference": "Board policy 4040 resolution 2026-08-01",
            "photocopy_page_rate": Decimal("0.15"),
            "scanned_page_rate": Decimal("0.10"),
            "electronic_file_bundle_rate": Decimal("0.05"),
            "electronic_gb_rate": Decimal("0.10"),
            "flat_fee_amount": Decimal("2.00"),
        }
        data.update(overrides)
        return activate_fee_schedule(actor=self.officer, **data)

    def test_activation_requires_current_officer_counsel_and_adoption_evidence(self):
        with self.assertRaises(PermissionDenied):
            activate_fee_schedule(
                actor=self.reviewer,
                name="Unauthorized",
                basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
                effective_on=date(2026, 8, 1),
                counsel_approval_reference="Legal memo 1",
                adoption_reference="Policy 1",
            )

        with self.assertRaisesMessage(ValidationError, "counsel approval"):
            activate_fee_schedule(
                actor=self.officer,
                name="Missing legal approval",
                basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
                effective_on=date(2026, 8, 1),
                counsel_approval_reference="",
                adoption_reference="Policy 1",
            )

        schedule = self.activate_schedule()

        self.assertEqual(schedule.activated_by, self.officer)
        self.assertEqual(schedule.photocopy_page_rate, Decimal("0.15"))
        self.assertTrue(AuditLog.objects.filter(
            action="fee_schedule.activated",
            actor=self.officer,
            detail__schedule_id=str(schedule.pk),
        ).exists())

    def test_statutory_schedule_cannot_exceed_published_caps(self):
        with self.assertRaisesMessage(ValidationError, "photocopy"):
            self.activate_schedule(photocopy_page_rate=Decimal("0.16"))
        with self.assertRaisesMessage(ValidationError, "flat fee"):
            self.activate_schedule(flat_fee_amount=Decimal("2.01"))

    def test_assessment_calculates_combined_itemized_charge_and_ten_percent_deposit(self):
        schedule = self.activate_schedule()

        assessment = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            photocopy_pages=2,
            scanned_pages=3,
            electronic_files=5,
            electronic_gb=Decimal("1.50"),
            media_cost=Decimal("1.20"),
            postage_cost=Decimal("0.68"),
            deposit_percent=Decimal(10),
            reason="Requested paper and electronic delivery costs under the adopted schedule.",
        )

        # .30 + .30 + ceil(5/4)*.05 + .15 + 1.20 + .68 = 2.73
        self.assertEqual(assessment.itemized_amount, Decimal("2.73"))
        self.assertEqual(assessment.amount, Decimal("2.73"))
        self.assertEqual(assessment.deposit_amount, Decimal("0.27"))
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.fee_amount, Decimal("2.73"))
        self.assertIsNone(self.installment.fee_paid_at)
        self.assertTrue(AuditLog.objects.filter(
            action="installment.fee_assessed",
            target=self.request.number,
            detail__assessment_id=str(assessment.pk),
            detail__amount="2.73",
        ).exists())

        with self.assertRaisesMessage(ValidationError, "ten percent"):
            assess_installment_fee(
                self.installment.pk,
                actor=self.officer,
                schedule_id=schedule.pk,
                deposit_percent=Decimal("10.01"),
                reason="Invalid deposit request above the statutory limit.",
            )

    def test_customized_service_charge_requires_complete_requester_notice_evidence(self):
        schedule = self.activate_schedule()

        with self.assertRaisesMessage(ValidationError, "specific expertise"):
            assess_installment_fee(
                self.installment.pk,
                actor=self.officer,
                schedule_id=schedule.pk,
                customized_service_cost=Decimal("25.00"),
                customized_service_explanation=(
                    "A one-off database compilation is not used for another district purpose."
                ),
                requester_notice_reference="Gmail message 18b9-notice",
                requester_approval_reference="Gmail reply 18b9-approved",
                reason="Requester elected to proceed after notice and opportunity to narrow.",
            )

        assessment = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            customized_service_cost=Decimal("25.00"),
            customized_service_explanation=(
                "A one-off database compilation is not used for another district purpose."
            ),
            customized_service_expertise="Database administrator SQL extraction",
            requester_notice_reference="Gmail message 18b9-notice",
            requester_approval_reference="Gmail reply 18b9-approved",
            reason="Requester elected to proceed after notice and opportunity to narrow.",
        )
        self.assertEqual(assessment.amount, Decimal("25.00"))

    def test_flat_fee_is_first_installment_only_and_covers_later_installments(self):
        schedule = self.activate_schedule()
        first = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            electronic_files=200,
            use_flat_fee=True,
            reason="Documented itemized estimate exceeds the adopted flat alternative.",
        )
        self.assertEqual(first.itemized_amount, Decimal("2.50"))
        self.assertEqual(first.amount, Decimal("2.00"))

        later = Installment.objects.create(request=self.request, sequence=2)
        covered = assess_installment_fee(
            later.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            electronic_files=200,
            reason="Covered by the flat fee elected for the first installment.",
        )
        self.assertEqual(covered.itemized_amount, Decimal("2.50"))
        self.assertEqual(covered.amount, Decimal("0.00"))
        self.assertTrue(covered.covered_by_request_flat_fee)

        another_request = PublicRecordsRequest.objects.create(
            requester=self.request.requester,
            description="Other records.",
        )
        second_only = Installment.objects.create(request=another_request, sequence=2)
        with self.assertRaisesMessage(ValidationError, "first installment"):
            assess_installment_fee(
                second_only.pk,
                actor=self.officer,
                schedule_id=schedule.pk,
                electronic_files=200,
                use_flat_fee=True,
                reason="Flat fee cannot begin on a later installment.",
            )

    def test_payment_evidence_is_append_only_and_projection_becomes_paid_at_exact_total(self):
        schedule = self.activate_schedule()
        assessment = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            electronic_files=80,
            reason="Electronic file charge under the adopted schedule.",
        )
        first = record_fee_payment(
            self.installment.pk,
            actor=self.officer,
            amount=Decimal("0.40"),
            method=FeePayment.Method.CHECK,
            reference="District receipt 1042",
            received_on=date(2026, 8, 2),
        )
        self.installment.refresh_from_db()
        self.assertIsNone(self.installment.fee_paid_at)
        second = record_fee_payment(
            self.installment.pk,
            actor=self.officer,
            amount=Decimal("0.60"),
            method=FeePayment.Method.CHECK,
            reference="District receipt 1043",
            received_on=date(2026, 8, 2),
        )
        self.installment.refresh_from_db()
        self.assertIsNotNone(self.installment.fee_paid_at)
        self.assertEqual(first.assessment, assessment)
        self.assertEqual(second.assessment, assessment)

        first.reference = "rewritten"
        with self.assertRaisesMessage(ValidationError, "append-only"):
            first.save()
        with self.assertRaisesMessage(ValidationError, "append-only"):
            FeePayment.objects.filter(pk=second.pk).delete()
        with self.assertRaisesMessage(ValidationError, "exceed"):
            record_fee_payment(
                self.installment.pk,
                actor=self.officer,
                amount=Decimal("0.01"),
                method=FeePayment.Method.CASH,
                reference="Overpayment attempt",
                received_on=date(2026, 8, 2),
            )

    def test_assessment_revision_retains_history_but_is_blocked_after_payment(self):
        schedule = self.activate_schedule()
        first = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=10,
            reason="Initial documented copy estimate.",
        )
        revised = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=8,
            reason="Requester narrowed the request before copies were made.",
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.supersedes, first)
        self.assertEqual(FeeAssessment.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("1.00"))

        record_fee_payment(
            self.installment.pk,
            actor=self.officer,
            amount=Decimal("0.80"),
            method=FeePayment.Method.OTHER,
            reference="Finance ledger entry 44",
            received_on=date(2026, 8, 2),
        )
        with self.assertRaisesMessage(ValidationError, "payment evidence"):
            assess_installment_fee(
                self.installment.pk,
                actor=self.officer,
                schedule_id=schedule.pk,
                scanned_pages=7,
                reason="Too late to revise after payment.",
            )


class FeeWaiverFloorTests(FeeFrameworkTests):
    """The floor prompts a person. It never waives anything by itself."""

    def adopt_floor(self, amount=Decimal("1.00"), **overrides):
        data = {
            "actor": self.officer,
            "amount": amount,
            "counsel_reference": "Legal memo PRA-waiver-floor-2026-09-21",
            "effective_on": date(2026, 8, 1),
        }
        data.update(overrides)
        return activate_fee_waiver_floor(**data)

    def small_assessment(self, schedule, *, pages=3):
        return assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=pages,
            reason="Three scanned pages under the adopted schedule.",
        )

    def test_adoption_needs_a_current_officer_and_a_counsel_reference(self):
        with self.assertRaises(PermissionDenied):
            self.adopt_floor(actor=self.reviewer)
        with self.assertRaisesMessage(ValidationError, "counsel reference"):
            self.adopt_floor(counsel_reference="")
        with self.assertRaisesMessage(ValidationError, "greater than zero"):
            self.adopt_floor(amount=Decimal(0))

        floor = self.adopt_floor()

        self.assertEqual(floor.amount, Decimal("1.00"))
        self.assertEqual(floor.activated_by, self.officer)
        self.assertTrue(
            AuditLog.objects.filter(
                action="fee_waiver_floor.activated",
                actor=self.officer,
                detail__floor_id=str(floor.pk),
                detail__amount="1.00",
            ).exists()
        )

    def test_assessment_under_the_adopted_floor_prompts_without_waiving(self):
        schedule = self.activate_schedule()
        self.adopt_floor()

        assessment = self.small_assessment(schedule)

        # Thirty cents of scanning is under the adopted dollar.
        self.assertEqual(assessment.amount, Decimal("0.30"))
        self.assertTrue(assessment_is_below_waiver_floor(assessment))
        # The prompt changes nothing: the fee is still owed and still blocks
        # release until a person waives or a payment arrives.
        self.assertEqual(assessment.waiver_amount, Decimal(0))
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.fee_amount, Decimal("0.30"))
        self.assertIsNone(self.installment.fee_paid_at)

    def test_floor_never_prompts_above_it_or_on_an_already_free_assessment(self):
        schedule = self.activate_schedule()
        self.adopt_floor()

        above = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=40,
            reason="Forty scanned pages under the adopted schedule.",
        )
        self.assertEqual(above.amount, Decimal("4.00"))
        self.assertFalse(assessment_is_below_waiver_floor(above))

        waived = assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=3,
            waiver_amount=Decimal("0.30"),
            waiver_reason="Under the adopted floor; collection costs more.",
            reason="Waived after the floor prompt, by the Records Officer.",
        )
        self.assertEqual(waived.amount, Decimal("0.00"))
        self.assertFalse(assessment_is_below_waiver_floor(waived))

    def test_no_adopted_floor_means_no_prompt_at_all(self):
        schedule = self.activate_schedule()
        assessment = self.small_assessment(schedule)
        self.assertIsNone(FeeWaiverFloor.objects.first())
        self.assertFalse(assessment_is_below_waiver_floor(assessment))


class FeeDepositGateTests(FeeFrameworkTests):
    """A deposit holds work. It never closes a case and never expires."""

    def open_gate(self, **overrides):
        data = {
            "actor": self.officer,
            "scope": FeeDepositGate.Scope.PRODUCTION,
            "amount": Decimal("36.72"),
            "estimated_total": Decimal("367.25"),
            "reason": "Ten percent deposit on the estimated copying charge.",
        }
        data.update(overrides)
        return apply_deposit_gate(self.request.number, **data)

    def test_a_gate_needs_an_adopted_schedule_a_reason_and_the_ten_percent_cap(self):
        with self.assertRaisesMessage(ValidationError, "adopted fee schedule"):
            self.open_gate()

        self.activate_schedule()

        with self.assertRaises(PermissionDenied):
            self.open_gate(actor=self.reviewer)
        with self.assertRaisesMessage(ValidationError, "ten percent"):
            self.open_gate(amount=Decimal("36.73"))
        with self.assertRaisesMessage(ValidationError, "deposit reason"):
            self.open_gate(reason="short")
        with self.assertRaisesMessage(ValidationError, "scope is invalid"):
            self.open_gate(scope="everything")

        gate = self.open_gate()

        self.assertTrue(gate.is_open)
        self.assertEqual(gate.applied_by, self.officer)
        self.assertTrue(
            AuditLog.objects.filter(
                action="fee_deposit_gate.applied",
                target=self.request.number,
                detail__gate_id=str(gate.pk),
                detail__amount="36.72",
            ).exists()
        )

        with self.assertRaisesMessage(ValidationError, "already held"):
            self.open_gate()
        # A different kind of work is a different gate.
        self.open_gate(scope=FeeDepositGate.Scope.COLLECTION)

    def test_an_open_gate_blocks_only_the_work_it_names(self):
        self.activate_schedule()
        self.open_gate(scope=FeeDepositGate.Scope.COLLECTION)

        with self.assertRaisesMessage(ValidationError, "deposit requested on"):
            assert_deposit_gate_clear(
                self.request,
                scope=FeeDepositGate.Scope.COLLECTION,
            )
        # Production is untouched by a collection deposit.
        assert_deposit_gate_clear(
            self.request,
            scope=FeeDepositGate.Scope.PRODUCTION,
        )

    def test_the_full_deposit_releases_the_work_and_a_partial_one_does_not(self):
        self.activate_schedule()
        gate = self.open_gate()

        with self.assertRaisesMessage(ValidationError, "partial deposit"):
            record_deposit_payment(
                gate.pk,
                actor=self.officer,
                amount=Decimal("10.00"),
                method=FeePayment.Method.CHECK,
                reference="Check 4411",
                received_on=date(2026, 8, 20),
            )
        with self.assertRaises(PermissionDenied):
            record_deposit_payment(
                gate.pk,
                actor=self.reviewer,
                amount=Decimal("36.72"),
                method=FeePayment.Method.CHECK,
                reference="Check 4411",
                received_on=date(2026, 8, 20),
            )

        paid = record_deposit_payment(
            gate.pk,
            actor=self.officer,
            amount=Decimal("36.72"),
            method=FeePayment.Method.CHECK,
            reference="Check 4411",
            received_on=date(2026, 8, 20),
        )

        self.assertFalse(paid.is_open)
        self.assertEqual(paid.satisfied_by, self.officer)
        assert_deposit_gate_clear(
            self.request,
            scope=FeeDepositGate.Scope.PRODUCTION,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="fee_deposit_gate.satisfied",
                target=self.request.number,
                detail__gate_id=str(paid.pk),
                detail__reference="Check 4411",
            ).exists()
        )
        with self.assertRaisesMessage(ValidationError, "already recorded"):
            record_deposit_payment(
                gate.pk,
                actor=self.officer,
                amount=Decimal("36.72"),
                method=FeePayment.Method.CHECK,
                reference="Check 4412",
                received_on=date(2026, 8, 21),
            )

    def test_lifting_a_gate_takes_a_named_officer_and_a_written_reason(self):
        self.activate_schedule()
        gate = self.open_gate()

        with self.assertRaises(PermissionDenied):
            lift_deposit_gate(gate.pk, actor=self.reviewer, reason="No reason.")
        with self.assertRaisesMessage(ValidationError, "lift reason"):
            lift_deposit_gate(gate.pk, actor=self.officer, reason="ok")

        lifted = lift_deposit_gate(
            gate.pk,
            actor=self.officer,
            reason="Requester narrowed the request below the deposit threshold.",
        )

        self.assertFalse(lifted.is_open)
        assert_deposit_gate_clear(
            self.request,
            scope=FeeDepositGate.Scope.PRODUCTION,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="fee_deposit_gate.lifted",
                target=self.request.number,
                detail__gate_id=str(lifted.pk),
            ).exists()
        )
        with self.assertRaisesMessage(ValidationError, "already lifted"):
            lift_deposit_gate(
                gate.pk,
                actor=self.officer,
                reason="Lifting a gate that is already lifted.",
            )


class DepositGateHoldsRealWorkTests(FeeFrameworkTests):
    """The gate is only worth anything if the gated work actually refuses."""

    def test_production_work_refuses_while_a_production_deposit_is_open(self):
        from apps.records.production import prepare_installment

        self.activate_schedule()
        gate = apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.PRODUCTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )

        with self.assertRaisesMessage(ValidationError, "deposit requested on"):
            prepare_installment(self.installment.pk, actor=self.officer)

        record_deposit_payment(
            gate.pk,
            actor=self.officer,
            amount=Decimal("36.72"),
            method=FeePayment.Method.CHECK,
            reference="Check 4411",
            received_on=timezone.localdate(),
        )

        # The deposit no longer blocks; preparation now fails on its own
        # merits (an installment with no records), not on the money.
        with self.assertRaises(ValidationError) as caught:
            prepare_installment(self.installment.pk, actor=self.officer)
        self.assertNotIn("deposit", str(caught.exception).lower())

    def test_a_production_deposit_does_not_hold_collection_work(self):
        self.activate_schedule()
        apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.PRODUCTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )

        assert_deposit_gate_clear(
            self.request,
            scope=FeeDepositGate.Scope.COLLECTION,
        )


class UnpaidFeeAttentionTests(FeeFrameworkTests):
    """Day thirty surfaces a case. It never closes one."""

    def later(self, *, days):
        """Look at the same rows from a day far enough in the future.

        Fee evidence is append-only, so the test moves the clock rather than
        back-dating a row the application itself could never rewrite.
        """
        return unpaid_fee_attention_requests(
            as_of=timezone.localdate() + timedelta(days=days)
        )

    def test_an_unpaid_assessment_surfaces_only_after_thirty_days(self):
        schedule = self.activate_schedule()
        assess_installment_fee(
            self.installment.pk,
            actor=self.officer,
            schedule_id=schedule.pk,
            scanned_pages=40,
            reason="Forty scanned pages under the adopted schedule.",
        )

        self.assertNotIn(self.request, list(self.later(days=29)))
        self.assertIn(self.request, list(self.later(days=31)))

        # Paying it in full takes the case back off the list.
        record_fee_payment(
            self.installment.pk,
            actor=self.officer,
            amount=Decimal("4.00"),
            method=FeePayment.Method.CHECK,
            reference="Check 4413",
            received_on=timezone.localdate(),
        )
        self.assertNotIn(self.request, list(self.later(days=31)))

    def test_an_unpaid_deposit_surfaces_and_lifting_it_takes_it_back_off(self):
        self.activate_schedule()
        gate = apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )

        self.assertNotIn(self.request, list(self.later(days=29)))
        self.assertIn(self.request, list(self.later(days=31)))

        lift_deposit_gate(
            gate.pk,
            actor=self.officer,
            reason="Requester withdrew the broad portion of the request.",
        )
        self.assertNotIn(self.request, list(self.later(days=31)))

    def test_a_closed_case_is_never_surfaced_for_an_unpaid_deposit(self):
        self.activate_schedule()
        apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )
        PublicRecordsRequest.objects.filter(pk=self.request.pk).update(
            status=RequestStatus.CLOSED_ABANDONED
        )

        self.assertEqual(list(self.later(days=45)), [])

    def test_the_watchdog_only_surfaces_the_case_and_closes_nothing(self):
        from apps.records.tasks import unpaid_fee_watchdog
        from apps.work_queue.models import WorkItem

        self.activate_schedule()
        apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )

        # Nothing at day zero.
        self.assertEqual(unpaid_fee_watchdog(), 0)
        self.assertFalse(
            WorkItem.objects.filter(
                work_kind=WorkItem.WorkKind.FEE_UNPAID
            ).exists()
        )

        with patch(
            "apps.records.fees.UNPAID_FEE_ATTENTION_DAYS",
            0,
        ):
            self.assertEqual(unpaid_fee_watchdog(), 1)
            # Running it again is idempotent: one chip per case.
            self.assertEqual(unpaid_fee_watchdog(), 1)

        item = WorkItem.objects.get(work_kind=WorkItem.WorkKind.FEE_UNPAID)
        self.assertEqual(item.request, self.request)
        self.assertEqual(
            item.source_type,
            WorkItem.SourceType.REQUEST,
        )
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.RECEIVED)

    def test_the_watchdog_retires_the_chip_once_nothing_is_owed(self):
        from apps.records.tasks import unpaid_fee_watchdog
        from apps.work_queue.models import WorkItem

        self.activate_schedule()
        gate = apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated copying charge.",
        )
        with patch("apps.records.fees.UNPAID_FEE_ATTENTION_DAYS", 0):
            unpaid_fee_watchdog()
            item = WorkItem.objects.get(work_kind=WorkItem.WorkKind.FEE_UNPAID)
            self.assertEqual(
                item.disposition,
                WorkItem.Disposition.NEEDS_ATTENTION,
            )

            lift_deposit_gate(
                gate.pk,
                actor=self.officer,
                reason="Requester narrowed the request below the deposit need.",
            )
            self.assertEqual(unpaid_fee_watchdog(), 0)
            item.refresh_from_db()
            self.assertEqual(
                item.disposition,
                WorkItem.Disposition.SOURCE_EXCLUDED,
            )

            # A later unpaid deposit on the same case brings it back.
            apply_deposit_gate(
                self.request.number,
                actor=self.officer,
                scope=FeeDepositGate.Scope.COLLECTION,
                amount=Decimal("10.00"),
                estimated_total=Decimal("100.00"),
                reason="Ten percent deposit on the added custodian's mail.",
            )
            self.assertEqual(unpaid_fee_watchdog(), 1)
            item.refresh_from_db()
            self.assertEqual(
                item.disposition,
                WorkItem.Disposition.NEEDS_ATTENTION,
            )
