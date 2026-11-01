"""The staff notes endpoint, its role scope, and the list column it feeds."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records import services
from apps.records.models import (
    CASE_NOTE_MAX_LENGTH,
    CaseNote,
    PublicRecordsRequest,
    Requester,
)
from apps.work_queue.models import WorkItem


def make_case(description="Every email about the stadium project", **kw):
    requester = Requester.objects.create(
        email="requester@example.org", name="Jane Q. Public"
    )
    return PublicRecordsRequest.objects.create(
        requester=requester, description=description, **kw
    )


class CaseNotesEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "notes-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = make_case()
        self.client.force_login(self.officer)

    def url(self, prr=None):
        return f"/api/staff/requests/{(prr or self.prr).number}/notes/"

    def test_posting_appends_a_note_and_returns_it(self):
        response = self.client.post(
            self.url(),
            {"body": "Called the custodian; video pulled Tuesday."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(
            payload["body"], "Called the custodian; video pulled Tuesday."
        )
        self.assertEqual(payload["author_username"], "notes-officer")
        self.assertEqual(CaseNote.objects.count(), 1)

    def test_the_list_is_newest_first(self):
        for body in ("first", "second", "third"):
            services.add_case_note(self.prr, actor=self.officer, body=body)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["body"] for row in rows], ["third", "second", "first"])

    def test_the_list_is_paginated(self):
        response = self.client.get(self.url())
        payload = response.json()
        self.assertIn("count", payload)
        self.assertIn("results", payload)

    def test_an_empty_note_is_refused(self):
        response = self.client.post(
            self.url(), {"body": "   "}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CaseNote.objects.exists())

    def test_an_overlong_note_is_refused(self):
        response = self.client.post(
            self.url(),
            {"body": "x" * (CASE_NOTE_MAX_LENGTH + 1)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CaseNote.objects.exists())

    def test_a_note_cannot_be_rewritten_or_deleted_through_the_api(self):
        services.add_case_note(self.prr, actor=self.officer, body="On record.")
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(self.url())
                self.assertIn(response.status_code, (403, 404, 405))

    def test_a_service_refusal_is_a_400_and_not_a_500(self):
        """The serializer and the service bound a note the same way today, so
        nothing reaches the service that it then rejects. That is a
        coincidence of two numbers agreeing, not a guarantee: the next rule
        anybody adds to `add_case_note` -- a blocked pattern, a per-case cap --
        lands here first, and it has to read as a refusal rather than as the
        application falling over."""
        with patch(
            "apps.records.services.add_case_note",
            side_effect=ValidationError("Not acceptable."),
        ):
            response = self.client.post(
                self.url(),
                {"body": "A perfectly ordinary note."},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Not acceptable.", response.json()["detail"])

    def test_anonymous_and_non_staff_callers_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 403)

        get_user_model().objects.create_user(
            "civilian", "civilian@example.com", "pw", is_staff=False
        )
        self.client.login(username="civilian", password="pw")
        self.assertEqual(self.client.get(self.url()).status_code, 403)


class CaseNotesScopeTests(TestCase):
    """A liaison reads and writes notes on exactly the cases they may open."""

    def setUp(self):
        User = get_user_model()
        self.liaison = User.objects.create_user(
            "notes-liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.officer = User.objects.create_user(
            "scope-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.assigned = make_case(description="Assigned to the liaison")
        self.unassigned = make_case(description="Somebody else's case")
        WorkItem.objects.create(
            request=self.assigned,
            work_kind=WorkItem.WorkKind.COLLECTION,
            assignee=self.liaison,
        )
        self.client.force_login(self.liaison)

    def url(self, prr):
        return f"/api/staff/requests/{prr.number}/notes/"

    def test_a_liaison_reads_notes_on_an_assigned_case(self):
        services.add_case_note(
            self.assigned, actor=self.officer, body="Please pull route 12."
        )

        response = self.client.get(self.url(self.assigned))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["body"] for row in rows], ["Please pull route 12."])

    def test_a_liaison_writes_a_note_on_an_assigned_case(self):
        response = self.client.post(
            self.url(self.assigned),
            {"body": "Pulled it; handing to review."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

    def test_a_liaison_cannot_read_notes_on_an_unassigned_case(self):
        services.add_case_note(
            self.unassigned, actor=self.officer, body="Not for this liaison."
        )

        response = self.client.get(self.url(self.unassigned))

        self.assertEqual(response.status_code, 404)

    def test_a_liaison_cannot_write_a_note_on_an_unassigned_case(self):
        response = self.client.post(
            self.url(self.unassigned),
            {"body": "Should not land."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CaseNote.objects.exists())


class LatestNoteColumnTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "column-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)

    def rows(self):
        response = self.client.get("/api/staff/requests/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        return payload["results"] if isinstance(payload, dict) else payload

    def test_a_case_with_no_note_reports_none(self):
        make_case()
        self.assertIsNone(self.rows()[0]["latest_note"])

    def test_the_newest_note_is_the_one_shown(self):
        prr = make_case()
        services.add_case_note(prr, actor=self.officer, body="The older note.")
        services.add_case_note(prr, actor=self.officer, body="The newer note.")

        latest = self.rows()[0]["latest_note"]

        self.assertEqual(latest["body_snippet"], "The newer note.")
        self.assertEqual(latest["author_username"], "column-officer")
        self.assertIsNotNone(latest["created_at"])

    def test_a_long_note_is_collapsed_to_a_snippet(self):
        prr = make_case()
        services.add_case_note(
            prr,
            actor=self.officer,
            # Newlines and runs of spaces are what a pasted email looks like.
            body="Spoke   to the\n\ncustodian. " + "detail " * 60,
        )

        snippet = self.rows()[0]["latest_note"]["body_snippet"]

        self.assertTrue(snippet.startswith("Spoke to the custodian."))
        self.assertNotIn("\n", snippet)
        self.assertTrue(snippet.endswith("…"))
        self.assertLessEqual(len(snippet), 161)

    def note_queries(self, expected_rows):
        """Queries the list issues that touch the note table."""
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(len(self.rows()), expected_rows)
        return [
            entry["sql"]
            for entry in captured.captured_queries
            if "records_casenote" in entry["sql"]
        ]

    def test_the_column_costs_one_query_however_many_rows(self):
        """Fifty cases is one page. A note lookup per row is fifty-one
        queries to answer a glance, which is how a work queue stops being
        opened at all.

        Counted against the note table specifically rather than against the
        whole response: the list has its own per-row costs that predate this
        column, and a total that drifts for an unrelated reason would make
        this assertion useless exactly when it mattered.
        """
        for index in range(3):
            prr = make_case(description=f"Case {index}")
            services.add_case_note(
                prr, actor=self.officer, body=f"Note for case {index}"
            )
        self.assertEqual(len(self.note_queries(3)), 1)

        for index in range(3, 12):
            prr = make_case(description=f"Case {index}")
            services.add_case_note(
                prr, actor=self.officer, body=f"Note for case {index}"
            )

        # Four times the rows, each with a note of its own, still one query.
        self.assertEqual(len(self.note_queries(12)), 1)
