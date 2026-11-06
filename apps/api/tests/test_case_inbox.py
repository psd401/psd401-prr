# ABOUTME: The per-case Inbox: every open case once, flagged by waiting requester email.
# ABOUTME: Also covers Mark caught up and the merge action through the staff API.
import datetime as dt

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.api.tests.test_requests import StaffApiTestCase, make_request
from apps.records.models import Requester, RequestStatus
from apps.records.tests.inbox_fixtures import matched_email
from apps.work_queue.models import WorkItem


class CaseInboxViewTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.web = make_request(
            description="Web form, no email anywhere",
            status=RequestStatus.RECEIVED,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=1),
        )
        self.emailed = make_request(
            requester=Requester.objects.create(
                email="bob@example.org", name="Bob Smith"
            ),
            description="Emailed request",
            status=RequestStatus.RECEIVED,
            channel="email",
            statutory_response_due=timezone.localdate() + dt.timedelta(days=4),
        )
        matched_email(self.emailed, officer=self.officer, intake=True)
        matched_email(self.emailed, officer=self.officer)
        self.closed = make_request(
            description="Done",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )

    def test_each_listed_case_carries_its_current_stage(self):
        # The Today list shows one next step per case without opening it.
        response = self.client.get(
            "/api/staff/requests/", {"status": "open", "ordering": "due"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        self.assertEqual(
            [row["number"] for row in rows],
            [self.web.number, self.emailed.number],
        )
        for row in rows:
            self.assertEqual(row["stage"], {
                "id": "intake",
                "label": "Intake",
                "state": "current",
                "open_count": 1,
                "summary": "Received and not yet acknowledged",
                "reason": "",
            })

    def test_the_case_list_costs_the_same_for_a_few_cases_or_many(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries_for_list():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get("/api/staff/requests/", {"status": "open"})
            self.assertEqual(response.status_code, 200, response.content)
            return len(queries.captured_queries)

        few = queries_for_list()
        for index in range(8):
            make_request(description=f"Another open case {index}", status=RequestStatus.ACKNOWLEDGED)
        self.assertEqual(queries_for_list(), few)

    def test_open_cases_view_lists_each_open_case_once_flagged_first(self):
        response = self.client.get("/api/staff/requests/", {"queue": "inbox"})

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        # The web-form case, which has no email row anywhere, is listed; the
        # case with waiting email sorts ahead of it despite a later deadline.
        self.assertEqual(
            [row["number"] for row in rows],
            [self.emailed.number, self.web.number],
        )
        by_number = {row["number"]: row for row in rows}
        self.assertEqual(by_number[self.emailed.number]["inbox_needs_attention"], 2)
        self.assertIsNotNone(by_number[self.emailed.number]["latest_inbound_at"])
        self.assertEqual(by_number[self.web.number]["inbox_needs_attention"], 0)
        self.assertIsNone(by_number[self.web.number]["latest_inbound_at"])
        self.assertNotIn(self.closed.number, by_number)

    def test_counts_report_flagged_cases_not_messages(self):
        response = self.client.get("/api/staff/work-items/counts/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["inbox_updated_cases"], 1)
        self.assertEqual(response.json()["inbox_needs_attention"], 2)

    def test_counts_use_no_per_row_source_lookups(self):
        # Production has thousands of work items; a correlated lookup per row
        # (matching a source id by casting primary keys) took ten seconds.
        # The counts match sources once, in uncorrelated subqueries.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/staff/work-items/counts/")

        self.assertEqual(response.status_code, 200, response.content)
        aggregate = max((query["sql"] for query in queries.captured_queries), key=len)
        self.assertNotIn('"_source_correspondence_id"', aggregate)
        self.assertNotIn('"_source_due_date"', aggregate)
        self.assertEqual(response.json()["inbox_needs_attention"], 2)

    def test_mark_caught_up_handles_every_waiting_row_on_the_case(self):
        response = self.client.post(
            f"/api/staff/requests/{self.emailed.number}/inbox-handled/",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"handled": 2})
        self.assertFalse(
            WorkItem.objects.filter(
                request=self.emailed,
                disposition=WorkItem.Disposition.NEEDS_ATTENTION,
            ).exists()
        )
        self.emailed.refresh_from_db()
        self.assertEqual(self.emailed.status, RequestStatus.RECEIVED)
        listed = self.client.get(
            "/api/staff/requests/", {"queue": "inbox"}
        ).json()["results"]
        self.assertEqual(
            {row["number"]: row["inbox_needs_attention"] for row in listed},
            {self.web.number: 0, self.emailed.number: 0},
        )

    def test_mark_caught_up_requires_a_records_officer(self):
        liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        self.client.logout()
        self.client.login(username="liaison", password="pw")

        response = self.client.post(
            f"/api/staff/requests/{self.emailed.number}/inbox-handled/",
            content_type="application/json",
        )

        self.assertIn(response.status_code, (403, 404), response.content)
        self.assertEqual(
            WorkItem.objects.filter(
                request=self.emailed,
                disposition=WorkItem.Disposition.NEEDS_ATTENTION,
            ).count(),
            2,
        )


class MergeActionTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(
            email="jane@example.org", name="Jane Q. Public"
        )
        self.target = make_request(
            requester=requester,
            description="Web form, received first",
            received_at=timezone.now() - dt.timedelta(days=3),
        )
        self.source = make_request(
            requester=requester,
            description="Emailed the same request again",
            channel="email",
        )
        matched_email(self.source, officer=self.officer, intake=True)
        self.reason = "Same request; the web form came first."

    def _merge(self, **overrides):
        body = {"target_number": self.target.number, "reason": self.reason}
        body.update(overrides)
        return self.client.post(
            f"/api/staff/requests/{self.source.number}/merge/",
            body,
            content_type="application/json",
        )

    def test_merge_returns_the_case_of_record_linked_both_ways(self):
        response = self._merge()

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["number"], self.target.number)
        self.assertEqual(payload["merged_sources"], [self.source.number])
        self.assertIsNone(payload["merged_into"])

        source = self.client.get(
            f"/api/staff/requests/{self.source.number}/"
        ).json()
        self.assertEqual(source["status"], RequestStatus.CLOSED_DUPLICATE)
        self.assertEqual(source["merged_into"], self.target.number)
        self.assertEqual(source["initial_response_plan"]["state"], "merged")

        # The requester's email now lists under the case of record.
        messages = self.client.get(
            "/api/staff/correspondence/", {"request": self.target.number}
        ).json()
        self.assertEqual(messages["count"], 1)

    def test_merge_refuses_an_unknown_target(self):
        response = self._merge(target_number="PRR-2099-9999")

        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json()["code"], "target_not_found")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_merge_refuses_a_short_reason(self):
        response = self._merge(reason="dup")

        self.assertEqual(response.status_code, 400, response.content)

    def test_merge_refuses_a_closed_target(self):
        closed = make_request(
            description="Already fulfilled",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )

        response = self._merge(target_number=closed.number)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "closed_request")


class InboxCountSemanticsTests(StaffApiTestCase):
    """Every chip counts what its list shows: unread means unread AND still
    waiting, and the Cases chip counts open cases, not flagged ones."""

    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.open_web = make_request(description="Web form, nothing to read")
        self.open_emailed = make_request(
            requester=Requester.objects.create(
                email="bob@example.org", name="Bob Smith"
            ),
            description="Emailed request",
            channel="email",
        )
        _, _, self.waiting_row = matched_email(
            self.open_emailed, officer=self.officer, intake=True
        )
        self.closed = make_request(
            description="Closed before the fix",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )
        _, _, self.closed_row = matched_email(
            self.closed, officer=self.officer, intake=True
        )
        # Retire the closed case's row the way a closure now does.
        from apps.work_queue.services import handle_case_inbox_items

        handle_case_inbox_items(
            self.closed, actor=self.officer, reason="request_closed:fulfilled"
        )

    def test_unread_counts_only_rows_still_waiting(self):
        counts = self.client.get("/api/staff/work-items/counts/").json()

        # Nobody opened either row; only the waiting one is news.
        self.assertEqual(counts["inbox_new"], 1)
        listed = self.client.get(
            "/api/staff/correspondence/",
            {
                "read_state": "unread",
                "work_state": "unresolved",
                "work_kind": ["inbound_reply", "email_intake"],
            },
        ).json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["results"][0]["request_number"], self.open_emailed.number)

    def test_cases_chip_counts_open_cases_not_flagged_ones(self):
        counts = self.client.get("/api/staff/work-items/counts/").json()

        self.assertEqual(counts["inbox_open_cases"], 2)
        self.assertEqual(counts["inbox_updated_cases"], 1)
        listed = self.client.get("/api/staff/requests/", {"queue": "inbox"}).json()
        self.assertEqual(listed["count"], counts["inbox_open_cases"])

    def test_message_chips_count_only_messages_on_open_cases(self):
        counts = self.client.get("/api/staff/work-items/counts/").json()

        # The closed case's retired row is Handled, but a closed case is not
        # Inbox business, so neither Handled nor All messages counts it.
        self.assertEqual(counts["inbox_handled"], 0)
        self.assertEqual(counts["inbox_all"], 1)
        for query, key in (
            ({"work_state": "handled"}, "inbox_handled"),
            ({}, "inbox_all"),
        ):
            listed = self.client.get(
                "/api/staff/correspondence/",
                {
                    **query,
                    "case_state": "open",
                    "work_kind": ["inbound_reply", "email_intake"],
                },
            ).json()
            self.assertEqual(listed["count"], counts[key], query)


    def test_named_work_item_queues_match_their_chips(self):
        # The agent-facing named queues answer the same question as the
        # chips, so they leave closed cases out too.
        counts = self.client.get("/api/staff/work-items/counts/").json()
        for queue in ("inbox_handled", "inbox_all", "inbox_needs_attention"):
            listed = self.client.get(
                "/api/staff/work-items/", {"queue": queue}
            ).json()
            self.assertEqual(listed["count"], counts[queue], queue)
            self.assertNotIn(
                self.closed.number,
                {row.get("request_number") for row in listed["results"]},
            )

class CaseStateFilterTests(StaffApiTestCase):
    """"All open" and "All closed" are groups of legal statuses, so an
    officer never has to pick the seven closed statuses one at a time."""

    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.received = make_request(description="Just in")
        self.gathering = make_request(
            description="Collecting", status=RequestStatus.GATHERING
        )
        self.fulfilled = make_request(
            description="Produced",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )
        self.withdrawn = make_request(
            description="Withdrawn",
            status=RequestStatus.CLOSED_WITHDRAWN,
            closed_at=timezone.now(),
        )
        _, self.open_message, _ = matched_email(
            self.received, officer=self.officer, intake=True
        )
        _, self.closed_message, _ = matched_email(
            self.fulfilled, officer=self.officer, intake=True
        )

    def _numbers(self, params):
        response = self.client.get("/api/staff/requests/", params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["number"] for row in response.json()["results"]}

    def test_status_open_lists_every_non_terminal_case(self):
        self.assertEqual(
            self._numbers({"status": "open"}),
            {self.received.number, self.gathering.number},
        )

    def test_status_closed_lists_every_terminal_case(self):
        self.assertEqual(
            self._numbers({"status": "closed"}),
            {self.fulfilled.number, self.withdrawn.number},
        )

    def test_a_single_status_still_filters_exactly(self):
        self.assertEqual(
            self._numbers({"status": RequestStatus.GATHERING}),
            {self.gathering.number},
        )

    def test_no_status_still_lists_every_case(self):
        # The API default is unchanged for agents and saved links; the screen
        # is what defaults to open.
        self.assertEqual(
            self._numbers({}),
            {
                self.received.number, self.gathering.number,
                self.fulfilled.number, self.withdrawn.number,
            },
        )

    def test_the_open_cases_queue_refuses_a_status(self):
        # queue=inbox is already the open band; a status on top of it could
        # only narrow it silently, and "closed" would empty it.
        response = self.client.get(
            "/api/staff/requests/", {"queue": "inbox", "status": "closed"}
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("status", response.json())

    def _message_ids(self, params):
        response = self.client.get("/api/staff/correspondence/", params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()["results"]}

    def test_messages_filter_by_case_state(self):
        self.assertEqual(
            self._message_ids({"case_state": "open"}),
            {str(self.open_message.pk)},
        )
        self.assertEqual(
            self._message_ids({"case_state": "closed"}),
            {str(self.closed_message.pk)},
        )
        self.assertEqual(
            self._message_ids({}),
            {str(self.open_message.pk), str(self.closed_message.pk)},
        )

    def test_messages_reject_an_unknown_case_state(self):
        response = self.client.get(
            "/api/staff/correspondence/", {"case_state": "sideways"}
        )
        self.assertEqual(response.status_code, 400, response.content)
