# ABOUTME: Staff API tests for the district knowledge pack — Records Officer
# ABOUTME: only, validated entries, and an audit row for every edit (#35).
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.records.models import (
    KnowledgeNoiseSender,
    KnowledgeRoleAccount,
    KnowledgeSystemNote,
    RecordSystem,
)

ROLE_ACCOUNTS = "/api/staff/knowledge/role-accounts/"
NOISE_SENDERS = "/api/staff/knowledge/noise-senders/"
SYSTEM_NOTES = "/api/staff/knowledge/system-notes/"


def _rows(response):
    payload = response.json()
    return payload["results"] if isinstance(payload, dict) else payload


class KnowledgePackAccessTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(username="kp-officer", email="officer@psd401.net", is_staff=True)
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        # Explicit non-officer roles: a staff user with no role group passes
        # officer checks under the test settings.
        self.admin = get_user_model().objects.create(username="kp-admin", email="admin@psd401.net", is_staff=True)
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.liaison = get_user_model().objects.create(username="kp-liaison", email="liaison@psd401.net", is_staff=True)
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})

    def test_every_endpoint_refuses_anyone_but_a_records_officer(self):
        for label, user in (("admin", self.admin), ("liaison", self.liaison)):
            self.client.force_login(user)
            for path in (ROLE_ACCOUNTS, NOISE_SENDERS, SYSTEM_NOTES):
                with self.subTest(user=label, path=path):
                    self.assertEqual(self.client.get(path).status_code, 403)
                    self.assertEqual(
                        self.client.post(path, {}, content_type="application/json").status_code,
                        403,
                    )
        self.client.logout()
        self.assertEqual(self.client.get(ROLE_ACCOUNTS).status_code, 403)

    def test_a_records_officer_can_read_every_section(self):
        self.client.force_login(self.officer)
        for path in (ROLE_ACCOUNTS, NOISE_SENDERS, SYSTEM_NOTES):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


class KnowledgeRoleAccountTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(username="kp-officer", email="officer@psd401.net", is_staff=True)
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)

    def test_create_update_and_delete_are_each_audited(self):
        created = self.client.post(
            ROLE_ACCOUNTS,
            {
                "role": "Principal, Example Elementary",
                "account_email": "Principal.Example@PSD401.net",
                "starts_on": "2024-07-01",
                "ends_on": "2026-06-30",
                "note": "Interim for one year.",
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        row = KnowledgeRoleAccount.objects.get()
        self.assertEqual(row.account_email, "principal.example@psd401.net")
        event = AuditLog.objects.get(action="knowledge.role_account_created")
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.target, str(row.pk))
        self.assertEqual(event.detail["after"]["role"], "Principal, Example Elementary")

        updated = self.client.patch(
            f"{ROLE_ACCOUNTS}{row.pk}/",
            {"ends_on": "2025-06-30"},
            content_type="application/json",
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        event = AuditLog.objects.get(action="knowledge.role_account_updated")
        self.assertEqual(event.detail["before"]["ends_on"], "2026-06-30")
        self.assertEqual(event.detail["after"]["ends_on"], "2025-06-30")

        deleted = self.client.delete(f"{ROLE_ACCOUNTS}{row.pk}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(KnowledgeRoleAccount.objects.exists())
        event = AuditLog.objects.get(action="knowledge.role_account_deleted")
        self.assertEqual(event.detail["before"]["account_email"], "principal.example@psd401.net")

    def test_a_range_that_ends_before_it_starts_is_refused(self):
        response = self.client.post(
            ROLE_ACCOUNTS,
            {
                "role": "Director of Human Resources",
                "account_email": "hr.director@psd401.net",
                "starts_on": "2026-07-01",
                "ends_on": "2026-06-30",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("ends_on", response.json())
        self.assertFalse(AuditLog.objects.filter(action__startswith="knowledge.").exists())

    def test_an_open_ended_range_is_allowed(self):
        response = self.client.post(
            ROLE_ACCOUNTS,
            {"role": "Director of Human Resources", "account_email": "hr.director@psd401.net"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()["starts_on"])
        self.assertIsNone(response.json()["ends_on"])

    def test_a_blank_role_is_refused(self):
        response = self.client.post(
            ROLE_ACCOUNTS,
            {"role": "  ", "account_email": "hr.director@psd401.net"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.json())


class KnowledgeNoiseSenderTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(username="kp-officer", email="officer@psd401.net", is_staff=True)
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)

    def test_each_kind_is_normalized_and_audited(self):
        for kind, raw, expected in (
            ("domain", "@News.Example.org", "news.example.org"),
            ("address", "No-Reply@Example.org", "no-reply@example.org"),
            ("list", "all-staff.example.org", "all-staff.example.org"),
            ("list", "All-Staff@Example.org", "all-staff@example.org"),
        ):
            with self.subTest(kind=kind, raw=raw):
                response = self.client.post(
                    NOISE_SENDERS,
                    {"kind": kind, "value": raw, "note": "District mass mail."},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(response.json()["value"], expected)
        self.assertEqual(
            AuditLog.objects.filter(action="knowledge.noise_sender_created").count(), 4
        )

    def test_malformed_values_are_refused(self):
        for kind, raw in (
            ("domain", "not a domain"),
            ("domain", "someone@example.org"),
            ("address", "example.org"),
            ("list", "two words"),
        ):
            with self.subTest(kind=kind, raw=raw):
                response = self.client.post(
                    NOISE_SENDERS,
                    {"kind": kind, "value": raw},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("value", response.json())

    def test_a_duplicate_entry_is_refused(self):
        KnowledgeNoiseSender.objects.create(kind="domain", value="news.example.org")
        response = self.client.post(
            NOISE_SENDERS,
            {"kind": "domain", "value": "NEWS.example.org"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_update_and_delete_are_audited(self):
        row = KnowledgeNoiseSender.objects.create(kind="domain", value="news.example.org")
        response = self.client.patch(
            f"{NOISE_SENDERS}{row.pk}/",
            {"active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        event = AuditLog.objects.get(action="knowledge.noise_sender_updated")
        self.assertTrue(event.detail["before"]["active"])
        self.assertFalse(event.detail["after"]["active"])
        self.assertEqual(self.client.delete(f"{NOISE_SENDERS}{row.pk}/").status_code, 204)
        self.assertTrue(
            AuditLog.objects.filter(action="knowledge.noise_sender_deleted").exists()
        )


class KnowledgeSystemNoteTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(username="kp-officer", email="officer@psd401.net", is_staff=True)
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)
        self.system = RecordSystem.objects.create(slug="google-workspace", name="Google Workspace")

    def test_a_note_is_kept_per_system_and_audited(self):
        response = self.client.post(
            SYSTEM_NOTES,
            {
                "record_system_id": self.system.pk,
                "notes": "Search the building mailbox, not every staff account.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["record_system_slug"], "google-workspace")
        note = KnowledgeSystemNote.objects.get()
        event = AuditLog.objects.get(action="knowledge.system_note_created")
        self.assertEqual(event.detail["after"]["record_system"], "google-workspace")

        second = self.client.post(
            SYSTEM_NOTES,
            {"record_system_id": self.system.pk, "notes": "Another note."},
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 400)

        updated = self.client.patch(
            f"{SYSTEM_NOTES}{note.pk}/",
            {"notes": "Search the building mailbox first."},
            content_type="application/json",
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        event = AuditLog.objects.get(action="knowledge.system_note_updated")
        self.assertIn("not every staff account", event.detail["before"]["notes"])

        self.assertEqual(self.client.delete(f"{SYSTEM_NOTES}{note.pk}/").status_code, 204)
        self.assertTrue(
            AuditLog.objects.filter(action="knowledge.system_note_deleted").exists()
        )

    def test_a_note_cannot_move_to_another_system(self):
        other = RecordSystem.objects.create(slug="bus-video", name="School Bus Video")
        note = KnowledgeSystemNote.objects.create(record_system=self.system, notes="First.")
        response = self.client.patch(
            f"{SYSTEM_NOTES}{note.pk}/",
            {"record_system_id": other.pk, "notes": "Moved."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        note.refresh_from_db()
        self.assertEqual(note.record_system, self.system)
        self.assertEqual(note.notes, "Moved.")

    def test_a_blank_note_is_refused(self):
        response = self.client.post(
            SYSTEM_NOTES,
            {"record_system_id": self.system.pk, "notes": "   "},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
