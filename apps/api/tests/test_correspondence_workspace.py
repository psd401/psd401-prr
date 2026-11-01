import datetime as dt
import re
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.models import StaffRole
from apps.api.pagination import ScopedKeysetPagination
from apps.api.tests.test_correspondence import CorrespondenceApiTestCase
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester
from apps.work_queue import services as work_services
from apps.work_queue.models import WorkItem
from apps.work_queue.reconciliation import reconcile_inbound_receipt


def _normalized_sql(sql):
    """Collapse inlined ids so the same statement compares equal run to run."""
    return re.sub(r"'[0-9a-fA-F-]{32,36}'", "?", sql)[:120]


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CorrespondenceWorkspaceApiTests(CorrespondenceApiTestCase):
    def _inbound(
        self,
        *,
        subject,
        body,
        thread="",
        kind=Correspondence.Kind.GENERAL,
    ):
        return Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=kind,
            status=Correspondence.Status.RECEIVED,
            received_subject=subject,
            received_body=body,
            received_sender="requester@example.org",
            received_recipients=["publicrecords@psd401.net"],
            received_at=timezone.now(),
            matched_at=timezone.now(),
            matched_by=self.officer,
            provider_thread_id=thread,
        )

    def _matched_receipt(
        self,
        correspondence,
        *,
        match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
    ):
        now = timezone.now()
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"receipt-{correspondence.pk}",
            received_at=now,
            envelope_sender="requester@example.org",
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=match_method,
            matched_request=self.prr,
            matched_correspondence=correspondence,
            matched_at=now,
            # An automatic match method records no human matcher; the
            # inbound-state check constraint enforces that pairing.
            matched_by=(
                None
                if match_method
                == InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
                else self.officer
            ),
            quarantined=False,
            raw_message_size=10,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key=f"workspace:{correspondence.pk}",
            submitted_by=self.officer,
            completed_at=now,
        )
        reconcile_inbound_receipt(receipt.pk)
        return receipt

    @staticmethod
    def _path(url):
        parsed = urlparse(url)
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def test_request_list_is_a_bounded_summary_and_detail_is_one_full_record(self):
        messages = [
            Correspondence(
                request=self.prr,
                direction=Correspondence.Direction.OUTBOUND,
                kind=Correspondence.Kind.GENERAL,
                status=Correspondence.Status.DRAFT,
                draft_subject=f"Message {index:03d}",
                draft_body=(f"Bounded snippet {index:03d}. " + "x" * 500),
                created_by=self.officer,
            )
            for index in range(55)
        ]
        Correspondence.objects.bulk_create(messages)
        self.login()

        response = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["count"], 55)
        self.assertEqual(len(payload["results"]), 50)
        self.assertLessEqual(len(response.content), 256 * 1024)
        self.assertIsNotNone(payload["next"])
        row = payload["results"][0]
        self.assertIn("subject", row)
        self.assertLessEqual(len(row["snippet"]), 241)
        for forbidden in (
            "draft_body",
            "approved_body",
            "received_body",
            "received_recipients",
            "recipients",
            "attachments",
            "delivery",
            "provider_message_id",
            "provider_thread_id",
        ):
            self.assertNotIn(forbidden, row)

        selected = self.client.get(
            f"/api/staff/correspondence/{row['id']}/"
        )
        self.assertEqual(selected.status_code, 200, selected.content)
        self.assertIn("draft_body", selected.json())
        self.assertEqual(selected.json()["id"], row["id"])

    def test_cross_case_rows_carry_bounded_case_triage_context(self):
        """An Inbox row must state the case's legal and statutory position.

        Without it the officer cannot tell a live request that owes a
        five-day response from vendor spam, which is exactly the triage the
        cross-case Inbox exists to support.
        """
        self.prr.statutory_response_due = dt.date(2026, 9, 4)
        self.prr.save(update_fields=["statutory_response_due"])
        inbound = self._inbound(subject="Records please", body="Everything.")
        self._matched_receipt(inbound)
        self.login()

        response = self.client.get(
            "/api/staff/correspondence/?work_kind=inbound_reply"
            "&work_kind=email_intake"
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        row = next(row for row in rows if row["id"] == str(inbound.pk))
        self.assertEqual(
            row["case_context"],
            {
                "legal_status": self.prr.status,
                "statutory_response_state": "manual_required",
                "statutory_response_due": "2026-09-04",
                "email_intake_auto_created": False,
            },
        )
        # No requester identity leaks in through the new field.
        self.assertNotIn("jane@example.org", response.content.decode())

    def test_case_context_marks_only_the_auto_created_email_intake_cases(self):
        """The non-request closure is offered exactly where it would succeed."""
        inbound = self._inbound(subject="Webinar invite", body="Register now.")
        self._matched_receipt(
            inbound,
            match_method=(
                InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
            ),
        )
        self.login()

        response = self.client.get(
            "/api/staff/correspondence/?work_kind=inbound_reply"
            "&work_kind=email_intake"
        )

        self.assertEqual(response.status_code, 200, response.content)
        row = next(
            row
            for row in response.json()["results"]
            if row["id"] == str(inbound.pk)
        )
        self.assertTrue(row["case_context"]["email_intake_auto_created"])

    def _case_with_inbound_message(self, index):
        """One more independent case with one inbound message and an ack."""
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email=f"batched-{index}@example.org",
            ),
            description=f"Batched case {index}.",
        )
        now = timezone.now()
        inbound = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject=f"Reply {index}",
            received_body="Body.",
            received_sender="requester@example.org",
            received_recipients=["publicrecords@psd401.net"],
            received_at=now,
            matched_at=now,
            matched_by=self.officer,
        )
        Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            status=Correspondence.Status.DRAFT,
            draft_subject="Acknowledgment",
            draft_body="Received.",
            created_by=self.officer,
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"batched-receipt-{index}",
            received_at=now,
            envelope_sender="requester@example.org",
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            ),
            match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            matched_request=prr,
            matched_correspondence=inbound,
            matched_at=now,
            matched_by=self.officer,
            quarantined=False,
            raw_message_size=10,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key=f"batched:{index}",
            submitted_by=self.officer,
            completed_at=now,
        )
        reconcile_inbound_receipt(receipt.pk)
        return prr

    def test_case_context_is_three_queries_no_matter_how_many_cases(self):
        """The triage context is batched; it must not become per-row queries.

        Every row here belongs to a DIFFERENT request, which is the shape
        that would turn a per-row statutory-plan lookup into an N+1. The
        three queries asserted are the statutory acknowledgment lookup, the
        email-intake provenance lookup, and the statutory-delivery lookup
        that back every row's context.
        """
        for index in range(6):
            self._case_with_inbound_message(index)
        self.login()

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                "/api/staff/correspondence/?work_kind=inbound_reply"
                "&work_kind=email_intake"
            )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["case_context"] is not None for row in rows))
        statements = [query["sql"] for query in captured.captured_queries]
        self.assertEqual(
            len([sql for sql in statements if "\"kind\" = 'acknowledgment'" in sql]),
            1,
        )
        self.assertEqual(
            len([
                sql for sql in statements
                if "\"match_method\" = 'email_intake'" in sql
            ]),
            1,
        )
        # The statutory-delivery answer for the whole page, selected with the
        # same predicate the day-5 safety-net worker uses. Read one row at a
        # time it costs a legacy-import row plus a status-event probe EACH.
        self.assertEqual(
            len([
                sql for sql in statements
                if "records_legacyrequestimport" in sql
            ]),
            1,
        )
        self.assertEqual(
            len([sql for sql in statements if "records_statusevent" in sql]),
            1,
        )

    def test_inbox_page_costs_the_same_queries_at_one_row_and_a_full_page(self):
        """The whole Inbox response must be constant-cost, not just its context.

        The context assertions above pin only the statements
        ``_attach_case_context`` owns, so per-row queries added anywhere else
        in the response stayed invisible to them. Three were: the viewer's
        managed-group lookup behind ``inbound_receipt_id`` (``staff_roles``
        re-reads the group table on every call), and per case the legacy
        import row and the acknowledged/clarification status-event probe
        behind ``initial_response_delivered``. That was roughly three extra
        round trips per row on a surface that pages up to 50.

        Comparing the totals catches any per-row query whatever adds it,
        which a fixed expected count would not.
        """
        self._case_with_inbound_message(0)
        self.login()

        with CaptureQueriesContext(connection) as one_row:
            first = self.client.get(
                "/api/staff/correspondence/?work_kind=inbound_reply"
                "&work_kind=email_intake"
            )

        for index in range(1, 25):
            self._case_with_inbound_message(index)

        with CaptureQueriesContext(connection) as full_page:
            second = self.client.get(
                "/api/staff/correspondence/?work_kind=inbound_reply"
                "&work_kind=email_intake"
            )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(len(first.json()["results"]), 1)
        self.assertEqual(len(second.json()["results"]), 25)
        # Every row is fully populated, so nothing is "constant" merely
        # because the extra rows were served empty.
        self.assertTrue(
            all(
                row["case_context"] is not None
                and row["work_item"] is not None
                and row["inbound_receipt_id"] is not None
                for row in second.json()["results"]
            )
        )

        grew = Counter(
            _normalized_sql(query["sql"])
            for query in full_page.captured_queries
        )
        grew.subtract(
            Counter(
                _normalized_sql(query["sql"])
                for query in one_row.captured_queries
            )
        )
        self.assertEqual(
            len(full_page.captured_queries),
            len(one_row.captured_queries),
            "The Inbox page grew from "
            f"{len(one_row.captured_queries)} queries at 1 row to "
            f"{len(full_page.captured_queries)} at 25. Statements that "
            "repeated per row: "
            + "; ".join(
                f"+{count} {sql}"
                for sql, count in grew.most_common()
                if count > 0
            ),
        )

    def test_omitted_attachment_filter_does_not_mean_without_attachments(self):
        with_attachment = self.general_draft()
        CorrespondenceAttachment.objects.create(
            correspondence=with_attachment,
            original_filename="bounded.txt",
            content_type="text/plain",
            size=7,
            sha256="a" * 64,
        )
        without_attachment = self.general_draft()
        self.login()

        unfiltered = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        )
        with_only = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&has_attachments=true"
        )
        without_only = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&has_attachments=false"
        )

        self.assertEqual(unfiltered.json()["count"], 2)
        self.assertEqual(
            [row["id"] for row in with_only.json()["results"]],
            [str(with_attachment.pk)],
        )
        self.assertEqual(
            [row["id"] for row in without_only.json()["results"]],
            [str(without_attachment.pk)],
        )

    def test_cursor_is_stable_under_timestamp_ties_and_front_inserts_and_scope_bound(self):
        messages = [
            Correspondence(
                request=self.prr,
                direction=Correspondence.Direction.OUTBOUND,
                kind=Correspondence.Kind.GENERAL,
                status=Correspondence.Status.DRAFT,
                draft_subject=f"Cursor {index:03d}",
                draft_body="Cursor body",
                created_by=self.officer,
            )
            for index in range(61)
        ]
        Correspondence.objects.bulk_create(messages)
        tied_at = timezone.now() - dt.timedelta(days=1)
        Correspondence.objects.filter(pk__in=[item.pk for item in messages]).update(
            created_at=tied_at
        )
        expected_ids = {str(item.pk) for item in messages}
        self.login()

        first = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        ).json()
        self.general_draft()
        second = self.client.get(self._path(first["next"])).json()
        traversed_rows = [*first["results"], *second["results"]]
        traversed = {row["id"] for row in traversed_rows}

        self.assertSetEqual(traversed, expected_ids)
        self.assertEqual(len(traversed_rows), len(expected_ids))
        parsed = urlparse(first["next"])
        query = parse_qs(parsed.query)
        query["direction"] = ["inbound"]
        tampered = f"{parsed.path}?{urlencode(query, doseq=True)}"
        self.assertEqual(self.client.get(tampered).status_code, 404)

    def test_search_is_bounded_immutable_and_authorized_before_match_discovery(self):
        immutable_match = self._inbound(
            subject="Harborview project update",
            body="The immutable received body contains lighthouse zebra.",
        )
        draft_only = self.general_draft()
        Correspondence.objects.filter(pk=draft_only.pk).update(
            draft_subject="lighthouse zebra draft",
            draft_body="lighthouse zebra mutable content",
        )
        inaccessible = Correspondence.objects.create(
            request=self.other_prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="lighthouse zebra secret",
            received_body="Inaccessible case content.",
            received_at=timezone.now(),
        )
        del inaccessible
        self.login()

        result = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "lighthouse zebra"},
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(
            [row["id"] for row in result.json()["results"]],
            [str(immutable_match.pk)],
        )
        too_long = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "x" * 201},
            content_type="application/json",
        )
        self.assertEqual(too_long.status_code, 400)
        too_many_atoms = self.client.post(
            "/api/staff/correspondence/search/",
            {
                "request": self.prr.number,
                "query": " ".join(f"term{index}" for index in range(13)),
            },
            content_type="application/json",
        )
        self.assertEqual(too_many_atoms.status_code, 400)
        self.assertIn("search terms", str(too_many_atoms.json()).lower())
        too_many_groups = self.client.post(
            "/api/staff/correspondence/search/",
            {
                "request": self.prr.number,
                "query": " OR ".join(f"term{index}" for index in range(7)),
            },
            content_type="application/json",
        )
        self.assertEqual(too_many_groups.status_code, 400)
        self.assertIn("or groups", str(too_many_groups.json()).lower())
        accepted_boundary = self.client.post(
            "/api/staff/correspondence/search/",
            {
                "request": self.prr.number,
                "query": "one two OR three four OR five six OR seven eight "
                "OR nine ten OR eleven twelve",
            },
            content_type="application/json",
        )
        self.assertEqual(accepted_boundary.status_code, 200)

        for user in (self.admin, self.reviewer):
            self.login(user)
            denied = self.client.post(
                "/api/staff/correspondence/search/",
                {"request": self.prr.number, "query": "lighthouse zebra"},
                content_type="application/json",
            )
            self.assertEqual(denied.status_code, 403)
            self.assertNotIn("lighthouse", str(denied.json()).lower())

        unassigned = get_user_model().objects.create_user(
            "unassigned-liaison",
            is_staff=True,
        )
        from apps.api.identity import apply_staff_roles

        apply_staff_roles(unassigned, {StaffRole.LIAISON})
        self.login(unassigned)
        liaison = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "lighthouse zebra"},
            content_type="application/json",
        )
        self.assertEqual(liaison.status_code, 200)
        self.assertEqual(liaison.json()["count"], 0)

        agent = get_user_model().objects.create_user("workspace-mcp", is_staff=False)
        agent.user_permissions.add(Permission.objects.get(codename="read_staff_api"))
        self.client.logout()
        token_denied = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "lighthouse zebra"},
            content_type="application/json",
            **self.token_headers(agent),
        )
        self.assertEqual(token_denied.status_code, 403)

    def test_work_filters_and_personal_read_state_are_compact_summary_metadata(self):
        inbound = self._inbound(subject="Needs handling", body="Please respond.")
        receipt = self._matched_receipt(inbound)
        item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=str(receipt.pk),
        )
        work_services.assign_work_item(
            item,
            assignee=self.officer,
            actor=self.officer,
            expected_version=1,
            idempotency_key="message-workspace:assign",
        )
        work_services.mark_work_item_read(
            item,
            actor=self.officer,
            idempotency_key="message-workspace:read",
        )
        self.login()

        response = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            f"&work_state=needs_attention&read_state=read&assignee={self.officer.pk}"
            "&work_kind=inbound_reply&work_kind=email_intake"
            "&direction=inbound&kind=general&matching_state=matched"
            "&has_attachments=false"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)
        work = response.json()["results"][0]["work_item"]
        self.assertEqual(work["id"], str(item.pk))
        self.assertEqual(work["work_kind"], WorkItem.WorkKind.INBOUND_REPLY)
        self.assertTrue(work["is_read"])
        self.assertEqual(work["disposition"], "needs_attention")
        self.assertNotIn("events", work)
        self.assertNotIn("no_action_reason", work)

    def test_inbound_work_projection_and_filter_follow_work_item_role_scope(self):
        inbound = self._inbound(
            subject="Officer-only inbound work",
            body="Please have the records officer review this reply.",
        )
        receipt = self._matched_receipt(inbound)
        item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=str(receipt.pk),
        )
        work_services.assign_work_item(
            item,
            assignee=self.officer,
            actor=self.officer,
            expected_version=item.version,
            idempotency_key="message-workspace:officer-only-assignment",
        )

        self.login(self.liaison)
        direct = self.client.get(f"/api/staff/work-items/{item.pk}/")
        summary = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        )
        detail = self.client.get(f"/api/staff/correspondence/{inbound.pk}/")
        filtered = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&work_state=needs_attention"
        )

        self.assertEqual(direct.status_code, 404)
        self.assertEqual(summary.status_code, 200, summary.content)
        self.assertIsNone(summary.json()["results"][0]["work_item"])
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertIsNone(detail.json()["work_item"])
        self.assertEqual(filtered.status_code, 200, filtered.content)
        self.assertEqual(filtered.json()["count"], 0)

        self.login(self.officer)
        visible = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&work_state=needs_attention"
        )
        self.assertEqual(visible.json()["count"], 1)
        self.assertEqual(
            visible.json()["results"][0]["work_item"]["id"],
            str(item.pk),
        )

    def test_unread_filter_is_personal_when_another_officer_has_read_the_item(self):
        inbound = self._inbound(
            subject="Personal unread state",
            body="Each officer tracks this reply independently.",
        )
        receipt = self._matched_receipt(inbound)
        item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=str(receipt.pk),
        )
        observer = get_user_model().objects.create_user(
            "workspace-observer",
            is_staff=True,
        )
        from apps.api.identity import apply_staff_roles

        apply_staff_roles(observer, {StaffRole.RECORDS_OFFICER})
        work_services.mark_work_item_read(
            item,
            actor=observer,
            idempotency_key="message-workspace:observer-read",
        )

        self.login(self.officer)
        officer_unread = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&read_state=unread"
        )
        self.assertEqual(officer_unread.json()["count"], 1)

        self.login(observer)
        observer_unread = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&read_state=unread"
        )
        self.assertEqual(observer_unread.json()["count"], 0)

    def test_thread_summaries_and_thread_messages_are_separately_paginated(self):
        first = self._inbound(
            subject="Thread first",
            body="First full body",
            thread="provider-thread-secret",
        )
        second = self._inbound(
            subject="Thread second",
            body="Second full body",
            thread="provider-thread-secret",
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        receipt = self._matched_receipt(first)
        for index in range(2):
            CorrespondenceAttachment.objects.create(
                correspondence=first,
                inbound_receipt=receipt,
                original_filename=f"thread-{index}.txt",
                content_type="text/plain",
                size=7,
                sha256=str(index) * 64,
            )
        self.login()

        threads = self.client.get(
            f"/api/staff/correspondence/threads/?request={self.prr.number}"
        )
        self.assertEqual(threads.status_code, 200, threads.content)
        self.assertEqual(threads.json()["count"], 1)
        thread = threads.json()["results"][0]
        self.assertEqual(thread["message_count"], 2)
        self.assertRegex(thread["evidence_id"], r"^[0-9a-f]{64}$")
        self.assertNotIn("provider-thread-secret", str(thread))

        repeated_thread = self.client.get(
            f"/api/staff/correspondence/threads/?request={self.prr.number}"
        ).json()["results"][0]
        self.assertEqual(repeated_thread["evidence_id"], thread["evidence_id"])

        messages = self.client.get(
            f"/api/staff/correspondence/thread-messages/?request={self.prr.number}"
            f"&thread={thread['id']}"
        )
        self.assertEqual(messages.status_code, 200, messages.content)
        self.assertSetEqual(
            {row["id"] for row in messages.json()["results"]},
            {str(first.pk), str(second.pk)},
        )
        self.assertNotIn("received_body", messages.json()["results"][0])

        filtered_threads = self.client.get(
            f"/api/staff/correspondence/threads/?request={self.prr.number}"
            "&kind=general&has_attachments=true"
        ).json()
        self.assertEqual(filtered_threads["results"][0]["message_count"], 1)
        self.assertEqual(filtered_threads["results"][0]["subject"], "Thread first")
        filtered_messages = self.client.get(
            f"/api/staff/correspondence/thread-messages/"
            f"?request={self.prr.number}"
            f"&kind=general&has_attachments=true"
            f"&thread={filtered_threads['results'][0]['id']}"
        )
        self.assertEqual(
            [row["id"] for row in filtered_messages.json()["results"]],
            [str(first.pk)],
        )

    def test_thread_page_cursor_uses_an_opaque_record_id_not_provider_metadata(self):
        messages = [
            self._inbound(
                subject=f"Thread {index:03d}",
                body="Thread cursor privacy.",
                thread=f"provider-secret-{index:03d}",
            )
            for index in range(51)
        ]
        self.login()

        response = self.client.get(
            f"/api/staff/correspondence/threads/?request={self.prr.number}"
        )

        self.assertEqual(response.status_code, 200, response.content)
        cursor = parse_qs(urlparse(response.json()["next"]).query)["cursor"][0]
        payload = signing.loads(cursor, salt=ScopedKeysetPagination.cursor_salt)
        self.assertNotIn("provider-secret", str(payload))
        self.assertIn(
            payload["position"][-1],
            {str(message.pk) for message in messages},
        )
