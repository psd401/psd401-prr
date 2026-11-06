# ABOUTME: Staff API tests for the PublicRecordsRequest resource — list/detail
# ABOUTME: shape, filters, PATCH field-locking, and lifecycle transition actions.
import datetime as dt
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.api.resources.requests import CASE_ORDERINGS
from apps.audit.models import AuditLog
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceDelivery,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    EstimateRevision,
    FootagePreservation,
    MatterKind,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
    RequestStatus,
    StatusEvent,
)


def make_request(**kw):
    requester = kw.pop("requester", None) or Requester.objects.create(
        email="jane@example.org", name="Jane Q. Public")
    defaults = {
        "requester": requester,
        "description": "All emails about the stadium project",
    }
    defaults.update(kw)
    return PublicRecordsRequest.objects.create(**defaults)


class RequestsStaffGateTests(TestCase):
    def test_list_refuses_anonymous(self):
        r = self.client.get("/api/staff/requests/")
        self.assertEqual(r.status_code, 403)

    def test_list_refuses_non_staff(self):
        get_user_model().objects.create_user(
            "civilian", "civilian@example.com", "pw", is_staff=False)
        self.client.login(username="civilian", password="pw")
        r = self.client.get("/api/staff/requests/")
        self.assertEqual(r.status_code, 403)


class StaffApiTestCase(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")

    def numbers(self, query=""):
        """Case numbers the staff list returns, in the order it returns them."""
        response = self.client.get(f"/api/staff/requests/{query}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        return [row["number"] for row in rows]


class RequestsListTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        self.r1 = make_request(
            description="All emails about the stadium project",
            status=RequestStatus.RECEIVED,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=5),
        )
        self.r2 = make_request(
            requester=Requester.objects.create(email="bob@example.org", name="Bob Smith"),
            description="Budget spreadsheets for the bond measure",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=1),
        )

    def test_list_returns_expected_fields(self):
        r = self.client.get("/api/staff/requests/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual(len(rows), 2)
        row = next(x for x in rows if x["number"] == self.r1.number)
        expected_keys = {
            "id", "number", "status", "matter_kind", "matter_kind_display",
            "received_at", "statutory_response_due", "ferpa_access_due",
            "estimated_completion", "closed_at", "requester_email",
            "requester_name", "assigned_officer_username",
            "tolled_business_days", "acknowledgment_sent_at",
            "initial_response_delivered",
            "next_due", "next_due_basis",
            "latest_note",
            "inbox_needs_attention", "latest_inbound_at",
            "subject", "subject_source", "intake_suggestion",
        }
        self.assertEqual(set(row.keys()), expected_keys)
        # Nothing has been sent on r1, so the next thing it owes is still the
        # RCW 42.56.520 first response.
        self.assertEqual(
            row["next_due"], self.r1.statutory_response_due.isoformat()
        )
        self.assertEqual(row["next_due_basis"], "statutory")
        self.assertEqual(row["requester_email"], "jane@example.org")
        self.assertEqual(row["requester_name"], "Jane Q. Public")
        self.assertIsNone(row["assigned_officer_username"])
        self.assertNotIn("description", row)

    def test_initial_response_delivered_includes_a_clarification_response(self):
        StatusEvent.objects.create(
            request=self.r1,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
        )

        response = self.client.get("/api/staff/requests/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        by_number = {row["number"]: row for row in rows}
        self.assertIsNone(by_number[self.r1.number]["acknowledgment_sent_at"])
        self.assertTrue(by_number[self.r1.number]["initial_response_delivered"])
        self.assertFalse(by_number[self.r2.number]["initial_response_delivered"])

    def test_default_ordering_by_statutory_response_due(self):
        r = self.client.get("/api/staff/requests/")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        numbers = [row["number"] for row in rows]
        self.assertEqual(numbers, [self.r2.number, self.r1.number])

    def ferpa_matter(self, days):
        return make_request(
            description="Parent access to a student file",
            matter_kind=MatterKind.FERPA_PARENT,
            statutory_response_due=None,
            ferpa_access_due=timezone.localdate() + dt.timedelta(days=days),
        )

    def test_default_ordering_ranks_a_ferpa_matter_by_its_access_date(self):
        """A FERPA matter has no RCW 42.56.520 date, only its 34 CFR
        99.10(b) access date. Ranking by the RCW date alone sank it below
        every public records case, however soon it was due."""
        ferpa = self.ferpa_matter(days=3)

        self.assertEqual(
            self.numbers(), [self.r2.number, ferpa.number, self.r1.number],
        )

    def test_inbox_default_ranks_a_ferpa_matter_by_its_access_date(self):
        ferpa = self.ferpa_matter(days=3)

        # No case has a waiting requester email, so the flagged-first group is
        # empty and the whole queue is in due-date order.
        self.assertEqual(
            self.numbers("?queue=inbox"),
            [self.r2.number, ferpa.number, self.r1.number],
        )

    def test_status_filter(self):
        r = self.client.get("/api/staff/requests/?status=gathering")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["number"] for row in rows], [self.r2.number])

    def test_search_by_number(self):
        r = self.client.get(f"/api/staff/requests/?search={self.r1.number}")
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual([row["number"] for row in rows], [self.r1.number])

    def test_search_by_description(self):
        r = self.client.get("/api/staff/requests/?search=bond measure")
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual([row["number"] for row in rows], [self.r2.number])

    def test_search_by_requester_email(self):
        r = self.client.get("/api/staff/requests/?search=bob@example")
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual([row["number"] for row in rows], [self.r2.number])


class RequestsOrderingTests(StaffApiTestCase):
    """Whitelisted column sorting for the case table.

    Every assertion here is about order, which is the whole contract: a table
    header that claims a sort and does not deliver one is worse than no header
    at all, because staff stop scrolling once they believe the top row is the
    oldest.
    """

    def setUp(self):
        super().setUp()
        self.alpha = make_request(
            requester=Requester.objects.create(
                email="zoe@example.org", name="Alice Anderson",
            ),
            description="Alpha stadium contract",
            status=RequestStatus.RECEIVED,
            received_at=timezone.now() - dt.timedelta(days=3),
            statutory_response_due=timezone.localdate() + dt.timedelta(days=9),
            estimated_completion=timezone.localdate() + dt.timedelta(days=30),
        )
        self.bravo = make_request(
            requester=Requester.objects.create(
                email="amy@example.org", name="Zeke Zimmer",
            ),
            description="Bravo bus camera footage",
            status=RequestStatus.GATHERING,
            received_at=timezone.now() - dt.timedelta(days=1),
            statutory_response_due=timezone.localdate() + dt.timedelta(days=2),
        )

    def test_ordering_by_received_ascending_and_descending(self):
        self.assertEqual(
            self.numbers("?ordering=received"),
            [self.alpha.number, self.bravo.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-received"),
            [self.bravo.number, self.alpha.number],
        )

    def test_ordering_by_status(self):
        self.assertEqual(
            self.numbers("?ordering=status"),
            [self.bravo.number, self.alpha.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-status"),
            [self.alpha.number, self.bravo.number],
        )

    def test_ordering_by_assignee(self):
        user_model = get_user_model()
        self.alpha.assigned_officer = user_model.objects.create_user(
            "zed", "zed@psd401.net", "pw", is_staff=True,
        )
        self.alpha.save(update_fields=["assigned_officer"])
        self.bravo.assigned_officer = user_model.objects.create_user(
            "abe", "abe@psd401.net", "pw", is_staff=True,
        )
        self.bravo.save(update_fields=["assigned_officer"])
        unassigned = make_request(
            description="Charlie, nobody working it",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=4),
        )

        # The unassigned case sorts last in both directions.
        self.assertEqual(
            self.numbers("?ordering=assignee"),
            [self.bravo.number, self.alpha.number, unassigned.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-assignee"),
            [self.alpha.number, self.bravo.number, unassigned.number],
        )

    def test_ordering_by_estimated_is_directional_and_undated_last(self):
        self.bravo.estimated_completion = (
            timezone.localdate() + dt.timedelta(days=10)
        )
        self.bravo.save(update_fields=["estimated_completion"])
        undated = make_request(
            description="Charlie, no estimate yet",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=4),
        )

        # The undated case sorts last in both directions: SQLite and Postgres
        # disagree about NULL placement and a deadline column must not.
        self.assertEqual(
            self.numbers("?ordering=estimated"),
            [self.bravo.number, self.alpha.number, undated.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-estimated"),
            [self.alpha.number, self.bravo.number, undated.number],
        )

    def test_ordering_by_due_date(self):
        self.assertEqual(
            self.numbers("?ordering=due"),
            [self.bravo.number, self.alpha.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-due"),
            [self.alpha.number, self.bravo.number],
        )

    def test_due_date_ordering_places_a_ferpa_matter_by_its_access_date(self):
        """The column shows a FERPA matter's 34 CFR 99.10(b) date, so it sorts
        by it. Sorting on the RCW date alone would sink every FERPA matter to
        the bottom of a "soonest first" table while its cell shows it due."""
        ferpa = make_request(
            description="Parent access to a student file",
            matter_kind=MatterKind.FERPA_PARENT,
            statutory_response_due=None,
            ferpa_access_due=timezone.localdate() + dt.timedelta(days=5),
        )

        self.assertEqual(
            self.numbers("?ordering=due"),
            [self.bravo.number, ferpa.number, self.alpha.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-due"),
            [self.alpha.number, ferpa.number, self.bravo.number],
        )

    def test_due_ordering_follows_the_estimate_once_the_case_is_answered(self):
        """An acknowledged case no longer owes its five-day date, so it must
        not be ranked by it. Here the acknowledged case's statutory date is the
        soonest in the fixture and its promised completion the latest, and the
        table has to rank it by the promise."""
        answered = make_request(
            description="Answered already",
            status=RequestStatus.ACKNOWLEDGED,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=30),
            acknowledgment_sent_at=timezone.now() - dt.timedelta(days=29),
            estimated_completion=timezone.localdate() + dt.timedelta(days=90),
        )

        self.assertEqual(
            self.numbers("?ordering=due"),
            [self.bravo.number, self.alpha.number, answered.number],
        )

    def test_the_due_column_and_the_due_sort_read_the_same_value(self):
        answered = make_request(
            description="Answered already",
            status=RequestStatus.ACKNOWLEDGED,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=30),
            acknowledgment_sent_at=timezone.now() - dt.timedelta(days=29),
            estimated_completion=timezone.localdate() + dt.timedelta(days=90),
        )

        response = self.client.get("/api/staff/requests/?ordering=due")
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        row = next(r for r in rows if r["number"] == answered.number)
        self.assertEqual(
            row["next_due"], answered.estimated_completion.isoformat()
        )
        self.assertEqual(row["next_due_basis"], "estimate")
        # The spent statutory date is still on the record, just no longer the
        # date the row is ranked or read by.
        self.assertEqual(
            row["statutory_response_due"],
            answered.statutory_response_due.isoformat(),
        )
        self.assertEqual([r["number"] for r in rows][-1], answered.number)

    def test_ordering_by_subject_uses_the_description_when_no_advisory_title(self):
        self.assertEqual(
            self.numbers("?ordering=subject"),
            [self.alpha.number, self.bravo.number],
        )
        self.assertEqual(
            self.numbers("?ordering=-subject"),
            [self.bravo.number, self.alpha.number],
        )

    def test_officer_may_order_by_requester(self):
        self.assertEqual(
            self.numbers("?ordering=requester"),
            [self.alpha.number, self.bravo.number],
        )

    def test_non_officer_requester_ordering_falls_back_to_the_default(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        self.client.force_login(reviewer)

        response = self.client.get("/api/staff/requests/?ordering=requester")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        # Identity is blanked for this role, so it must not decide row order
        # either: the default statutory ordering stands instead, which here is
        # the reverse of what ordering by requester name would have produced.
        self.assertIsNone(rows[0]["requester_name"])
        self.assertEqual(
            [row["number"] for row in rows],
            [self.bravo.number, self.alpha.number],
        )

    def test_unknown_ordering_key_falls_back_to_the_default(self):
        self.assertEqual(
            self.numbers("?ordering=requester__email"),
            [self.bravo.number, self.alpha.number],
        )
        self.assertEqual(
            self.numbers("?ordering=nonsense"),
            [self.bravo.number, self.alpha.number],
        )

    def test_absent_ordering_keeps_the_inbox_flagged_first_default(self):
        self.assertEqual(
            self.numbers("?queue=inbox"),
            [self.bravo.number, self.alpha.number],
        )

    def test_an_explicit_ordering_replaces_the_inbox_default(self):
        self.assertEqual(
            self.numbers("?queue=inbox&ordering=received"),
            [self.alpha.number, self.bravo.number],
        )

    def test_subject_ordering_inside_the_inbox_queue(self):
        self.assertEqual(
            self.numbers("?queue=inbox&ordering=-subject"),
            [self.bravo.number, self.alpha.number],
        )

    def test_the_whitelist_matches_the_staff_ui_sort_keys(self):
        """The frontend drops a key it does not know rather than sending it,
        so a column added on one side only would silently stop sorting."""
        source = (
            Path(settings.BASE_DIR) / "frontend" / "src" / "caseSort.ts"
        ).read_text()
        block = re.search(
            r"CASE_SORT_KEYS = new Set\(\[(.*?)\]\)", source, re.DOTALL,
        )
        self.assertIsNotNone(block)
        ui_keys = set(re.findall(r"'([a-z_]+)'", block.group(1)))
        api_keys = {key for key in CASE_ORDERINGS if not key.startswith("-")}
        self.assertEqual(ui_keys, api_keys)


class RequestsSubjectTests(StaffApiTestCase):
    """The advisory subject column, and what it is allowed to be."""

    def setUp(self):
        super().setUp()
        self.prr = make_request(
            description="  Every  email\nabout the stadium project  ",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=5),
        )

    def row(self):
        response = self.client.get("/api/staff/requests/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        return next(row for row in rows if row["number"] == self.prr.number)

    def complete_intent_run(self, proposal):
        return AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
            request=self.prr,
            model_identifier="test-model",
            input_sha256="0" * 64,
            requested_by=self.officer,
            proposal=proposal,
        )

    def test_subject_falls_back_to_a_collapsed_description_snippet(self):
        row = self.row()
        self.assertEqual(row["subject"], "Every email about the stadium project")
        self.assertEqual(row["subject_source"], "description")

    def test_long_descriptions_are_truncated_to_one_line(self):
        # A description is immutable after intake, so a long one is a new
        # case rather than an edit to this one.
        long_case = make_request(
            description="stadium " * 60,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=5),
        )

        response = self.client.get("/api/staff/requests/")
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        row = next(row for row in rows if row["number"] == long_case.number)

        self.assertLessEqual(len(row["subject"]), 141)
        self.assertTrue(row["subject"].endswith("…"))
        self.assertNotIn("\n", row["subject"])

    def test_a_completed_intent_run_supplies_an_advisory_subject(self):
        self.complete_intent_run({"short_title": "Stadium contract emails"})

        row = self.row()

        self.assertEqual(row["subject"], "Stadium contract emails")
        self.assertEqual(row["subject_source"], "ai")

    def complete_case_title_run(self, title):
        return AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.CASE_TITLE,
            state=AdvisoryRun.State.COMPLETE,
            request=self.prr,
            model_identifier="test-model",
            input_sha256="1" * 64,
            requested_by=self.officer,
            proposal={"title": title},
        )

    def test_a_completed_case_title_run_supplies_the_subject(self):
        self.complete_case_title_run("Stadium contractor emails")

        row = self.row()

        self.assertEqual(row["subject"], "Stadium contractor emails")
        self.assertEqual(row["subject_source"], "ai")

    def test_a_case_title_wins_over_an_intent_run_short_title(self):
        """The title run exists for this column and is prepared for every
        case; the intent run's short title is a by-product of an analysis an
        officer asked for."""
        self.complete_intent_run({"short_title": "From the intent analysis"})
        self.complete_case_title_run("From the title run")

        self.assertEqual(self.row()["subject"], "From the title run")

    def test_an_intent_short_title_still_answers_when_there_is_no_title_run(self):
        self.complete_intent_run({"short_title": "From the intent analysis"})

        self.assertEqual(self.row()["subject"], "From the intent analysis")

    def test_an_incomplete_case_title_run_is_not_read(self):
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.CASE_TITLE,
            state=AdvisoryRun.State.RUNNING,
            request=self.prr,
            model_identifier="test-model",
            input_sha256="1" * 64,
            requested_by=self.officer,
            proposal={"title": "Not finished"},
        )

        self.assertEqual(self.row()["subject_source"], "description")

    def test_a_non_officer_sees_the_description_not_the_case_title(self):
        self.complete_case_title_run("Stadium contractor emails")
        reviewer = get_user_model().objects.create_user(
            "subject-reviewer", "subject-reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        self.client.force_login(reviewer)

        row = self.row()

        self.assertEqual(row["subject_source"], "description")
        self.assertNotIn("Stadium contractor emails", row["subject"])

    def test_an_intent_run_without_a_short_title_leaves_the_description(self):
        self.complete_intent_run({"summary": "A summary from an older run."})

        row = self.row()

        self.assertEqual(row["subject_source"], "description")

    def test_an_incomplete_run_is_not_read(self):
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.RUNNING,
            request=self.prr,
            model_identifier="test-model",
            input_sha256="0" * 64,
            requested_by=self.officer,
            proposal={"short_title": "Not finished"},
        )

        self.assertEqual(self.row()["subject_source"], "description")

    def test_the_latest_of_two_simultaneous_runs_is_chosen_deterministically(self):
        older = self.complete_intent_run({"short_title": "First title"})
        newer = self.complete_intent_run({"short_title": "Second title"})
        AdvisoryRun.objects.filter(pk__in=[older.pk, newer.pk]).update(
            created_at=timezone.now(),
        )
        expected = max([older, newer], key=lambda run: run.pk)

        self.assertEqual(
            self.row()["subject"], expected.proposal["short_title"],
        )

    def test_a_non_officer_sees_the_description_not_the_advisory_title(self):
        """Advisory output is Records Officer only on its own endpoint, so the
        list column must not become a second, wider way to read it."""
        self.complete_intent_run({"short_title": "Stadium contract emails"})
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        self.client.force_login(reviewer)

        row = self.row()

        self.assertEqual(row["subject"], "Every email about the stadium project")
        self.assertEqual(row["subject_source"], "description")

    def test_a_non_officer_subject_sort_ignores_the_advisory_title(self):
        # By its advisory title this case sorts first; by description, last.
        titled = make_request(
            description="Zebra crossing reports",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=5),
        )
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
            request=titled,
            model_identifier="test-model",
            input_sha256="0" * 64,
            requested_by=self.officer,
            proposal={"short_title": "Aardvark"},
        )
        other = make_request(
            description="Bus route records",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=5),
        )

        def order():
            response = self.client.get("/api/staff/requests/?ordering=subject")
            payload = response.json()
            rows = payload["results"] if isinstance(payload, dict) else payload
            return [
                row["number"] for row in rows
                if row["number"] in {titled.number, other.number}
            ]

        self.assertEqual(order(), [titled.number, other.number])
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        self.client.force_login(reviewer)
        self.assertEqual(order(), [other.number, titled.number])

    def test_the_subject_column_never_edits_the_legal_record(self):
        original = PublicRecordsRequest.objects.get(pk=self.prr.pk).description
        self.complete_intent_run({"short_title": "Stadium contract emails"})

        self.row()

        refreshed = PublicRecordsRequest.objects.get(pk=self.prr.pk)
        self.assertEqual(refreshed.description, original)
        self.assertFalse(
            AdvisoryRun.objects.filter(request=self.prr, applied=True).exists()
        )

    def test_the_subject_column_costs_no_query_per_row(self):
        """The advisory title is one subquery for the page, not one per row.

        Counting every query on this list would assert unrelated things (the
        list has other per-row reads of its own, which predate this column).
        The defect worth guarding against here is precise: an advisory run
        looked up once per case.
        """
        self.complete_intent_run({"short_title": "Stadium contract emails"})
        for index in range(9):
            other = make_request(
                description=f"Another request {index}",
                statutory_response_due=timezone.localdate(),
            )
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.REQUEST_INTENT,
                state=AdvisoryRun.State.COMPLETE,
                request=other,
                model_identifier="test-model",
                input_sha256="0" * 64,
                requested_by=self.officer,
                proposal={"short_title": f"Title {index}"},
            )

        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get("/api/staff/requests/").status_code, 200)

        advisory_reads = [
            query for query in captured.captured_queries
            if "records_advisoryrun" in query["sql"]
        ]
        self.assertEqual(len(advisory_reads), 1)


class RequestsDetailTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        self.prr = make_request()
        self.older_event = StatusEvent.objects.create(
            request=self.prr, from_status="", to_status=RequestStatus.RECEIVED,
            note="Intake received.", occurred_at=timezone.now() - dt.timedelta(hours=2),
        )
        self.newer_event = StatusEvent.objects.create(
            request=self.prr, actor=self.officer, from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED, note="Acknowledged.",
            visible_to_requester=True, occurred_at=timezone.now() - dt.timedelta(hours=1),
        )
        EstimateRevision.objects.create(
            request=self.prr, previous_date=None,
            new_date=timezone.localdate() + dt.timedelta(days=30),
            reason="Initial estimate.", revised_by=self.officer,
        )
        self.system = RecordSystem.objects.create(slug="sis", name="Student Info System")
        CollectionTask.objects.create(
            request=self.prr, system=self.system, kind=CollectionTask.Kind.MANUAL,
            due_date=timezone.localdate() + dt.timedelta(days=10),
        )
        FootagePreservation.objects.create(
            request=self.prr,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

    def test_retrieve_by_number_shape(self):
        r = self.client.get(f"/api/staff/requests/{self.prr.number}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()

        self.assertEqual(body["number"], self.prr.number)
        self.assertEqual(body["description"], self.prr.description)
        self.assertEqual(body["channel"], self.prr.channel)
        self.assertEqual(body["requester"], {
            "name": "Jane Q. Public", "email": "jane@example.org", "phone": "",
            "mailing_address": "", "city": "", "state": "",
            "postal_code": "", "company": "",
        })
        self.assertEqual(body["intake_attachments"], [])

        events = body["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["note"], "Acknowledged.")
        self.assertEqual(events[0]["actor"], "officer")
        self.assertEqual(events[0]["visible_to_requester"], True)
        self.assertEqual(events[1]["note"], "Intake received.")
        self.assertIsNone(events[1]["actor"])

        revisions = body["estimate_revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["reason"], "Initial estimate.")
        self.assertEqual(revisions[0]["revised_by"], "officer")
        self.assertFalse(revisions[0]["automated"])

        tasks = body["collection_tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["system"], "Student Info System")
        self.assertEqual(tasks[0]["state"], CollectionTask.State.PENDING)
        self.assertEqual(tasks[0]["kind"], CollectionTask.Kind.MANUAL)

        preservations = body["footage_preservations"]
        self.assertEqual(len(preservations), 1)
        self.assertEqual(preservations[0]["state"], FootagePreservation.State.PENDING)
        self.assertEqual(body["initial_response_plan"]["state"], "delivered")

    @override_settings(
        AUTO_ACK_SAFETY_NET=True,
        PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
    )
    def test_detail_explains_when_the_five_day_safety_net_will_run(self):
        scheduled = make_request(
            requester=Requester.objects.create(email="scheduled@example.org"),
            statutory_response_due=timezone.localdate() + dt.timedelta(days=1),
        )
        due = make_request(
            requester=Requester.objects.create(email="due@example.org"),
            statutory_response_due=timezone.localdate(),
        )

        scheduled_response = self.client.get(
            f"/api/staff/requests/{scheduled.number}/"
        )
        due_response = self.client.get(f"/api/staff/requests/{due.number}/")

        self.assertEqual(
            scheduled_response.json()["initial_response_plan"]["state"],
            "scheduled",
        )
        self.assertEqual(
            due_response.json()["initial_response_plan"]["state"],
            "due_for_safety_net",
        )
        self.assertEqual(
            due_response.json()["initial_response_plan"]["automatic_check"],
            "Hourly at :30 Pacific on and after the due date.",
        )

    @override_settings(
        AUTO_ACK_SAFETY_NET=False,
        PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False,
    )
    def test_detail_requires_human_acknowledgment_in_manual_only_mode(self):
        due = make_request(
            requester=Requester.objects.create(email="manual@example.org"),
            statutory_response_due=timezone.localdate(),
        )

        response = self.client.get(f"/api/staff/requests/{due.number}/")

        self.assertEqual(response.status_code, 200)
        plan = response.json()["initial_response_plan"]
        self.assertEqual(plan["state"], "manual_required")
        self.assertIn("human-approved acknowledgment", plan["automatic_check"])

    @override_settings(
        AUTO_ACK_SAFETY_NET=False,
        PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False,
    )
    def test_detail_identifies_an_existing_automatic_acknowledgment_as_held(self):
        due = make_request(
            requester=Requester.objects.create(email="held@example.org"),
            statutory_response_due=timezone.localdate(),
        )
        acknowledgment = Correspondence.objects.create(
            request=due,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            status=Correspondence.Status.QUEUED,
            approved_subject="Five-day response",
            approved_body="Approved automatic snapshot.",
            approved_at=timezone.now(),
            automated=True,
            queued_at=timezone.now(),
        )
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=acknowledgment,
            idempotency_key=f"correspondence-delivery:{acknowledgment.pk}",
        )

        response = self.client.get(f"/api/staff/requests/{due.number}/")

        self.assertEqual(response.status_code, 200)
        plan = response.json()["initial_response_plan"]
        self.assertEqual(plan["state"], "automatic_blocked")
        self.assertEqual(plan["correspondence_id"], str(acknowledgment.pk))
        self.assertEqual(plan["delivery_status"], delivery.status)
        self.assertTrue(plan["automated"])

    def test_detail_does_not_claim_safety_net_will_process_a_terminal_request(self):
        closed = make_request(
            requester=Requester.objects.create(email="closed@example.org"),
            status=RequestStatus.CLOSED_NON_REQUEST,
            statutory_response_due=timezone.localdate(),
            closed_at=timezone.now(),
        )

        response = self.client.get(f"/api/staff/requests/{closed.number}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["initial_response_plan"]["state"],
            "terminal_excluded",
        )

    def test_detail_omits_no_footage_preservation_gracefully(self):
        prr = make_request(
            requester=Requester.objects.create(email="noholds@example.org"))
        r = self.client.get(f"/api/staff/requests/{prr.number}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["footage_preservations"], [])

    def test_retrieve_unknown_number_404(self):
        r = self.client.get("/api/staff/requests/PRR-2026-9999/")
        self.assertEqual(r.status_code, 404)


class RequestsPatchTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        self.prr = make_request()
        self.other_officer = get_user_model().objects.create_user(
            "officer2", "officer2@psd401.net", "pw", is_staff=True)

    def test_patch_assigned_officer_works(self):
        r = self.client.patch(
            f"/api/staff/requests/{self.prr.number}/",
            {"assigned_officer_id": self.other_officer.id},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.assigned_officer_id, self.other_officer.id)
        self.assertEqual(r.json()["assigned_officer_username"], "officer2")

    def test_patch_assigned_officer_locks_fresh_state_and_appends_audit_evidence(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            assigned_officer=self.other_officer,
        )

        response = self.client.patch(
            f"/api/staff/requests/{self.prr.number}/",
            {"assigned_officer_id": self.officer.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.assigned_officer_id, self.officer.id)
        evidence = AuditLog.objects.get(
            action="request.owner_changed",
            target=self.prr.number,
        )
        self.assertEqual(evidence.actor, self.officer)
        self.assertEqual(
            {
                key: value
                for key, value in evidence.detail.items()
                if key != "occurred_at"
            },
            {
                "previous_owner_id": self.other_officer.id,
                "previous_owner_username": self.other_officer.username,
                "new_owner_id": self.officer.id,
                "new_owner_username": self.officer.username,
            },
        )
        self.assertRegex(
            evidence.detail["occurred_at"],
            r"^\d{4}-\d{2}-\d{2}T",
        )

    def test_patch_clears_assigned_officer_with_null(self):
        self.prr.assigned_officer = self.other_officer
        self.prr.save(update_fields=["assigned_officer"])
        r = self.client.patch(
            f"/api/staff/requests/{self.prr.number}/",
            {"assigned_officer_id": None},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.prr.refresh_from_db()
        self.assertIsNone(self.prr.assigned_officer_id)

    def test_patch_rejects_a_case_owner_without_current_officer_authority(self):
        liaison = get_user_model().objects.create_user(
            "owner-liaison",
            "owner-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})

        response = self.client.patch(
            f"/api/staff/requests/{self.prr.number}/",
            {"assigned_officer_id": liaison.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("active Records Officer", str(response.json()))
        self.prr.refresh_from_db()
        self.assertIsNone(self.prr.assigned_officer_id)

    def test_patch_description_status_number_ignored(self):
        original_description = self.prr.description
        original_status = self.prr.status
        original_number = self.prr.number
        r = self.client.patch(
            f"/api/staff/requests/{self.prr.number}/",
            {
                "description": "REWRITTEN",
                "status": RequestStatus.DENIED,
                "number": "PRR-2026-0001",
                "assigned_officer_id": self.other_officer.id,
            },
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.description, original_description)
        self.assertEqual(self.prr.status, original_status)
        self.assertEqual(self.prr.number, original_number)
        self.assertEqual(self.prr.assigned_officer_id, self.other_officer.id)


class RequestsTransitionActionTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        self.prr = make_request(status=RequestStatus.RECEIVED)

    def _open_clarification_fixture(self, *, opened_at=None):
        opened_at = opened_at or timezone.now() - dt.timedelta(days=1)
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.CLARIFICATION,
        )
        self.prr.refresh_from_db()
        opened_event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
            occurred_at=opened_at,
        )
        outbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.DRAFT,
            draft_subject="Clarification requested",
            draft_body="Please clarify the scope.",
            created_by=self.officer,
        )
        return ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            opened_at=opened_at,
            opened_status_event=opened_event,
        )

    def test_transition_creates_status_event_and_updates_status(self):
        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {"to_status": RequestStatus.GATHERING, "note": "Collection started."},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)
        event = self.prr.events.latest("occurred_at")
        self.assertEqual(event.to_status, RequestStatus.GATHERING)
        self.assertEqual(event.from_status, RequestStatus.RECEIVED)
        self.assertEqual(event.note, "Collection started.")
        self.assertEqual(event.actor, self.officer)

    def test_correspondence_driven_statuses_cannot_be_set_directly(self):
        original_event_count = self.prr.events.count()

        for to_status in (
            RequestStatus.ACKNOWLEDGED,
            RequestStatus.CLARIFICATION,
            RequestStatus.DENIED,
        ):
            with self.subTest(to_status=to_status):
                r = self.client.post(
                    f"/api/staff/requests/{self.prr.number}/transition/",
                    {"to_status": to_status, "note": "Message sent."},
                    content_type="application/json",
                )

                self.assertEqual(r.status_code, 409)
                self.assertEqual(
                    r.json()["code"],
                    "correspondence_delivery_required",
                )
                self.prr.refresh_from_db()
                self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
                self.assertEqual(self.prr.events.count(), original_event_count)

    def test_terminal_closure_requires_a_substantive_reason(self):
        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {
                "to_status": RequestStatus.FULFILLED,
                "note": "done",
            },
            content_type="application/json",
        )

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "terminal_action_required")
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.closed_at)
        self.assertFalse(self.prr.events.exists())

    def test_open_clarification_cannot_be_left_by_generic_transition(self):
        self._open_clarification_fixture()
        original_event_count = self.prr.events.count()

        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {
                "to_status": RequestStatus.GATHERING,
                "note": "Scope discussed.",
            },
            content_type="application/json",
        )

        self.assertEqual(r.status_code, 409)
        self.assertEqual(
            r.json()["code"],
            "clarification_resolution_required",
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLARIFICATION)
        self.assertEqual(self.prr.events.count(), original_event_count)

    def test_attributed_officer_token_can_close_clarification_atomically(self):
        opened_at = timezone.now() - dt.timedelta(days=1)
        cycle = self._open_clarification_fixture(
            opened_at=opened_at,
        )
        reason = "Requester did not clarify by the documented response deadline."
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        token = Token.objects.create(user=self.officer)
        self.client.logout()

        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {
                "to_status": RequestStatus.CLOSED_ABANDONED,
                "note": reason,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        self.prr.refresh_from_db()
        cycle.refresh_from_db()
        event = self.prr.events.get(
            from_status=RequestStatus.CLARIFICATION,
            to_status=RequestStatus.CLOSED_ABANDONED,
        )
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_ABANDONED)
        self.assertEqual(cycle.cancelled_at, event.occurred_at)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)
        self.assertIsNone(cycle.resolved_at)
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.clarification_cancelled_for_closure",
                actor=self.officer,
                detail__clarification_cycle_id=str(cycle.id),
                detail__reason=reason,
            ).exists()
        )

    def test_transition_bad_status_rejected(self):
        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {"to_status": "not_a_real_status", "note": ""},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_transition_from_closed_status_rejected(self):
        self.prr.status = RequestStatus.FULFILLED
        self.prr.closed_at = timezone.now()
        self.prr.save(update_fields=["status", "closed_at"])
        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {"to_status": RequestStatus.GATHERING, "note": ""},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("closed", r.json()["detail"].lower())


class EmailNonRequestClosureActionTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = make_request(
            status=RequestStatus.RECEIVED,
            channel="email",
        )
        matched_at = timezone.now()
        self.correspondence = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="New request",
            received_body=self.prr.description,
            received_sender="Jane <jane@example.org>",
            received_recipients=["publicrecords@psd401.net"],
            received_at=self.prr.received_at,
            matched_at=matched_at,
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id="email-intake-message",
            created_by=self.officer,
        )
        self.receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id="email-intake-message",
            provider_thread_id="email-intake-thread",
            received_at=self.prr.received_at,
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
            matched_request=self.prr,
            matched_correspondence=self.correspondence,
            matched_at=matched_at,
            quarantined=False,
            raw_message_size=128,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key="gmail:poll:email-intake-message",
            submitted_by=self.officer,
            completed_at=matched_at,
        )

    def test_detail_exposes_dedicated_closure_eligibility(self):
        response = self.client.get(
            f"/api/staff/requests/{self.prr.number}/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["email_intake_auto_created"])

    def test_records_officer_closes_non_request_with_retained_evidence(self):
        reason = "Automated vendor solicitation; no public records were requested."

        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/close-non-request/",
            {"reason": reason},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.prr.refresh_from_db()
        self.receipt.refresh_from_db()
        self.correspondence.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertIsNotNone(self.prr.closed_at)
        event = self.prr.events.get(
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLOSED_NON_REQUEST,
        )
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.note, reason)
        self.assertEqual(self.receipt.matched_request, self.prr)
        self.assertEqual(self.correspondence.request, self.prr)
        self.assertTrue(PublicRecordsRequest.objects.filter(pk=self.prr.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.status_changed",
                target=self.prr.number,
                actor=self.officer,
                detail__to_status=RequestStatus.CLOSED_NON_REQUEST,
            ).exists()
        )

    def test_closure_requires_substantive_reason(self):
        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/close-non-request/",
            {"reason": "not one"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_generic_transition_cannot_bypass_dedicated_closure(self):
        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/transition/",
            {
                "to_status": RequestStatus.CLOSED_NON_REQUEST,
                "note": "This is not a public records request.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "terminal_action_required")
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_non_email_intake_request_cannot_use_closure(self):
        other = make_request(channel="email")

        response = self.client.post(
            f"/api/staff/requests/{other.number}/close-non-request/",
            {"reason": "This message does not contain a records request."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "not_email_intake")
        other.refresh_from_db()
        self.assertEqual(other.status, RequestStatus.RECEIVED)

    def test_reviewer_cannot_close_email_intake(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer-close-email",
            "reviewer-close-email@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.logout()
        self.client.login(username=reviewer.username, password="pw")

        response = self.client.post(
            f"/api/staff/requests/{self.prr.number}/close-non-request/",
            {"reason": "This message does not contain a records request."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)


class RequestsLegacyReviseEstimateActionTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        self.prr = make_request(estimated_completion=timezone.localdate())

    def test_obsolete_direct_revision_endpoint_is_not_routed(self):
        old_date = self.prr.estimated_completion
        new_date = timezone.localdate() + dt.timedelta(days=45)
        r = self.client.post(
            f"/api/staff/requests/{self.prr.number}/revise-estimate/",
            {
                "new_date": str(new_date),
                "reason": "Vault export returned thousands of messages requiring review.",
            },
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.estimated_completion, old_date)
        self.assertEqual(EstimateRevision.objects.count(), 0)


class CaseClosureActionTests(StaffApiTestCase):
    """Closing a case is everyone's work; the written reason is the guard."""

    CLOSURE_REASON = "Requester withdrew by phone; the call is logged in notes."

    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = make_request(status=RequestStatus.GATHERING)

    def _as(self, role):
        actor = get_user_model().objects.create_user(
            f"{role}-closer",
            f"{role}-closer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(actor, {role})
        if role == StaffRole.LIAISON:
            # A liaison sees only the cases their own work sits on, which is
            # exactly the case they would be closing.
            CollectionTask.objects.create(
                request=self.prr,
                kind=CollectionTask.Kind.MANUAL,
                assigned_to=actor,
                instructions="Search the department share for responsive files.",
            )
        self.client.logout()
        self.client.login(username=actor.username, password="pw")
        return actor

    def _close(self, to_status=RequestStatus.CLOSED_WITHDRAWN, **overrides):
        body = {"to_status": to_status, "reason": self.CLOSURE_REASON}
        body.update(overrides)
        return self.client.post(
            f"/api/staff/requests/{self.prr.number}/close/",
            body,
            content_type="application/json",
        )

    def test_every_role_that_works_cases_may_close_one(self):
        for role in (
            StaffRole.RECORDS_OFFICER,
            StaffRole.LIAISON,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        ):
            with self.subTest(role=role):
                self.prr = make_request(status=RequestStatus.GATHERING)
                actor = self._as(role)

                response = self._close()

                self.assertEqual(response.status_code, 200)
                self.prr.refresh_from_db()
                event = self.prr.events.get(
                    to_status=RequestStatus.CLOSED_WITHDRAWN,
                )
                self.assertEqual(self.prr.status, RequestStatus.CLOSED_WITHDRAWN)
                self.assertIsNotNone(self.prr.closed_at)
                self.assertEqual(event.actor, actor)
                self.assertEqual(event.note, self.CLOSURE_REASON)
                self.assertTrue(
                    AuditLog.objects.filter(
                        action="request.status_changed",
                        target=self.prr.number,
                        actor=actor,
                        detail__to_status=RequestStatus.CLOSED_WITHDRAWN,
                    ).exists()
                )

    def test_abandonment_is_available_on_the_same_action(self):
        response = self._close(to_status=RequestStatus.CLOSED_ABANDONED)

        self.assertEqual(response.status_code, 200)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_ABANDONED)

    def test_a_liaison_may_not_close_a_case_they_are_not_working(self):
        """Widening the closure did not widen which cases a liaison can see."""
        actor = get_user_model().objects.create_user(
            "unassigned-liaison",
            "unassigned-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(actor, {StaffRole.LIAISON})
        self.client.logout()
        self.client.login(username=actor.username, password="pw")

        response = self._close()

        self.assertEqual(response.status_code, 404)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)

    def test_intake_clerk_may_not_close_a_case(self):
        self._as(StaffRole.INTAKE_CLERK)

        response = self._close()

        self.assertEqual(response.status_code, 403)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)

    def test_a_substantive_reason_is_required(self):
        response = self._close(reason="short")

        self.assertEqual(response.status_code, 400)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)

    def test_only_withdrawal_and_abandonment_are_offered_here(self):
        for to_status in (
            RequestStatus.FULFILLED,
            RequestStatus.DENIED,
            RequestStatus.CLOSED_NON_REQUEST,
            RequestStatus.CLOSED_NO_RECORDS,
            RequestStatus.GATHERING,
        ):
            with self.subTest(to_status=to_status):
                response = self._close(to_status=to_status)

                self.assertEqual(response.status_code, 400)
                self.prr.refresh_from_db()
                self.assertEqual(self.prr.status, RequestStatus.GATHERING)

    def test_an_already_closed_case_is_a_conflict(self):
        self._close()
        self.prr.refresh_from_db()

        response = self._close(to_status=RequestStatus.CLOSED_ABANDONED)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "closed_request")
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_WITHDRAWN)

    def test_an_open_clarification_is_cancelled_without_inventing_a_response(self):
        opened_at = timezone.now() - dt.timedelta(days=1)
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.CLARIFICATION,
        )
        self.prr.refresh_from_db()
        opened_event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
            occurred_at=opened_at,
        )
        outbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.DRAFT,
            draft_subject="Clarification requested",
            draft_body="Please clarify the scope.",
            created_by=self.officer,
        )
        cycle = ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            opened_at=opened_at,
            opened_status_event=opened_event,
        )
        self._as(StaffRole.LIAISON)

        response = self._close(to_status=RequestStatus.CLOSED_ABANDONED)

        self.assertEqual(response.status_code, 200)
        self.prr.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_ABANDONED)
        self.assertIsNotNone(cycle.cancelled_at)
        self.assertIsNone(cycle.responded_at)
        self.assertIsNone(cycle.resolved_at)
