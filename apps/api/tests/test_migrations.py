from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class PrivilegedGroupSeparationMigrationTests(TransactionTestCase):
    migrate_from = ("api", "0001_staff_identity")
    migrate_to = ("api", "0002_separate_privileged_google_groups")

    def test_dual_privileged_group_fails_closed_with_audit_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Mapping = old_apps.get_model("api", "GoogleGroupRoleMapping")

        admin = Mapping.objects.create(
            role="admin",
            google_group="privileged-staff@psd401.net",
            active=True,
        )
        officer = Mapping.objects.create(
            role="records_officer",
            google_group="PRIVILEGED-STAFF@psd401.net",
            active=True,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        Mapping = current_apps.get_model("api", "GoogleGroupRoleMapping")
        AuditLog = current_apps.get_model("audit", "AuditLog")

        self.assertTrue(Mapping.objects.get(pk=admin.pk).active)
        self.assertFalse(Mapping.objects.get(pk=officer.pk).active)
        evidence = AuditLog.objects.get(
            action="identity.privileged_mapping_deactivated",
            target=str(officer.pk),
        )
        self.assertEqual(evidence.detail["role"], "records_officer")
        self.assertIn("separated", evidence.detail["reason"])

    def test_dual_privileged_group_revokes_materialized_legacy_authority(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model("auth", "User")
        Group = old_apps.get_model("auth", "Group")
        StaffIdentity = old_apps.get_model("api", "StaffIdentity")
        Mapping = old_apps.get_model("api", "GoogleGroupRoleMapping")

        user = User.objects.create(
            username="legacy-admin",
            email="legacy-admin@psd401.net",
            is_active=True,
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=user,
            source="google",
            google_subject="legacy-google-subject",
            last_group_check_at=timezone.now(),
        )
        admin_group = Group.objects.create(name="prr-role:admin")
        officer_group = Group.objects.create(name="prr-role:records_officer")
        user.groups.add(admin_group, officer_group)
        Mapping.objects.create(
            role="admin",
            google_group="privileged-staff@psd401.net",
            active=True,
        )
        Mapping.objects.create(
            role="records_officer",
            google_group="PRIVILEGED-STAFF@psd401.net",
            active=True,
        )
        session = SessionStore()
        session["_auth_user_id"] = str(user.pk)
        session.save()

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        User = current_apps.get_model("auth", "User")
        StaffIdentity = current_apps.get_model("api", "StaffIdentity")
        user = User.objects.get(pk=user.pk)
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {"prr-role:admin"},
        )
        self.assertIsNone(
            StaffIdentity.objects.get(user_id=user.pk).last_group_check_at
        )
        self.assertFalse(Session.objects.filter(session_key=session.session_key).exists())


class SharedPrivilegedGroupMigrationTests(TransactionTestCase):
    migrate_from = ("api", "0002_separate_privileged_google_groups")
    migrate_to = ("api", "0003_allow_shared_privileged_group")

    def test_removes_shared_privileged_group_database_constraint(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Mapping = old_apps.get_model("api", "GoogleGroupRoleMapping")
        Mapping.objects.create(
            role="admin",
            google_group="shared-build-team@psd401.net",
            active=True,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        Mapping = current_apps.get_model("api", "GoogleGroupRoleMapping")
        Mapping.objects.create(
            role="records_officer",
            google_group="shared-build-team@psd401.net",
            active=True,
        )

        self.assertEqual(
            Mapping.objects.filter(
                google_group="shared-build-team@psd401.net",
                active=True,
            ).count(),
            2,
        )
