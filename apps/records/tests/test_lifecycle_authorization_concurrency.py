# ABOUTME: PostgreSQL concurrency coverage for Records Officer authorization.
# ABOUTME: Legal actions serialize with managed-role membership revocation.
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings

from apps.api.identity import apply_staff_roles, role_group_name
from apps.api.models import StaffRole
from apps.records import services
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


@skipUnless(
    connection.vendor == "postgresql",
    "Lifecycle authorization concurrency requires PostgreSQL row locks.",
)
@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class RecordsOfficerAuthorizationConcurrencyTests(TransactionTestCase):
    """Exercise the guard shared by terminal and clarification actions."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "concurrent-records-officer",
            "concurrent-records-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(
            self.officer,
            {StaffRole.RECORDS_OFFICER},
        )
        self.officer_group = self.officer.groups.get(
            name=role_group_name(StaffRole.RECORDS_OFFICER)
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email="concurrent-authorization@example.org",
            ),
            description=(
                "Deterministic Records Officer authorization race test."
            ),
        )

    def _wait_until_blocked_by(
        self,
        *,
        blocked_pid,
        blocker_pid,
        unexpectedly_completed,
        timeout=10,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if unexpectedly_completed.is_set():
                self.fail(
                    "The concurrent transaction completed without waiting "
                    "for the Records Officer authorization lock."
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        wait_event_type = 'Lock'
                        AND %s = ANY(pg_blocking_pids(%s))
                    FROM pg_stat_activity
                    WHERE pid = %s
                    """,
                    [blocker_pid, blocked_pid, blocked_pid],
                )
                row = cursor.fetchone()
            if row and row[0]:
                return
        self.fail(
            "The concurrent transaction did not block on the Records "
            "Officer authorization lock."
        )

    def _delete_officer_membership(self):
        membership_model = self.officer.groups.through
        membership_model.objects.filter(
            user_id=self.officer.pk,
            group_id=self.officer_group.pk,
        ).delete()

    def test_revocation_waits_when_terminal_action_authorizes_first(self):
        action_authorized = threading.Event()
        allow_action_commit = threading.Event()
        revocation_started = threading.Event()
        revocation_committed = threading.Event()
        backend_pids = {}

        def audit_gate(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pids["action"] = cursor.fetchone()[0]
            action_authorized.set()
            if not allow_action_commit.wait(timeout=10):
                raise RuntimeError(
                    "Timed out waiting to commit the terminal action."
                )

        def complete_terminal_action():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.officer.pk)
                prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
                return services.transition_manually(
                    prr,
                    RequestStatus.FULFILLED,
                    actor=actor,
                    note=(
                        "Records Officer documented the fulfilled request."
                    ),
                )
            finally:
                close_old_connections()

        def revoke_role():
            close_old_connections()
            try:
                if not action_authorized.wait(timeout=10):
                    raise RuntimeError(
                        "Timed out waiting for terminal authorization."
                    )
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        backend_pids["revocation"] = cursor.fetchone()[0]
                    revocation_started.set()
                    self._delete_officer_membership()
                revocation_committed.set()
            finally:
                close_old_connections()

        with (
            mock.patch(
                "apps.records.services.audit",
                side_effect=audit_gate,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            action = executor.submit(complete_terminal_action)
            revocation = executor.submit(revoke_role)
            try:
                self.assertTrue(action_authorized.wait(timeout=10))
                self.assertTrue(revocation_started.wait(timeout=10))
                self._wait_until_blocked_by(
                    blocked_pid=backend_pids["revocation"],
                    blocker_pid=backend_pids["action"],
                    unexpectedly_completed=revocation_committed,
                )
                self.assertFalse(revocation_committed.is_set())
            finally:
                allow_action_commit.set()

            closed = action.result(timeout=20)
            revocation.result(timeout=20)

        closed.refresh_from_db()
        self.assertEqual(closed.status, RequestStatus.FULFILLED)
        self.assertTrue(revocation_committed.is_set())
        self.assertFalse(
            self.officer.groups.filter(pk=self.officer_group.pk).exists()
        )

    def test_terminal_action_waits_and_denies_when_revocation_wins(self):
        revocation_pending = threading.Event()
        allow_revocation_commit = threading.Event()
        action_started = threading.Event()
        action_completed = threading.Event()
        backend_pids = {}

        def revoke_role():
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        backend_pids["revocation"] = cursor.fetchone()[0]
                    self._delete_officer_membership()
                    revocation_pending.set()
                    if not allow_revocation_commit.wait(timeout=10):
                        raise RuntimeError(
                            "Timed out waiting to commit role revocation."
                        )
            finally:
                close_old_connections()

        def attempt_terminal_action():
            close_old_connections()
            try:
                if not revocation_pending.wait(timeout=10):
                    raise RuntimeError(
                        "Timed out waiting for pending role revocation."
                    )
                actor = get_user_model().objects.get(pk=self.officer.pk)
                prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pids["action"] = cursor.fetchone()[0]
                action_started.set()
                return services.transition_manually(
                    prr,
                    RequestStatus.FULFILLED,
                    actor=actor,
                    note=(
                        "Records Officer documented the fulfilled request."
                    ),
                )
            finally:
                action_completed.set()
                close_old_connections()

        with (
            mock.patch("apps.records.services.audit"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            revocation = executor.submit(revoke_role)
            action = executor.submit(attempt_terminal_action)
            try:
                self.assertTrue(revocation_pending.wait(timeout=10))
                self.assertTrue(action_started.wait(timeout=10))
                self._wait_until_blocked_by(
                    blocked_pid=backend_pids["action"],
                    blocker_pid=backend_pids["revocation"],
                    unexpectedly_completed=action_completed,
                )
                self.assertFalse(action_completed.is_set())
            finally:
                allow_revocation_commit.set()

            revocation.result(timeout=20)
            with self.assertRaises(PermissionDenied):
                action.result(timeout=20)

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertFalse(self.prr.events.exists())
        self.assertFalse(
            self.officer.groups.filter(pk=self.officer_group.pk).exists()
        )

    def test_owner_assignment_waits_and_denies_when_target_revocation_wins(self):
        target = get_user_model().objects.create_user(
            "concurrent-owner-target",
            "concurrent-owner-target@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(target, {StaffRole.RECORDS_OFFICER})
        target_group = target.groups.get(
            name=role_group_name(StaffRole.RECORDS_OFFICER)
        )
        revocation_pending = threading.Event()
        allow_revocation_commit = threading.Event()
        assignment_started = threading.Event()
        assignment_completed = threading.Event()
        backend_pids = {}

        def revoke_target_role():
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        backend_pids["revocation"] = cursor.fetchone()[0]
                    target.groups.through.objects.filter(
                        user_id=target.pk,
                        group_id=target_group.pk,
                    ).delete()
                    revocation_pending.set()
                    if not allow_revocation_commit.wait(timeout=10):
                        raise RuntimeError(
                            "Timed out waiting to commit owner-role revocation."
                        )
            finally:
                close_old_connections()

        def attempt_owner_assignment():
            close_old_connections()
            try:
                if not revocation_pending.wait(timeout=10):
                    raise RuntimeError(
                        "Timed out waiting for pending owner-role revocation."
                    )
                actor = get_user_model().objects.get(pk=self.officer.pk)
                owner = get_user_model().objects.get(pk=target.pk)
                prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pids["assignment"] = cursor.fetchone()[0]
                assignment_started.set()
                return services.assign_request_owner(
                    prr,
                    owner=owner,
                    actor=actor,
                )
            finally:
                assignment_completed.set()
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            revocation = executor.submit(revoke_target_role)
            assignment = executor.submit(attempt_owner_assignment)
            try:
                self.assertTrue(revocation_pending.wait(timeout=10))
                self.assertTrue(assignment_started.wait(timeout=10))
                self._wait_until_blocked_by(
                    blocked_pid=backend_pids["assignment"],
                    blocker_pid=backend_pids["revocation"],
                    unexpectedly_completed=assignment_completed,
                )
                self.assertFalse(assignment_completed.is_set())
            finally:
                allow_revocation_commit.set()

            revocation.result(timeout=20)
            with self.assertRaises(ValidationError):
                assignment.result(timeout=20)

        self.prr.refresh_from_db()
        self.assertIsNone(self.prr.assigned_officer_id)
        self.assertFalse(target.groups.filter(pk=target_group.pk).exists())
