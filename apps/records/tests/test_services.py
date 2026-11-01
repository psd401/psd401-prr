# ABOUTME: Regression tests for human-attributed terminal closure while a
# ABOUTME: clarification cycle is open, including atomic audit evidence.
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import ClarificationCycle, Correspondence
from apps.records import services
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class ClarificationTerminalClosureTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "records-officer",
            "records-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(
            self.officer,
            {StaffRole.RECORDS_OFFICER},
        )

    def _open_clarification(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Private Requester",
                email="private-requester@example.org",
            ),
            description="PRIVATE legal request narrative.",
            status=RequestStatus.CLARIFICATION,
        )
        opened_at = timezone.now() - dt.timedelta(days=1)
        opened_event = StatusEvent.objects.create(
            request=prr,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
            occurred_at=opened_at,
        )
        outbound = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.DRAFT,
            draft_subject="Clarification requested",
            draft_body="Please clarify the scope.",
            created_by=self.officer,
        )
        cycle = ClarificationCycle.objects.create(
            request=prr,
            outbound_correspondence=outbound,
            opened_at=opened_at,
            opened_status_event=opened_event,
        )
        return prr, cycle

    def test_human_terminal_closure_cancels_cycle_at_transition_time_and_audits(self):
        for closure_status, reason in (
            (
                RequestStatus.CLOSED_WITHDRAWN,
                "Requester withdrew the request in a separately retained message.",
            ),
            (
                RequestStatus.CLOSED_ABANDONED,
                "Requester did not clarify by the documented response deadline.",
            ),
        ):
            with self.subTest(closure_status=closure_status):
                prr, cycle = self._open_clarification()

                closed = services.transition_manually(
                    prr,
                    closure_status,
                    actor=self.officer,
                    note=reason,
                )

                closed.refresh_from_db()
                cycle.refresh_from_db()
                event = closed.events.get(
                    from_status=RequestStatus.CLARIFICATION,
                    to_status=closure_status,
                )
                self.assertEqual(closed.status, closure_status)
                self.assertEqual(closed.closed_at, event.occurred_at)
                self.assertEqual(cycle.cancelled_at, event.occurred_at)
                self.assertEqual(event.actor, self.officer)
                self.assertEqual(event.note, reason)
                self.assertIsNone(cycle.inbound_response_correspondence_id)
                self.assertIsNone(cycle.responded_at)
                self.assertIsNone(cycle.resolved_at)
                self.assertIsNone(cycle.resolved_by_id)
                self.assertIsNone(cycle.resolved_status_event_id)

                audit_row = AuditLog.objects.get(
                    action="correspondence.clarification_cancelled_for_closure",
                    target=closed.number,
                )
                self.assertEqual(audit_row.actor, self.officer)
                self.assertEqual(
                    audit_row.detail,
                    {
                        "clarification_cycle_id": str(cycle.id),
                        "outbound_correspondence_id": str(
                            cycle.outbound_correspondence_id
                        ),
                        "status_event_id": str(event.id),
                        "closure_status": closure_status,
                        "reason": reason,
                    },
                )
                audit_text = str(audit_row.detail)
                self.assertNotIn(closed.requester.name, audit_text)
                self.assertNotIn(closed.requester.email, audit_text)
                self.assertNotIn(closed.description, audit_text)

    def test_terminal_closure_requires_an_open_cycle_and_substantive_reason(self):
        prr, cycle = self._open_clarification()

        with self.assertRaisesRegex(
            services.ClarificationResolutionRequired,
            "substantive closure reason",
        ):
            services.transition_manually(
                prr,
                RequestStatus.CLOSED_WITHDRAWN,
                actor=self.officer,
                note=" ",
            )

        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(cycle.cancelled_at)

        cycle.cancelled_at = timezone.now()
        cycle.save(update_fields=["cancelled_at"])
        with self.assertRaisesRegex(
            services.ClarificationResolutionRequired,
            "open clarification cycle",
        ):
            services.transition_manually(
                prr,
                RequestStatus.CLOSED_WITHDRAWN,
                actor=self.officer,
                note="Requester withdrew the pending request.",
            )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)

    def test_clarification_closure_requires_current_human_staff_actor(self):
        prr, cycle = self._open_clarification()

        with self.assertRaisesRegex(
            services.ClarificationResolutionRequired,
            "current human staff identity",
        ):
            services.transition_manually(
                prr,
                RequestStatus.CLOSED_ABANDONED,
                actor=None,
                note="Requester did not respond to the clarification request.",
            )

        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(cycle.cancelled_at)

    def test_stale_deactivated_officer_cannot_close_open_clarification(self):
        prr, cycle = self._open_clarification()
        get_user_model().objects.filter(pk=self.officer.pk).update(
            is_active=False
        )

        with self.assertRaises(PermissionDenied):
            services.transition_manually(
                prr,
                RequestStatus.CLOSED_ABANDONED,
                actor=self.officer,
                note="Requester did not respond to the clarification request.",
            )

        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(cycle.cancelled_at)
        self.assertIsNone(cycle.resolved_at)

    def test_stale_deactivated_officer_cannot_resolve_clarification(self):
        prr, cycle = self._open_clarification()
        inbound = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
            received_at=timezone.now(),
        )
        cycle.inbound_response_correspondence = inbound
        cycle.responded_at = timezone.now()
        cycle.save(
            update_fields=[
                "inbound_response_correspondence",
                "responded_at",
            ]
        )
        get_user_model().objects.filter(pk=self.officer.pk).update(
            is_active=False
        )

        with self.assertRaises(PermissionDenied):
            services.resolve_clarification_cycle(
                cycle,
                actor=self.officer,
                resolution_notes=(
                    "Scope confirmed as school years 2024 through 2026."
                ),
            )

        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(cycle.resolved_at)
        self.assertIsNone(cycle.resolved_by_id)
        self.assertIsNone(cycle.resolved_status_event_id)

    def test_every_staff_role_working_a_case_may_close_open_clarification(self):
        """A withdrawal is a fact about the requester, not a legal ruling.

        Reserving it for the Records Officer left finished cases open behind a
        queue for a signature that protected nothing. What keeps the closure
        accountable is the substantive reason and the audit entry naming who
        recorded it, and both are still required of every role here.
        """
        for role in (
            StaffRole.LIAISON,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        ):
            with self.subTest(role=role):
                actor = get_user_model().objects.create_user(
                    f"{role}-actor",
                    f"{role}@psd401.net",
                    "pw",
                    is_staff=True,
                )
                apply_staff_roles(actor, {role})
                prr, cycle = self._open_clarification()

                closed = services.close_case(
                    prr,
                    RequestStatus.CLOSED_WITHDRAWN,
                    actor=actor,
                    reason="Requester withdrew the request in a retained message.",
                )

                closed.refresh_from_db()
                cycle.refresh_from_db()
                self.assertEqual(closed.status, RequestStatus.CLOSED_WITHDRAWN)
                self.assertIsNotNone(cycle.cancelled_at)
                self.assertEqual(
                    closed.events.get(
                        from_status=RequestStatus.CLARIFICATION,
                        to_status=RequestStatus.CLOSED_WITHDRAWN,
                    ).actor,
                    actor,
                )

    def test_a_role_that_does_not_work_cases_cannot_close_one(self):
        actor = get_user_model().objects.create_user(
            "intake-clerk-actor",
            "intake-clerk@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(actor, {StaffRole.INTAKE_CLERK})
        prr, cycle = self._open_clarification()

        with self.assertRaises(PermissionDenied):
            services.close_case(
                prr,
                RequestStatus.CLOSED_WITHDRAWN,
                actor=actor,
                reason="Requester withdrew the request in a retained message.",
            )

        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(cycle.cancelled_at)

    def test_close_case_refuses_a_closure_with_its_own_workflow(self):
        for to_status in (
            RequestStatus.FULFILLED,
            RequestStatus.CLOSED_NON_REQUEST,
            RequestStatus.CLOSED_NO_RECORDS,
            RequestStatus.DENIED,
            RequestStatus.GATHERING,
        ):
            with self.subTest(to_status=to_status):
                prr, cycle = self._open_clarification()

                with self.assertRaises(ValidationError):
                    services.close_case(
                        prr,
                        to_status,
                        actor=self.officer,
                        reason="A substantive reason that is long enough.",
                    )

                prr.refresh_from_db()
                cycle.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
                self.assertIsNone(cycle.cancelled_at)

    def test_close_case_still_requires_a_substantive_reason(self):
        prr, _cycle = self._open_clarification()

        with self.assertRaises(services.ClarificationResolutionRequired):
            services.close_case(
                prr,
                RequestStatus.CLOSED_ABANDONED,
                actor=self.officer,
                reason="short",
            )

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)

    def test_close_case_closes_an_ordinary_working_case_with_a_reason(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Working Requester",
                email="working-requester@example.org",
            ),
            description="Ordinary working case narrative.",
            status=RequestStatus.GATHERING,
        )
        reason = "Requester withdrew by phone; call logged in the case notes."

        closed = services.close_case(
            prr,
            RequestStatus.CLOSED_WITHDRAWN,
            actor=self.officer,
            reason=reason,
        )

        closed.refresh_from_db()
        event = closed.events.get(
            from_status=RequestStatus.GATHERING,
            to_status=RequestStatus.CLOSED_WITHDRAWN,
        )
        self.assertEqual(closed.status, RequestStatus.CLOSED_WITHDRAWN)
        self.assertEqual(closed.closed_at, event.occurred_at)
        self.assertEqual(event.note, reason)
        self.assertEqual(event.actor, self.officer)

    def test_other_terminal_states_cannot_bypass_clarification_resolution(self):
        for terminal_status in (
            RequestStatus.FULFILLED,
            RequestStatus.DENIED,
        ):
            with self.subTest(terminal_status=terminal_status):
                prr, cycle = self._open_clarification()

                with self.assertRaises(
                    services.ClarificationResolutionRequired,
                ):
                    services.transition_manually(
                        prr,
                        terminal_status,
                        actor=self.officer,
                        note="Attempted terminal transition.",
                    )

                prr.refresh_from_db()
                cycle.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
                self.assertIsNone(cycle.cancelled_at)

    def test_canonical_transition_cannot_leave_an_open_clarification_cycle(self):
        for to_status in (
            RequestStatus.GATHERING,
            RequestStatus.FULFILLED,
            RequestStatus.CLOSED_WITHDRAWN,
            RequestStatus.CLOSED_ABANDONED,
            RequestStatus.DENIED,
        ):
            with self.subTest(to_status=to_status):
                prr, cycle = self._open_clarification()
                original_event_count = prr.events.count()

                with self.assertRaisesRegex(
                    services.ClarificationResolutionRequired,
                    "dedicated clarification",
                ):
                    services.transition(
                        prr,
                        to_status,
                        actor=self.officer,
                        note=(
                            "The Records Officer documented the attempted "
                            "clarification exit."
                        ),
                    )

                prr.refresh_from_db()
                cycle.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
                self.assertIsNone(cycle.resolved_at)
                self.assertIsNone(cycle.cancelled_at)
                self.assertEqual(prr.events.count(), original_event_count)

    def test_clarification_closure_rolls_back_status_event_and_cycle_together(self):
        prr, cycle = self._open_clarification()
        original_event_count = prr.events.count()

        with (
            mock.patch(
                "apps.records.services.audit",
                side_effect=[
                    None,
                    RuntimeError("audit store unavailable"),
                ],
            ) as mocked_audit,
            self.assertRaisesRegex(RuntimeError, "audit store unavailable"),
        ):
            services.transition_manually(
                prr,
                RequestStatus.CLOSED_WITHDRAWN,
                actor=self.officer,
                note="Requester withdrew the request in a retained message.",
            )

        self.assertEqual(mocked_audit.call_count, 2)
        prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(prr.closed_at)
        self.assertIsNone(cycle.cancelled_at)
        self.assertEqual(prr.events.count(), original_event_count)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class TerminalActionDomainTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "terminal-officer",
            "terminal-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.admin = User.objects.create_user(
            "terminal-admin",
            "terminal-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.integration = User.objects.create_user(
            "terminal-integration",
            password=None,
        )

    @staticmethod
    def make_request():
        return PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email=f"terminal-{timezone.now().timestamp()}@example.org",
            ),
            description="Terminal legal action domain test.",
        )

    def test_denial_can_only_be_applied_by_confirmed_correspondence_delivery(self):
        for actor in (None, self.integration, self.admin, self.officer):
            with self.subTest(actor=getattr(actor, "username", None)):
                prr = self.make_request()
                with self.assertRaisesRegex(
                    services.CorrespondenceDeliveryRequired,
                    "denial correspondence",
                ):
                    services.transition_manually(
                        prr,
                        RequestStatus.DENIED,
                        actor=actor,
                        note="The legal denial review is complete.",
                    )
                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)
                self.assertIsNone(prr.closed_at)
                self.assertFalse(prr.events.exists())

    def test_canonical_transition_cannot_bypass_confirmed_denial_delivery(self):
        for actor in (None, self.integration, self.admin, self.officer):
            with self.subTest(actor=getattr(actor, "username", None)):
                prr = self.make_request()
                with self.assertRaisesRegex(
                    services.CorrespondenceDeliveryRequired,
                    "confirmed denial correspondence delivery",
                ):
                    services.transition(
                        prr,
                        RequestStatus.DENIED,
                        actor=actor,
                        note="The legal denial review is complete.",
                    )
                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)
                self.assertIsNone(prr.closed_at)
                self.assertFalse(prr.events.exists())

    def test_canonical_transition_cannot_bypass_acknowledgment_or_clarification_delivery(
        self,
    ):
        for to_status in (
            RequestStatus.ACKNOWLEDGED,
            RequestStatus.CLARIFICATION,
        ):
            with self.subTest(to_status=to_status):
                prr = self.make_request()
                with self.assertRaisesRegex(
                    services.CorrespondenceDeliveryRequired,
                    "confirmed .* delivery",
                ):
                    services.transition(
                        prr,
                        to_status,
                        actor=self.officer,
                        note="Requester-facing correspondence was delivered.",
                        visible_to_requester=True,
                    )

                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)
                self.assertIsNone(prr.acknowledgment_sent_at)
                self.assertFalse(prr.events.exists())

    def test_record_acknowledgment_requires_confirmed_delivery_before_mutation(self):
        prr = self.make_request()

        with self.assertRaisesRegex(
            services.CorrespondenceDeliveryRequired,
            "confirmed acknowledgment delivery",
        ):
            services.record_acknowledgment(
                prr,
                actor=self.officer,
                note="Statutory acknowledgment delivered.",
            )

        prr.refresh_from_db()
        self.assertIsNone(prr.acknowledgment_sent_at)
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertFalse(prr.events.exists())

    def test_other_terminal_closures_require_officer_and_substantive_reason(self):
        reason = "The Records Officer documented the terminal case outcome."
        for actor in (None, self.integration, self.admin):
            with self.subTest(actor=getattr(actor, "username", None)):
                prr = self.make_request()
                with self.assertRaises(PermissionDenied):
                    services.transition_manually(
                        prr,
                        RequestStatus.FULFILLED,
                        actor=actor,
                        note=reason,
                    )
                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)
                self.assertFalse(prr.events.exists())

        prr = self.make_request()
        with self.assertRaisesRegex(
            services.TerminalActionRequired,
            "substantive",
        ):
            services.transition_manually(
                prr,
                RequestStatus.FULFILLED,
                actor=self.officer,
                note="done",
            )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertFalse(prr.events.exists())

        for closure_status in (
            RequestStatus.FULFILLED,
            RequestStatus.CLOSED_WITHDRAWN,
            RequestStatus.CLOSED_ABANDONED,
        ):
            with self.subTest(closure_status=closure_status):
                prr = self.make_request()
                closed = services.transition_manually(
                    prr,
                    closure_status,
                    actor=self.officer,
                    note=reason,
                )
                self.assertEqual(closed.status, closure_status)
                event = closed.events.get(to_status=closure_status)
                self.assertEqual(event.actor, self.officer)
                self.assertEqual(event.note, reason)

    def test_canonical_transition_enforces_terminal_officer_and_reason(self):
        reason = "The Records Officer documented the terminal case outcome."
        for actor in (None, self.integration, self.admin):
            with self.subTest(actor=getattr(actor, "username", None)):
                prr = self.make_request()
                with self.assertRaises(PermissionDenied):
                    services.transition(
                        prr,
                        RequestStatus.FULFILLED,
                        actor=actor,
                        note=reason,
                    )
                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)
                self.assertFalse(prr.events.exists())

        prr = self.make_request()
        with self.assertRaisesRegex(
            services.TerminalActionRequired,
            "substantive",
        ):
            services.transition(
                prr,
                RequestStatus.FULFILLED,
                actor=self.officer,
                note="done",
            )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertFalse(prr.events.exists())

        prr = self.make_request()
        closed = services.transition(
            prr,
            RequestStatus.FULFILLED,
            actor=self.officer,
            note=reason,
        )
        event = closed.events.get(to_status=RequestStatus.FULFILLED)
        self.assertEqual(closed.status, RequestStatus.FULFILLED)
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.note, reason)

    def test_stale_deactivated_officer_cannot_apply_terminal_action(self):
        prr = self.make_request()
        get_user_model().objects.filter(pk=self.officer.pk).update(
            is_active=False
        )

        with self.assertRaises(PermissionDenied):
            services.transition(
                prr,
                RequestStatus.FULFILLED,
                actor=self.officer,
                note="The Records Officer documented the fulfilled request.",
            )

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(prr.closed_at)
        self.assertFalse(prr.events.exists())

    def test_canonical_transition_rejects_unknown_status_before_side_effects(self):
        prr = self.make_request()

        with (
            mock.patch("apps.records.services.audit") as mocked_audit,
            self.assertRaisesRegex(ValidationError, "valid request status"),
        ):
            services.transition(
                prr,
                "denied ",
                actor=self.officer,
                note="The Records Officer documented the attempted outcome.",
            )

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(prr.closed_at)
        self.assertFalse(prr.events.exists())
        mocked_audit.assert_not_called()
