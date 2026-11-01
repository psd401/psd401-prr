# ABOUTME: Live-HTTP tests for PRRClient (integrations/mcp/client.py) — a
# ABOUTME: real Django test server on the wire, no mocks, per house testing rules.
import datetime as dt
from typing import Any

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import LiveServerTestCase, SimpleTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.audit.models import audit
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
    RequestStatus,
    StatusEvent,
)
from integrations.mcp.client import (
    OVERDUE_CONCISE_FIELDS,
    OVERDUE_DEFAULT_LIMIT,
    OVERDUE_MAX_LIMIT,
    OVERDUE_MAX_PAGES,
    PRRClient,
    _validated_iso_date,
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


class PRRClientTests(LiveServerTestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.token = Token.objects.create(user=self.officer)
        self.prr_client = PRRClient(self.live_server_url, self.token.key)

        self.r1 = make_request(
            description="All emails about the stadium project",
            status=RequestStatus.ACKNOWLEDGED,
        )
        self.r2 = make_request(
            requester=Requester.objects.create(email="bob@example.org", name="Bob Smith"),
            description="Budget spreadsheets for the bond measure",
            status=RequestStatus.GATHERING,
        )
        # Seed a historical acknowledged request. Canonical new transitions
        # require the confirmed correspondence-delivery graph, which is tested
        # in the correspondence domain rather than this read-only HTTP client.
        occurred_at = timezone.now()
        StatusEvent.objects.create(
            request=self.r1,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Acknowledged.",
            visible_to_requester=True,
            occurred_at=occurred_at,
        )
        audit(
            "request.status_changed",
            target=self.r1.number,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            occurred_at=occurred_at.isoformat(),
        )

    # -- list_requests ---------------------------------------------------
    def test_list_requests_returns_seeded_rows(self):
        payload = self.prr_client.list_requests()
        rows = payload["results"] if isinstance(payload, dict) else payload
        numbers = {row["number"] for row in rows}
        self.assertIn(self.r1.number, numbers)
        self.assertIn(self.r2.number, numbers)

    def test_list_requests_status_filter(self):
        payload = self.prr_client.list_requests(status=RequestStatus.GATHERING)
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["number"] for row in rows], [self.r2.number])

    def test_list_requests_search_filter(self):
        payload = self.prr_client.list_requests(search="bond measure")
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["number"] for row in rows], [self.r2.number])

    def test_list_requests_ordering_matches_the_staff_ui(self):
        """Rule 10 parity: an agent may order the queue the way the GUI does."""
        payload = self.prr_client.list_requests(ordering="-number")
        rows = payload["results"] if isinstance(payload, dict) else payload
        numbers = [row["number"] for row in rows]
        self.assertEqual(numbers, sorted(numbers, reverse=True))
        self.assertEqual(
            {row["subject_source"] for row in rows}, {"description"}
        )

    def test_list_requests_unknown_ordering_is_not_an_error(self):
        payload = self.prr_client.list_requests(ordering="requester__email")
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertIn(self.r1.number, {row["number"] for row in rows})

    def test_list_requests_page_param_accepted(self):
        payload = self.prr_client.list_requests(page=1)
        self.assertIn("results", payload)
        numbers = {row["number"] for row in payload["results"]}
        self.assertIn(self.r1.number, numbers)

    # -- get_request -------------------------------------------------------
    def test_get_request_includes_status_timeline(self):
        detail = self.prr_client.get_request(self.r1.number)
        self.assertEqual(detail["number"], self.r1.number)
        self.assertEqual(detail["description"], self.r1.description)

        events = detail["events"]
        self.assertGreaterEqual(len(events), 1)
        acked = next(e for e in events if e["to_status"] == RequestStatus.ACKNOWLEDGED)
        self.assertEqual(acked["note"], "Acknowledged.")
        self.assertEqual(acked["actor"], "officer")
        self.assertTrue(acked["visible_to_requester"])

    def test_get_request_unknown_number_raises_http_error(self):
        with self.assertRaises(requests.exceptions.HTTPError):
            self.prr_client.get_request("PRR-2026-9999")

    def test_get_request_rejects_path_escape_before_issuing_http(self):
        with self.assertRaisesRegex(ValueError, "request number"):
            self.prr_client.get_request("../record-items")

    # -- list_overdue_requests ---------------------------------------------
    def test_list_overdue_requests_excludes_future_and_closed(self):
        overdue = make_request(
            requester=Requester.objects.create(email="overdue@example.org"),
            description="Overdue request",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=3),
        )
        make_request(
            requester=Requester.objects.create(email="future@example.org"),
            description="Not due yet",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=3),
        )
        make_request(
            requester=Requester.objects.create(email="closed@example.org"),
            description="Overdue but already closed",
            status=RequestStatus.FULFILLED,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=3),
            closed_at=timezone.now(),
        )

        payload = self.prr_client.list_overdue_requests()

        numbers = {row["number"] for row in payload["results"]}
        self.assertEqual(numbers, {overdue.number})
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["days_overdue"], 3)

    def test_list_overdue_requests_returns_a_bounded_envelope(self):
        payload = self.prr_client.list_overdue_requests()

        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["response_format"], "concise")
        self.assertEqual(payload["limit"], OVERDUE_DEFAULT_LIMIT)
        self.assertFalse(payload["truncated"])
        self.assertTrue(payload["scan_complete"])
        self.assertEqual(payload["pages_scanned"], 1)
        self.assertEqual(payload["as_of"], timezone.localdate().isoformat())
        # A complete, untruncated scan has nothing to caveat.
        self.assertNotIn("warning", payload)
        self.assertNotIn("note", payload)

    def test_concise_overdue_rows_always_carry_every_concise_field(self):
        make_request(
            requester=Requester.objects.create(email="shape@example.org"),
            description="Overdue request",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=1),
        )

        row = self.prr_client.list_overdue_requests()["results"][0]

        # Present, not merely "present when the API happened to send it": a
        # serializer rename must surface as a null, not as a vanished column.
        for field in OVERDUE_CONCISE_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, row)
        self.assertEqual(row["status"], RequestStatus.GATHERING)
        self.assertEqual(row["days_overdue"], 1)

    def test_concise_overdue_rows_carry_subject_and_requester_contact(self):
        make_request(
            requester=Requester.objects.create(
                email="concise@example.org", name="Concise Requester",
            ),
            description="Overdue request",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=1),
        )

        concise = self.prr_client.list_overdue_requests()
        detailed = self.prr_client.list_overdue_requests(
            response_format="detailed",
        )

        # The data owner (Hagel, CIO) signed off on 2026-10-19 that MCP tool
        # results may carry the subject and requester contact to the
        # caller's model provider, so the concise view returns them; see
        # docs/phase-2-production-handoffs.md.
        row = concise["results"][0]
        self.assertEqual(row["subject"], "Overdue request")
        self.assertEqual(row["requester_email"], "concise@example.org")
        self.assertEqual(row["requester_name"], "Concise Requester")
        # Detailed is still the full staff row, not just the concise fields.
        self.assertIn("subject_source", detailed["results"][0])
        self.assertNotIn("subject_source", row)
        self.assertEqual(
            detailed["results"][0]["requester_email"], "concise@example.org",
        )
        self.assertEqual(detailed["results"][0]["subject"], "Overdue request")
        self.assertEqual(detailed["response_format"], "detailed")

    def test_overdue_limit_truncates_and_says_so(self):
        for index in range(3):
            make_request(
                requester=Requester.objects.create(
                    email=f"many{index}@example.org",
                ),
                description=f"Overdue request {index}",
                status=RequestStatus.GATHERING,
                statutory_response_due=(
                    timezone.localdate() - dt.timedelta(days=index + 1)
                ),
            )

        payload = self.prr_client.list_overdue_requests(limit=2)

        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["limit"], 2)
        self.assertTrue(payload["truncated"])
        # The whole point of counting separately from shaping: `limit` caps
        # the rows, never the number a statutory-deadline report turns on.
        self.assertEqual(payload["total"], 3)
        # The queue was read to the end, so `total` is the real number.
        self.assertTrue(payload["scan_complete"])
        self.assertNotIn("warning", payload)
        # And the caveat travels as prose, which survives being paraphrased
        # to an officer in a way a boolean does not.
        self.assertIn("2 most urgent of 3 overdue", payload["note"])

    def test_overdue_rejects_an_unknown_response_format_with_the_vocabulary(self):
        with self.assertRaisesRegex(ValueError, "'concise'.*'detailed'"):
            self.prr_client.list_overdue_requests(response_format="summary")

    def test_overdue_rejects_a_limit_outside_the_documented_range(self):
        for bad in (0, -1, OVERDUE_MAX_LIMIT + 1):
            with self.subTest(limit=bad):
                with self.assertRaisesRegex(ValueError, "limit must be"):
                    self.prr_client.list_overdue_requests(limit=bad)

    def test_overdue_rejects_a_non_integer_limit(self):
        # Typed Any on purpose: these are the values a model actually sends
        # when it guesses -- a numeric string, a float, a bool -- and the
        # point of the test is that the runtime guard catches what the type
        # annotation alone cannot.
        wrong_types: list[Any] = ["10", 10.5, True]
        for bad in wrong_types:
            with self.subTest(limit=bad):
                with self.assertRaisesRegex(ValueError, "limit must be"):
                    self.prr_client.list_overdue_requests(limit=bad)

    # -- list_collection_tasks ----------------------------------------------
    def test_list_collection_tasks_filters_by_request_number(self):
        system = RecordSystem.objects.create(slug="sis", name="Student Info System")
        CollectionTask.objects.create(
            request=self.r1, system=system, kind=CollectionTask.Kind.MANUAL,
            instructions="Search SIS.",
        )
        CollectionTask.objects.create(
            request=self.r2, kind=CollectionTask.Kind.MANUAL,
            instructions="Unrelated task on a different request.",
        )

        payload = self.prr_client.list_collection_tasks(request_number=self.r1.number)

        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_number"], self.r1.number)
        self.assertEqual(rows[0]["system_slug"], "sis")

    def test_list_collection_tasks_without_filter_returns_all(self):
        CollectionTask.objects.create(
            request=self.r1, kind=CollectionTask.Kind.MANUAL, instructions="a")
        CollectionTask.objects.create(
            request=self.r2, kind=CollectionTask.Kind.MANUAL, instructions="b")

        payload = self.prr_client.list_collection_tasks()

        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual(len(rows), 2)

    # -- list_record_systems -------------------------------------------------
    def test_list_record_systems_after_seed_command(self):
        call_command("seed_systems")

        payload = self.prr_client.list_record_systems()

        rows = payload["results"] if isinstance(payload, dict) else payload
        slugs = {row["slug"] for row in rows}
        self.assertIn("google-workspace", slugs)
        self.assertIn("bus-video", slugs)

    # -- list_exemptions ----------------------------------------------------
    def test_list_exemptions_after_seed_command(self):
        call_command("seed_exemptions")

        payload = self.prr_client.list_exemptions()

        rows = payload["results"] if isinstance(payload, dict) else payload
        citations = {row["citation"] for row in rows}
        self.assertIn("RCW 42.56.230(1)", citations)

    # -- search_audit_log -----------------------------------------------------
    def test_search_audit_log_by_target(self):
        payload = self.prr_client.search_audit_log(target=self.r1.number)

        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertTrue(rows)
        self.assertTrue(all(self.r1.number in row["target"] for row in rows))
        self.assertTrue(any(row["action"] == "request.status_changed" for row in rows))

    def test_search_audit_log_without_target_returns_all(self):
        payload = self.prr_client.search_audit_log()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertTrue(rows)

    # -- agent capability discovery -----------------------------------------
    def test_list_capabilities_returns_gui_parity_contract(self):
        payload = self.prr_client.list_capabilities()

        self.assertEqual(payload["source"], "https://every.to/guides/agent-native")
        ids = {capability["id"] for capability in payload["capabilities"]}
        self.assertIn("requests.list", ids)
        self.assertIn("correspondence.draft_estimate_revision", ids)
        self.assertNotIn("requests.revise_estimate", ids)

    # -- auth failures surface as ordinary HTTP errors -----------------------
    def test_non_staff_token_raises_http_error(self):
        civilian = get_user_model().objects.create_user(
            "civilian", "civilian@example.com", "pw", is_staff=False)
        civilian_token = Token.objects.create(user=civilian)
        civilian_client = PRRClient(self.live_server_url, civilian_token.key)

        with self.assertRaises(requests.exceptions.HTTPError):
            civilian_client.list_requests()


class OverdueScanBoundsTests(SimpleTestCase):
    """The bounded page walk itself, without a server or a mocking library.

    `PRRClientTests` above is deliberately mock-free and talks to a real
    Django test server. Proving the OVERDUE_MAX_PAGES ceiling that way would
    need a backlog of two thousand requests, so the walk is exercised here
    against a hand-written stub subclass instead: a test double for the one
    paginated read, not a patched HTTP layer.
    """

    class _EndlessPages(PRRClient):
        """A client whose request list never runs out of pages."""

        def __init__(self, overdue_due):
            # Deliberately skips PRRClient.__init__: this stub never opens a
            # session, and _validated_base_url would reject a placeholder.
            self.overdue_due = overdue_due
            self.pages_served = 0

        def list_requests(
            self,
            status=None,
            search=None,
            page=None,
            ordering=None,
        ):
            self.pages_served += 1
            return {
                "results": [
                    {
                        "number": f"PRR-2026-{self.pages_served:04d}",
                        "status": RequestStatus.GATHERING,
                        "statutory_response_due": self.overdue_due.isoformat(),
                    },
                ],
                "next": "https://example.invalid/api/staff/requests/?page=2",
            }

    def test_scan_stops_at_the_page_ceiling_and_reports_an_incomplete_scan(self):
        client = self._EndlessPages(dt.date(2026, 1, 1))

        payload = client.list_overdue_requests(limit=OVERDUE_MAX_LIMIT)

        self.assertEqual(payload["pages_scanned"], OVERDUE_MAX_PAGES)
        self.assertEqual(client.pages_served, OVERDUE_MAX_PAGES)
        self.assertEqual(payload["count"], OVERDUE_MAX_PAGES)
        self.assertEqual(payload["total"], OVERDUE_MAX_PAGES)
        # The ceiling stopped the walk, so `total` is a floor. This is the
        # one state where nothing was truncated yet the number is still not
        # the number overdue -- exactly the case a reader who only checks
        # `truncated` would get wrong, so the warning must be present.
        self.assertFalse(payload["scan_complete"])
        self.assertFalse(payload["truncated"])
        self.assertIn("INCOMPLETE SCAN", payload["warning"])
        self.assertIn("is a floor", payload["warning"])
        self.assertNotIn("note", payload)

    def test_the_count_is_not_capped_by_the_row_limit(self):
        client = self._EndlessPages(dt.date(2026, 1, 1))

        payload = client.list_overdue_requests(limit=5)

        # 40 pages scanned, one overdue row each: five rows shaped, forty
        # counted. A `limit` that also capped the count would have reported
        # five overdue requests when there were at least forty.
        self.assertEqual(payload["count"], 5)
        self.assertEqual(payload["total"], OVERDUE_MAX_PAGES)
        self.assertTrue(payload["truncated"])
        self.assertFalse(payload["scan_complete"])


class IsoDateValidationTests(SimpleTestCase):
    """The date guard in front of the two drafting POSTs.

    Both drafting tools document their date argument as ``YYYY-MM-DD``, and
    both send it to a service that treats the value as a statutory date. The
    guard is tested directly because the interesting inputs never arrive over
    MCP -- the tool schema types them as strings -- but do arrive from code
    importing ``PRRClient``, which the module supports.
    """

    def test_a_dashed_iso_date_is_accepted(self):
        self.assertEqual(
            _validated_iso_date("2026-10-20", field="estimated_completion"),
            dt.date(2026, 10, 20),
        )

    def test_the_compact_form_is_rejected_though_fromisoformat_takes_it(self):
        # date.fromisoformat accepts "20261020" on Python 3.12. Accepting it
        # here would mean the documented format was not the only one, and an
        # integer stringified to eight digits would sail through.
        with self.assertRaisesMessage(ValueError, "YYYY-MM-DD"):
            _validated_iso_date("20261020", field="estimated_completion")

    def test_a_non_string_is_rejected_rather_than_stringified(self):
        for value in (20261020, dt.date(2026, 10, 20), ["2026-10-20"]):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ValueError, "YYYY-MM-DD"):
                    _validated_iso_date(value, field="clarification_due")

    def test_the_error_names_the_caller_s_own_argument(self):
        with self.assertRaisesMessage(
            ValueError,
            "clarification_due must be an ISO date (YYYY-MM-DD).",
        ):
            _validated_iso_date("next Friday", field="clarification_due")
