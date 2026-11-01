# ABOUTME: Locked case-owner assignment and append-only accountability evidence.
# ABOUTME: Ownership remains independent from legal status and work assignment.
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.records import services
from apps.records.models import PublicRecordsRequest, Requester, StatusEvent
from apps.work_queue.models import WorkItem


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CaseOwnerServiceTests(TestCase):
    def setUp(self):
        self.actor = self._officer("owner-actor")
        self.first_owner = self._officer("owner-first")
        self.second_owner = self._officer("owner-second")
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="owner@example.org"),
            description="Case-owner accountability fixture.",
            assigned_officer=self.first_owner,
        )

    @staticmethod
    def _officer(username):
        user = get_user_model().objects.create_user(
            username,
            email=f"{username}@psd401.net",
            is_staff=True,
        )
        apply_staff_roles(user, {StaffRole.RECORDS_OFFICER})
        return user

    def test_reassign_and_clear_append_exact_evidence_without_legal_side_effects(self):
        event_count = StatusEvent.objects.count()
        work_count = WorkItem.objects.count()

        services.assign_request_owner(
            self.prr,
            owner=self.second_owner,
            actor=self.actor,
        )
        services.assign_request_owner(
            self.prr,
            owner=None,
            actor=self.actor,
        )

        self.prr.refresh_from_db()
        self.assertIsNone(self.prr.assigned_officer_id)
        evidence = list(AuditLog.objects.filter(
            action="request.owner_changed",
            target=self.prr.number,
        ).order_by("at"))
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            evidence[0].detail["previous_owner_id"],
            self.first_owner.id,
        )
        self.assertEqual(
            evidence[0].detail["new_owner_id"],
            self.second_owner.id,
        )
        self.assertEqual(
            evidence[1].detail["previous_owner_id"],
            self.second_owner.id,
        )
        self.assertIsNone(evidence[1].detail["new_owner_id"])
        self.assertEqual(StatusEvent.objects.count(), event_count)
        self.assertEqual(WorkItem.objects.count(), work_count)

    def test_same_owner_retry_is_a_no_op(self):
        services.assign_request_owner(
            self.prr,
            owner=self.first_owner,
            actor=self.actor,
        )

        self.assertFalse(AuditLog.objects.filter(
            action="request.owner_changed",
            target=self.prr.number,
        ).exists())

    def test_current_actor_and_target_authority_are_rechecked_under_lock(self):
        self.second_owner.is_active = False
        self.second_owner.save(update_fields=["is_active"])
        with self.assertRaisesMessage(
            ValidationError,
            "active Records Officer",
        ):
            services.assign_request_owner(
                self.prr,
                owner=self.second_owner,
                actor=self.actor,
            )

        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        with self.assertRaisesMessage(
            PermissionDenied,
            "Current Records Officer authority",
        ):
            services.assign_request_owner(
                self.prr,
                owner=None,
                actor=self.actor,
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.assigned_officer_id, self.first_owner.id)
        self.assertFalse(AuditLog.objects.filter(
            action="request.owner_changed",
            target=self.prr.number,
        ).exists())

    def test_audit_failure_rolls_back_owner_projection(self):
        with (
            mock.patch(
                "apps.records.services.audit",
                side_effect=RuntimeError("audit unavailable"),
            ),
            self.assertRaisesMessage(RuntimeError, "audit unavailable"),
        ):
            services.assign_request_owner(
                self.prr,
                owner=self.second_owner,
                actor=self.actor,
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.assigned_officer_id, self.first_owner.id)
        self.assertEqual(AuditLog.objects.count(), 0)
