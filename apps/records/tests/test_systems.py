"""System registry + routing tests — the pre-checked boxes."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.records import triage
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
)


def make_request(description: str):
    r = Requester.objects.create(email="jane@example.org")
    return PublicRecordsRequest.objects.create(requester=r, description=description)


class SeedTests(TestCase):
    def test_seed_populates_all_discovered_systems(self):
        call_command("seed_systems")
        slugs = set(RecordSystem.objects.values_list("slug", flat=True))
        for expected in ("google-workspace", "avigilon-alta", "bus-video",
                         "powerschool-sis", "schoology", "qmlativ", "parentsquare",
                         "busright", "safety-pass", "classlink", "schoolinks",
                         # gap-analysis additions
                         "personal-devices", "board-records", "athletics",
                         "special-education", "health-services",
                         "nutrition-services", "hr-hiring", "capital-projects",
                         "safety-security", "legal-claims", "phone-voip",
                         "social-media", "kghp-radio", "paper-records"):
            self.assertIn(expected, slugs)
        # Bus video is flagged as a retention race until verified
        bus = RecordSystem.objects.get(slug="bus-video")
        self.assertTrue(bus.urgent_hold)
        self.assertTrue(bus.needs_verification)
        self.assertIn("VERIFY", bus.retention_note)

    def test_reseed_preserves_district_custodian_assignments(self):
        call_command("seed_systems")
        bus = RecordSystem.objects.get(slug="bus-video")
        bus.custodian_email = "transport.super@psd401.net"
        bus.retention_days = 14
        bus.needs_verification = False
        bus.save()
        call_command("seed_systems")   # re-run must not clobber
        bus.refresh_from_db()
        self.assertEqual(bus.custodian_email, "transport.super@psd401.net")
        self.assertEqual(bus.retention_days, 14)
        self.assertFalse(bus.needs_verification)


class KeywordMatchTests(TestCase):
    def test_phrase_and_word_boundary_matching(self):
        sys = RecordSystem(triage_keywords=["bus video", "gps"])
        self.assertTrue(sys.matches("I want the BUS VIDEO from route 12"))
        self.assertTrue(sys.matches("provide GPS records for the bus"))
        self.assertFalse(sys.matches("the gypsum order invoices"))   # no 'gps' word

    def test_plural_forms_of_single_word_keywords_match(self):
        sys = RecordSystem(triage_keywords=["invoice", "contract"])
        self.assertTrue(sys.matches("Copies of all invoices from Acme Paving"))
        self.assertTrue(sys.matches("all vendor contracts signed in 2025"))
        self.assertFalse(sys.matches("the invoicing department org chart"))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class RoutingTests(TestCase):
    def setUp(self):
        call_command("seed_systems")

    @mock.patch("apps.records.triage.notifications.notify_collection_task")
    def test_routing_retry_does_not_duplicate_tasks_or_notifications(
        self,
        notify_collection_task,
    ):
        RecordSystem.objects.update(active=False)
        system = RecordSystem.objects.create(
            slug="retry-safe-system",
            name="Retry-safe system",
            triage_keywords=["retry-sentinel"],
            active=True,
        )
        prr = make_request("Records matching retry-sentinel")

        first = triage.route_to_systems(prr)
        second = triage.route_to_systems(prr)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(
            CollectionTask.objects.filter(request=prr, system=system).count(),
            1,
        )
        notify_collection_task.assert_called_once_with(first[0])

    @mock.patch("apps.records.triage.notifications.notify_collection_task")
    def test_bus_video_request_creates_urgent_task_notification(
        self,
        notify_collection_task,
    ):
        prr = make_request(
            "Requesting the school bus video from route 12 on the afternoon of "
            "the incident last Tuesday."
        )
        tasks = triage.route_to_systems(prr)

        bus_tasks = [t for t in tasks if t.system.slug == "bus-video"]
        self.assertEqual(len(bus_tasks), 1)
        task = bus_tasks[0]
        self.assertEqual(task.kind, CollectionTask.Kind.MANUAL)
        self.assertIn("Transportation Supervisor", task.instructions)
        self.assertIn("Checklist:", task.instructions)

        notify_collection_task.assert_called_once_with(task)

    def test_collection_task_records_durable_handoff_evidence(self):
        RecordSystem.objects.update(active=False)
        system = RecordSystem.objects.create(
            slug="private-system",
            name="PRIVATE System Title",
            custodian_title="PRIVATE Custodian Title",
            custodian_email="private.custodian@psd401.net",
            triage_keywords=["sentinel-match"],
            active=True,
        )
        prr = make_request(
            "PRIVATE requester narrative with sentinel-match and sensitive details"
        )

        created = triage.route_to_systems(prr)

        self.assertEqual(len(created), 1)
        task = created[0]
        self.assertEqual(task.system, system)
        self.assertEqual(task.state, CollectionTask.State.PENDING)
        self.assertTrue(
            prr.events.filter(note__icontains=system.name).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="triage.system_matched",
                target=prr.number,
                detail__system=system.slug,
            ).exists()
        )

    @mock.patch("apps.records.triage.notifications.notify_collection_task")
    def test_routing_assigns_the_system_default_staff_owner(
        self,
        _notify_collection_task,
    ):
        owner = get_user_model().objects.create_user(
            "records-liaison",
            "records-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        RecordSystem.objects.update(active=False)
        system = RecordSystem.objects.create(
            slug="assigned-system",
            name="Assigned system",
            default_assignee=owner,
            triage_keywords=["assignment-sentinel"],
            active=True,
        )

        task = triage.route_to_systems(
            make_request("Find records matching assignment-sentinel")
        )[0]

        self.assertEqual(task.system, system)
        self.assertEqual(task.assigned_to, owner)

    @mock.patch("apps.records.triage.notifications.notify_collection_task")
    def test_custodian_email_receives_urgent_when_assigned(
        self,
        notify_collection_task,
    ):
        bus = RecordSystem.objects.get(slug="bus-video")
        bus.custodian_email = "transport.super@psd401.net"
        bus.save()
        prr = make_request("bus camera footage from yesterday please")
        created = triage.route_to_systems(prr)
        bus_task = next(task for task in created if task.system_id == bus.id)
        notify_collection_task.assert_any_call(bus_task)

    @mock.patch("apps.records.triage.notifications.notify_collection_task")
    def test_hr_request_routes_to_qmlativ_without_urgency(
        self,
        notify_collection_task,
    ):
        prr = make_request("Copies of the superintendent's contract and all "
                           "invoices from Acme Consulting in 2025.")
        tasks = triage.route_to_systems(prr)
        slugs = {t.system.slug for t in tasks}
        self.assertIn("qmlativ", slugs)
        qmlativ_task = next(
            task for task in tasks if task.system.slug == "qmlativ"
        )
        self.assertFalse(qmlativ_task.system.urgent_hold)
        notify_collection_task.assert_any_call(qmlativ_task)

    def test_multi_system_request_creates_multiple_tasks(self):
        prr = make_request("All ParentSquare announcements about bus routes, plus "
                           "the GPS records for bus 7.")
        tasks = triage.route_to_systems(prr)
        slugs = {t.system.slug for t in tasks}
        self.assertIn("parentsquare", slugs)
        self.assertIn("busright", slugs)

    def test_texts_request_routes_to_personal_device_procedure(self):
        prr = make_request("All text messages between the principal and Coach "
                           "Smith about tryouts.")
        tasks = triage.route_to_systems(prr)
        slugs = {t.system.slug for t in tasks}
        self.assertIn("personal-devices", slugs)
        self.assertIn("athletics", slugs)
        pd = next(t for t in tasks if t.system.slug == "personal-devices")
        self.assertIn("affidavit", pd.instructions)
        self.assertIn("Nissen", pd.system.retention_note)

    def test_settlement_request_routes_to_legal(self):
        prr = make_request("Copy of the settlement agreement in the Johnson matter.")
        slugs = {t.system.slug for t in triage.route_to_systems(prr)}
        self.assertIn("legal-claims", slugs)

    def test_unrelated_request_creates_no_tasks(self):
        prr = make_request("Meeting minutes of the board retreat.")
        self.assertEqual(triage.route_to_systems(prr), [])

    @mock.patch("apps.integrations.avigilon.client.list_cameras", return_value=[])
    def test_bus_video_also_triggers_building_camera_hold(self, m_list):
        """Preservation-first: 'video' keywords fire the Avigilon hold too;
        over-holding is safe, humans narrow."""
        import datetime as dt

        from django.utils import timezone
        day = (timezone.localdate() - dt.timedelta(days=2)).isoformat()
        prr = make_request(f"bus video from {day}")
        fp = triage.triage_footage(prr)
        self.assertIsNotNone(fp)
