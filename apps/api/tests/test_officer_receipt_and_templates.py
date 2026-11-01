# ABOUTME: Staff API for the officer-triggered courtesy receipt, the editable
# ABOUTME: template catalog and prefill, the guided statutory plan, and the
# ABOUTME: awaiting-statutory-response Inbox queue.
import datetime as dt
import inspect
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api.capabilities import CAPABILITIES
from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.resources.correspondence import PREFILL_CREATE_ACTIONS
from apps.correspondence import services as correspondence_services
from apps.correspondence.automatic_templates import (
    FREE_FORM_TEMPLATE_KINDS,
    TEMPLATE_SPECS,
)
from apps.correspondence.models import Correspondence
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)
from apps.work_queue import source_reconciliation


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class OfficerCorrespondenceApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "receipt-api-officer",
            "receipt-api-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "receipt-api-reviewer",
            "receipt-api-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jane Public",
                email="jane@example.org",
            ),
            description="All records about the capital project.",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            statutory_response_due=dt.date(2026, 8, 17),
        )
        self.client.force_login(self.officer)

    def test_officer_may_draft_the_courtesy_receipt(self):
        response = self.client.post(
            "/api/staff/correspondence/drafts/submission-receipt/",
            {"request_number": self.request.number},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload["kind"], "submission_receipt")
        self.assertEqual(payload["status"], "draft")
        self.assertFalse(payload["automated"])
        self.request.refresh_from_db()
        self.assertFalse(self.request.initial_response_delivered)
        self.assertEqual(self.request.status, RequestStatus.RECEIVED)

    def test_a_second_courtesy_receipt_is_refused(self):
        first = self.client.post(
            "/api/staff/correspondence/drafts/submission-receipt/",
            {"request_number": self.request.number},
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 201)

        second = self.client.post(
            "/api/staff/correspondence/drafts/submission-receipt/",
            {"request_number": self.request.number},
            content_type="application/json",
        )

        self.assertEqual(second.status_code, 400)
        self.assertIn("submission receipt", str(second.json()))

    def test_the_courtesy_receipt_requires_a_drafting_role(self):
        self.client.force_login(self.reviewer)

        response = self.client.post(
            "/api/staff/correspondence/drafts/submission-receipt/",
            {"request_number": self.request.number},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_prefill_returns_an_editable_starting_point(self):
        response = self.client.get(
            "/api/staff/correspondence/template-prefill/",
            {"request_number": self.request.number, "kind": "general"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn(self.request.number, payload["subject"])
        self.assertIn("Public Records Officer", payload["body"])

    def test_prefill_rejects_a_structured_kind(self):
        response = self.client.get(
            "/api/staff/correspondence/template-prefill/",
            {
                "request_number": self.request.number,
                "kind": "acknowledgment",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_prefill_offers_exactly_the_free_form_kinds(self):
        for kind in FREE_FORM_TEMPLATE_KINDS:
            with self.subTest(kind=kind):
                response = self.client.get(
                    "/api/staff/correspondence/template-prefill/",
                    {"request_number": self.request.number, "kind": kind},
                )
                self.assertEqual(response.status_code, 200, kind)

    def test_prefill_role_matches_the_role_that_may_create_the_draft(self):
        """A drafting aid must not out-reach the draft it leads to."""
        liaison = get_user_model().objects.create_user(
            "prefill-liaison",
            "prefill-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            assigned_to=liaison,
            instructions="Search the mailbox.",
        )
        self.client.force_login(liaison)

        for kind, expected in (
            (Correspondence.Kind.GENERAL, 200),
            (Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE, 200),
            (Correspondence.Kind.CLOSURE, 403),
            (Correspondence.Kind.DENIAL, 403),
        ):
            with self.subTest(kind=kind):
                response = self.client.get(
                    "/api/staff/correspondence/template-prefill/",
                    {"request_number": self.request.number, "kind": kind},
                )
                self.assertEqual(response.status_code, expected, kind)

    def test_every_free_form_kind_declares_its_creating_action(self):
        """Adding a free-form kind must not silently skip the role check."""
        self.assertEqual(
            set(PREFILL_CREATE_ACTIONS),
            set(FREE_FORM_TEMPLATE_KINDS),
        )

    def test_new_primitives_are_registered_capabilities(self):
        registered = {capability["id"] for capability in CAPABILITIES}
        self.assertIn("correspondence.draft_submission_receipt", registered)
        self.assertIn("correspondence.template_prefill", registered)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class EditableTemplateCatalogApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "catalog-officer",
            "catalog-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)

    def test_catalog_lists_every_editable_kind_with_its_family(self):
        response = self.client.get("/api/staff/automatic-message-templates/")

        self.assertEqual(response.status_code, 200, response.content)
        slots = {slot["kind"]: slot for slot in response.json()["templates"]}
        self.assertSetEqual(set(slots), set(TEMPLATE_SPECS))
        self.assertEqual(slots["general"]["authoring"], "free_form")
        self.assertEqual(slots["acknowledgment"]["authoring"], "structured")
        for slot in slots.values():
            self.assertTrue(slot["group"])
            self.assertTrue(slot["active"]["subject_template"])
            self.assertTrue(slot["active"]["template_version"])

    def test_every_kind_previews_against_the_sample_context(self):
        """The sample context must supply every structured token."""
        for kind, spec in TEMPLATE_SPECS.items():
            with self.subTest(kind=kind):
                created = self.client.post(
                    "/api/staff/automatic-message-templates/",
                    {
                        "kind": kind,
                        "subject_template": spec.subject_template,
                        "body_template": spec.body_template,
                        "change_reason": (
                            "Confirm the district can preview this wording."
                        ),
                    },
                    content_type="application/json",
                )
                self.assertEqual(created.status_code, 201, created.content)
                preview = self.client.post(
                    "/api/staff/automatic-message-templates/"
                    f"{created.json()['id']}/preview/",
                    {},
                    content_type="application/json",
                )
                self.assertEqual(preview.status_code, 200, preview.content)
                body = preview.json()["body"]
                self.assertNotIn("{{", body)
                self.assertIn("PRR-2026-0001", preview.json()["subject"])


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class GuidedStatutoryResponseTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "guided-officer",
            "guided-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jane Public",
                email="jane@example.org",
            ),
            description="All records about the capital project.",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            statutory_response_due=dt.date(2026, 8, 17),
        )
        self.client.force_login(self.officer)

    def plan(self):
        response = self.client.get(
            f"/api/staff/requests/{self.request.number}/"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["initial_response_plan"]

    def test_every_plan_state_has_explicit_guidance(self):
        """A new plan state must not silently fall back to generic guidance."""
        from apps.api.resources import requests as request_resources

        source = inspect.getsource(
            request_resources.compute_initial_response_plan
        )
        states = set(re.findall(r'plan\["state"\] = "([a-z_]+)"', source))
        states.update(re.findall(r'"state": "([a-z_]+)"', source))
        states.add("delivered")
        self.assertTrue(states)
        self.assertSetEqual(
            states.difference(request_resources._INITIAL_RESPONSE_GUIDANCE),
            set(),
        )

    def test_plan_guides_the_officer_to_prepare_a_draft_first(self):
        plan = self.plan()

        self.assertIn("acknowledgment", plan["next_action"].lower())
        self.assertEqual(
            [step["key"] for step in plan["steps"]],
            ["prepare", "approve", "queue", "deliver"],
        )
        self.assertEqual(plan["steps"][0]["state"], "current")
        self.assertEqual(
            {step["state"] for step in plan["steps"][1:]},
            {"pending"},
        )

    def test_a_draft_advances_the_guide_without_satisfying_the_deadline(self):
        correspondence_services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )

        plan = self.plan()

        self.assertEqual(plan["state"], "human_draft")
        self.assertEqual(plan["steps"][0]["state"], "done")
        self.assertEqual(plan["steps"][1]["state"], "current")
        self.assertIn("does not satisfy", plan["next_action"])
        self.request.refresh_from_db()
        self.assertFalse(self.request.initial_response_delivered)

    def test_an_approval_advances_the_guide_to_queueing(self):
        draft = correspondence_services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )
        correspondence_services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        plan = self.plan()

        self.assertEqual(plan["state"], "approved_not_queued")
        self.assertEqual(
            [step["state"] for step in plan["steps"]],
            ["done", "done", "current", "pending"],
        )

    def test_the_case_header_carries_the_same_next_action(self):
        response = self.client.get(
            f"/api/staff/requests/{self.request.number}/workspace-summary/"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["statutory_response"]["next_action"],
            self.plan()["next_action"],
        )


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class AwaitingStatutoryResponseQueueTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "statutory-queue-officer",
            "statutory-queue-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)

    def make_request(self, *, due, description):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jane Public",
                email=f"jane+{due.isoformat()}@example.org",
            ),
            description=description,
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            statutory_response_due=due,
        )
        source_reconciliation.reconcile_request_work(prr)
        return prr

    def test_queue_lists_undelivered_requests_soonest_due_first(self):
        later = self.make_request(
            due=dt.date(2026, 8, 20),
            description="Later request about buses.",
        )
        sooner = self.make_request(
            due=dt.date(2026, 8, 14),
            description="Sooner request about buses.",
        )

        response = self.client.get(
            "/api/staff/work-items/"
            "?queue=inbox_awaiting_statutory_response"
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        self.assertEqual(
            [row["request_number"] for row in rows],
            [sooner.number, later.number],
        )
        self.assertEqual(rows[0]["urgency_date"], "2026-08-14")

    def test_counts_include_the_statutory_backlog(self):
        self.make_request(
            due=dt.date(2026, 8, 14),
            description="A request awaiting its statutory response.",
        )

        response = self.client.get("/api/staff/work-items/counts/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["inbox_awaiting_statutory_response"],
            1,
        )

    def test_a_delivered_initial_response_leaves_the_queue(self):
        prr = self.make_request(
            due=dt.date(2026, 8, 14),
            description="A request that gets acknowledged.",
        )
        prr.acknowledgment_sent_at = dt.datetime(
            2026, 8, 12, 16, 0, tzinfo=dt.UTC
        )
        prr.save(update_fields=["acknowledgment_sent_at"])
        source_reconciliation.reconcile_request_work(prr)

        response = self.client.get(
            "/api/staff/work-items/"
            "?queue=inbox_awaiting_statutory_response"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CrossCaseTriageContentTests(TestCase):
    """Decision 1.2: officers get cross-case subject/snippet; nobody else does."""

    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "triage-officer",
            "triage-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "triage-liaison",
            "triage-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jane Public",
                email="jane@example.org",
            ),
            description="All records about the capital project.",
            received_at=dt.datetime(2026, 8, 10, 16, 0, tzinfo=dt.UTC),
            statutory_response_due=dt.date(2026, 8, 17),
            assigned_officer=self.officer,
        )
        # A liaison only reaches a case through assigned work.
        CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Search the capital-project shared drive.",
            assigned_to=self.liaison,
        )
        Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: bus camera footage request",
            received_body="Following up on the afternoon route footage.",
            received_sender="jane@example.org",
            received_recipients=["publicrecords@psd401.net"],
            received_at=dt.datetime(2026, 8, 11, 16, 0, tzinfo=dt.UTC),
            matched_at=dt.datetime(2026, 8, 11, 16, 1, tzinfo=dt.UTC),
        )

    def rows(self, user, query=""):
        self.client.force_login(user)
        response = self.client.get(f"/api/staff/correspondence/{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_officer_cross_case_rows_carry_subject_and_snippet(self):
        row = self.rows(self.officer)[0]

        self.assertEqual(row["subject"], "Re: bus camera footage request")
        self.assertIn("afternoon route footage", row["snippet"])

    def test_other_roles_still_get_blank_cross_case_content(self):
        row = self.rows(self.liaison)[0]

        self.assertEqual(row["subject"], "")
        self.assertEqual(row["snippet"], "")

    def test_no_role_gets_bodies_or_sender_from_a_cross_case_row(self):
        for user in (self.officer, self.liaison):
            with self.subTest(user=user.username):
                row = self.rows(user)[0]
                self.assertNotIn("received_body", row)
                self.assertNotIn("received_sender", row)
                self.assertNotIn("received_recipients", row)

    def test_request_scoped_reads_are_unchanged_for_other_roles(self):
        row = self.rows(
            self.liaison,
            f"?request={self.request.number}",
        )[0]

        self.assertEqual(row["subject"], "Re: bus camera footage request")
