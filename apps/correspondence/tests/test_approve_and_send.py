"""Approving and sending are one officer decision, not two clicks.

``approve_draft`` and ``queue_approved`` both call ``_require_records_officer``.
There is no workflow in which a reviewer approves and an officer then releases
-- the role check forbids it -- so the second click was ceremony, and it read
to the officer as the app declining to do what it had just been told.

These tests hold the collapse honest: one call still produces both audited
writes with both attributions, and it still refuses everything the two-step
refused.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings

from apps.api.identity import apply_staff_roles
from apps.correspondence import services
from apps.correspondence.models import Correspondence, CorrespondenceDelivery

from .test_services import CorrespondenceServiceTestCase


@override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True)
class ApproveAndSendTests(CorrespondenceServiceTestCase):
    def _draft(self):
        return services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="A message the officer means to send",
            body="One decision, one action.",
            actor=self.officer,
        )

    def test_one_call_approves_and_queues_with_both_attributions(self):
        draft = self._draft()

        delivery = services.approve_and_send(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.QUEUED)
        # Both writes still happened and both still name who did them, so the
        # correspondence record is no thinner than it was under two clicks.
        self.assertEqual(draft.approved_by, self.officer)
        self.assertEqual(draft.queued_by, self.officer)
        self.assertIsNotNone(draft.approved_at)
        self.assertIsNotNone(draft.queued_at)
        self.assertIsInstance(delivery, CorrespondenceDelivery)

    def test_both_audit_events_are_still_written(self):
        from apps.audit.models import AuditLog

        draft = self._draft()
        services.approve_and_send(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        actions = set(
            AuditLog.objects.filter(target=self.prr.number).values_list(
                "action",
                flat=True,
            )
        )
        self.assertIn("correspondence.approved", actions)
        self.assertIn("correspondence.queued", actions)

    def test_a_non_officer_cannot_send(self):
        draft = self._draft()
        apply_staff_roles(self.officer, set())

        with self.assertRaises(PermissionDenied):
            services.approve_and_send(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_a_stale_version_is_still_refused(self):
        draft = self._draft()

        with self.assertRaises(ValidationError):
            services.approve_and_send(
                draft,
                actor=self.officer,
                expected_version=draft.version + 5,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    def test_disabled_transport_fails_before_approving_not_after(self):
        """The officer must not be left with a silently rolled-back approval."""
        draft = self._draft()

        with self.assertRaises(services.OutboundTransportDisabled):
            services.approve_and_send(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertIsNone(draft.approved_at)

    def test_the_separate_primitives_still_work_for_agents_and_recovery(self):
        """Collapsing the GUI must not remove either half from the API."""
        draft = self._draft()
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)

        services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.QUEUED)
