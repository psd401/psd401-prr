import datetime as dt
import hashlib
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import PersonalAccessToken, StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import Correspondence, InboundCorrespondenceReceipt
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    StatusEvent,
)
from apps.work_queue.models import WorkItem, WorkItemEvent
from apps.work_queue.services import (
    assign_work_item,
    materialize_work_item,
    set_no_action_required,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class WorkItemApiTests(TestCase):
    def setUp(self):
        self.officer = self.make_staff("work-api-officer", StaffRole.RECORDS_OFFICER)
        self.second_officer = self.make_staff(
            "work-api-second-officer",
            StaffRole.RECORDS_OFFICER,
        )
        self.liaison = self.make_staff("work-api-liaison", StaffRole.LIAISON)
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Sensitive Requester",
                email="sensitive.requester@example.org",
            ),
            description="Sensitive immutable request description.",
        )
        self.correspondence = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Matched requester reply",
            received_body="Retained inbound evidence.",
            received_at=timezone.now(),
            matched_at=timezone.now(),
            provider_message_id="work-api-primary-message",
        )
        self.receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="work-api-primary-receipt",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method="reply_token",
            matched_request=self.request,
            matched_correspondence=self.correspondence,
            matched_at=timezone.now(),
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="1" * 64,
            submitted_by=self.officer,
            idempotency_key="work-api-primary-receipt",
            payload_sha256="2" * 64,
            completed_at=timezone.now(),
        )
        self.item = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=self.receipt.pk,
            work_kind=WorkItem.WorkKind.INBOUND_REPLY,
            request=self.request,
        ).work_item
        self.client.force_login(self.officer)

    @staticmethod
    def make_staff(username, role):
        user = get_user_model().objects.create_user(
            username,
            f"{username}@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(user, {role})
        return user

    def personal_token_auth(self):
        key = "prr_pat_abcdefghijkl_workitemsecret"
        PersonalAccessToken.objects.create(
            user=self.officer,
            name="Work item API",
            prefix="abcdefghijkl",
            token_hash=hashlib.sha256(key.encode()).hexdigest(),
            expires_at=timezone.now() + dt.timedelta(days=1),
        )
        return {"HTTP_AUTHORIZATION": f"Token {key}"}

    def shared_agent_auth(self):
        principal = get_user_model().objects.create_user("work-api-shared-mcp")
        principal.set_unusable_password()
        principal.save(update_fields=["password"])
        principal.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="records",
                codename="read_staff_api",
            )
        )
        token = Token.objects.create(user=principal)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    def test_list_and_detail_are_pii_minimal_and_include_personal_read_and_history(self):
        read = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/read/",
            {"idempotency_key": "api-read:item:1"},
            content_type="application/json",
        )
        self.assertEqual(read.status_code, 200, read.content)

        listing = self.client.get("/api/staff/work-items/")
        detail = self.client.get(f"/api/staff/work-items/{self.item.pk}/")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        row = listing.json()["results"][0]
        self.assertTrue(row["is_read"])
        self.assertEqual(row["request_number"], self.request.number)
        self.assertEqual(
            row["source_url"],
            f"/requests/{self.request.number}/messages?message={self.correspondence.pk}",
        )
        self.assertNotIn("requester", row)
        self.assertNotIn("description", row)
        serialized = str(listing.json()) + str(detail.json())
        self.assertNotIn("Sensitive Requester", serialized)
        self.assertNotIn("sensitive.requester@example.org", serialized)
        self.assertNotIn("Sensitive immutable request description", serialized)
        self.assertGreaterEqual(len(detail.json()["events"]), 2)
        self.assertNotIn("idempotency_key", detail.json()["events"][-1])

    def test_source_urls_open_the_exact_matched_message_task_and_record(self):
        correspondence = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Exact matched message",
            received_body="Retained evidence.",
            received_at=timezone.now(),
            matched_at=timezone.now(),
            provider_message_id="work-item-exact-message",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="work-item-exact-receipt",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method="reply_token",
            matched_request=self.request,
            matched_correspondence=correspondence,
            matched_at=timezone.now(),
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="a" * 64,
            submitted_by=self.officer,
            idempotency_key="work-item-exact-receipt",
            payload_sha256="b" * 64,
            completed_at=timezone.now(),
        )
        inbound = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=receipt.pk,
            work_kind=WorkItem.WorkKind.INBOUND_REPLY,
            request=self.request,
        ).work_item
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
        )
        collection = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=task.pk,
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        record = RecordItem.objects.create(
            request=self.request,
            storage_key="records/exact.pdf",
            sha256="c" * 64,
        )
        review = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=record.pk,
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item

        urls = {
            str(inbound.pk): (
                f"/requests/{self.request.number}/messages?message={correspondence.pk}"
            ),
            str(collection.pk): (
                f"/requests/{self.request.number}/collect?task={task.pk}"
            ),
            str(review.pk): (
                f"/requests/{self.request.number}/records?item={record.pk}"
            ),
        }
        response = self.client.get(
            "/api/staff/work-items/",
            {"queue": "team"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row["id"]: row for row in response.json()["results"]}
        for work_item_id, expected_url in urls.items():
            with self.subTest(work_item_id=work_item_id):
                self.assertEqual(rows[work_item_id]["source_url"], expected_url)

    def test_unmatched_inbound_source_url_opens_the_canonical_inbox_review(self):
        correspondence = Correspondence.objects.create(
            request=None,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject="Unmatched inbound message",
            received_body="Retained unmatched evidence.",
            received_at=timezone.now(),
            provider_message_id="work-item-unmatched-message",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="work-item-unmatched-receipt",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
            match_method="",
            matched_correspondence=correspondence,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="d" * 64,
            submitted_by=self.officer,
            idempotency_key="work-item-unmatched-receipt",
            payload_sha256="e" * 64,
            completed_at=timezone.now(),
        )
        work = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=receipt.pk,
            work_kind=WorkItem.WorkKind.INBOUND_UNMATCHED,
            request=None,
        ).work_item

        response = self.client.get("/api/staff/work-items/", {"queue": "inbox_unmatched"})

        self.assertEqual(response.status_code, 200, response.content)
        row = next(item for item in response.json()["results"] if item["id"] == str(work.pk))
        self.assertEqual(
            row["source_url"],
            f"/inbox?view=unmatched&inbound_receipt={receipt.pk}",
        )

    def test_candidate_inbound_source_url_does_not_open_an_unmatched_case_message(self):
        correspondence = Correspondence.objects.create(
            request=None,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject="Candidate inbound message",
            received_body="Retained candidate evidence.",
            received_at=timezone.now(),
            provider_message_id="work-item-candidate-message",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="work-item-candidate-receipt",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
            match_method=InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
            candidate_request=self.request,
            matched_correspondence=correspondence,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="f" * 64,
            submitted_by=self.officer,
            idempotency_key="work-item-candidate-receipt",
            payload_sha256="0" * 64,
            completed_at=timezone.now(),
        )
        work = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=receipt.pk,
            work_kind=WorkItem.WorkKind.INBOUND_CANDIDATE,
            request=self.request,
        ).work_item

        response = self.client.get("/api/staff/work-items/", {"queue": "inbox_unmatched"})

        self.assertEqual(response.status_code, 200, response.content)
        row = next(item for item in response.json()["results"] if item["id"] == str(work.pk))
        self.assertEqual(
            row["source_url"],
            f"/inbox?view=unmatched&inbound_receipt={receipt.pk}",
        )

    def test_quarantined_inbound_source_url_opens_the_quarantined_inbox_view(self):
        source_id = uuid.uuid4()
        work = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=source_id,
            work_kind=WorkItem.WorkKind.INBOUND_QUARANTINED,
            request=None,
        ).work_item

        response = self.client.get(
            "/api/staff/work-items/",
            {"queue": "inbox_quarantined"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row = next(
            item
            for item in response.json()["results"]
            if item["id"] == str(work.pk)
        )
        self.assertEqual(
            row["source_url"],
            f"/inbox?view=quarantined&inbound_receipt={source_id}",
        )

    def test_named_shell_queues_are_real_role_scoped_and_urgency_ordered(self):
        today = timezone.localdate()
        self.request.statutory_response_due = today + dt.timedelta(days=4)
        self.request.save(update_fields=["statutory_response_due"])
        assigned = assign_work_item(
            self.item,
            assignee=self.officer,
            actor=self.officer,
            expected_version=1,
            idempotency_key="api-queue:assign-primary",
        ).work_item

        critical_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Critical Requester",
                email="critical@example.org",
            ),
            description="Critical immutable description.",
            statutory_response_due=today - dt.timedelta(days=1),
        )
        critical = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=critical_request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=critical_request,
        ).work_item
        assign_work_item(
            critical,
            assignee=self.officer,
            actor=self.officer,
            expected_version=1,
            idempotency_key="api-queue:assign-critical",
        )
        waiting = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        waiting.disposition = WorkItem.Disposition.WAITING
        waiting.save(update_fields=["disposition", "updated_at"])

        personal = self.client.get("/api/staff/work-items/", {"queue": "personal"})
        team = self.client.get("/api/staff/work-items/", {"queue": "team"})
        unassigned = self.client.get("/api/staff/work-items/", {"queue": "unassigned"})
        waiting_response = self.client.get(
            "/api/staff/work-items/",
            {"queue": "waiting"},
        )
        critical_response = self.client.get(
            "/api/staff/work-items/",
            {"queue": "critical"},
        )

        self.assertEqual(personal.status_code, 200, personal.content)
        self.assertEqual(
            [row["id"] for row in personal.json()["results"]],
            [str(critical.pk), str(assigned.pk)],
        )
        self.assertEqual(team.json()["results"][0]["id"], str(critical.pk))
        self.assertEqual(
            [row["id"] for row in unassigned.json()["results"]],
            [str(waiting.pk)],
        )
        self.assertEqual(
            [row["id"] for row in waiting_response.json()["results"]],
            [str(waiting.pk)],
        )
        self.assertEqual(
            [row["id"] for row in critical_response.json()["results"]],
            [str(critical.pk)],
        )
        first = team.json()["results"][0]
        self.assertEqual(first["urgency_date"], str(today - dt.timedelta(days=1)))
        self.assertEqual(first["next_action"], "Respond to request")
        self.assertEqual(first["detail_url"], f"/work/{critical.pk}")
        self.assertNotIn("Critical Requester", str(team.json()))
        self.assertNotIn("critical@example.org", str(team.json()))

    def test_named_queue_rejects_unknown_or_conflicting_filters(self):
        unknown = self.client.get(
            "/api/staff/work-items/",
            {"queue": "invented"},
        )
        conflicting = self.client.get(
            "/api/staff/work-items/",
            {"queue": "personal", "disposition": "handled"},
        )

        self.assertEqual(unknown.status_code, 400, unknown.content)
        self.assertEqual(unknown.json()["queue"], ["Unknown named queue."])
        self.assertEqual(conflicting.status_code, 400, conflicting.content)
        self.assertEqual(
            conflicting.json()["disposition"],
            ["Do not combine disposition with a named queue."],
        )

    def test_critical_queue_uses_authoritative_collection_due_date(self):
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            due_date=timezone.localdate() - dt.timedelta(days=1),
        )
        collection = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=task.pk,
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item

        queue = self.client.get("/api/staff/work-items/", {"queue": "critical"})
        counts = self.client.get("/api/staff/work-items/counts/")

        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertIn(str(collection.pk), [row["id"] for row in queue.json()["results"]])
        self.assertEqual(counts.status_code, 200, counts.content)
        self.assertEqual(counts.json()["critical"], 1)

    def test_non_request_work_never_reuses_the_initial_response_deadline(self):
        self.request.statutory_response_due = timezone.localdate() - dt.timedelta(days=1)
        self.request.estimated_completion = timezone.localdate() + dt.timedelta(days=10)
        self.request.save(
            update_fields=["statutory_response_due", "estimated_completion"]
        )
        review = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item

        team = self.client.get("/api/staff/work-items/", {"queue": "team"})
        critical = self.client.get("/api/staff/work-items/", {"queue": "critical"})

        self.assertEqual(team.status_code, 200, team.content)
        rows = {row["id"]: row for row in team.json()["results"]}
        self.assertEqual(
            rows[str(review.pk)]["urgency_date"],
            str(self.request.estimated_completion),
        )
        self.assertNotIn(
            str(review.pk),
            [row["id"] for row in critical.json()["results"]],
        )

    def test_recently_changed_queue_is_a_documented_seven_day_window(self):
        stale = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item
        WorkItem.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now() - dt.timedelta(days=8),
        )

        response = self.client.get(
            "/api/staff/work-items/",
            {"queue": "recently_changed"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        ids = [row["id"] for row in response.json()["results"]]
        self.assertIn(str(self.item.pk), ids)
        self.assertNotIn(str(stale.pk), ids)

    def test_shell_counts_cover_work_review_and_inbox_without_cross_role_leakage(self):
        review = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item
        delivery = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        ).work_item
        assign_work_item(
            self.item,
            assignee=self.officer,
            actor=self.officer,
            expected_version=1,
            idempotency_key="api-counts:assign-inbound",
        )

        counts = self.client.get("/api/staff/work-items/counts/")

        self.assertEqual(counts.status_code, 200, counts.content)
        payload = counts.json()
        self.assertEqual(payload["personal"], 1)
        self.assertEqual(payload["team"], 3)
        self.assertEqual(payload["review"], 1)
        self.assertEqual(payload["inbox_needs_attention"], 1)
        self.assertEqual(payload["inbox_delivery_exceptions"], 1)
        self.assertEqual(payload["inbox_new"], 1)
        self.assertEqual(payload["inbox_waiting"], 0)

        self.client.force_login(self.liaison)
        liaison_counts = self.client.get("/api/staff/work-items/counts/")
        self.assertEqual(liaison_counts.status_code, 200, liaison_counts.content)
        self.assertEqual(liaison_counts.json()["team"], 0)
        self.assertEqual(liaison_counts.json()["review"], 0)
        self.assertEqual(liaison_counts.json()["inbox_needs_attention"], 0)
        self.assertTrue(review.pk)
        self.assertTrue(delivery.pk)

    def test_every_named_view_count_matches_its_list_universe(self):
        materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        )
        materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        )
        counts = self.client.get("/api/staff/work-items/counts/").json()

        for queue in (
            "personal",
            "team",
            "unassigned",
            "waiting",
            "critical",
            "recently_changed",
            "review",
            "inbox_needs_attention",
            "inbox_new",
            "inbox_unmatched",
            "inbox_quarantined",
            "inbox_delivery_exceptions",
            "inbox_waiting",
            "inbox_handled",
            "inbox_no_action",
            "inbox_all",
        ):
            with self.subTest(queue=queue):
                response = self.client.get(
                    "/api/staff/work-items/",
                    {"queue": queue},
                )
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["count"], counts[queue])

    def test_message_inbox_counts_match_the_cursor_bounded_message_views(self):
        counts = self.client.get("/api/staff/work-items/counts/").json()
        base = "work_kind=inbound_reply&work_kind=email_intake"
        views = {
            "inbox_needs_attention": f"work_state=needs_attention&{base}",
            "inbox_new": f"read_state=unread&work_state=unresolved&{base}",
            "inbox_waiting": f"work_state=waiting&{base}",
            "inbox_handled": f"work_state=handled&{base}",
            "inbox_no_action": f"work_state=no_action_required&{base}",
            "inbox_all": base,
        }

        for count_key, query in views.items():
            with self.subTest(count_key=count_key):
                response = self.client.get(f"/api/staff/correspondence/?{query}")
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["count"], counts[count_key])

    def test_assignment_targets_are_role_valid_pii_minimal_and_object_scoped(self):
        reviewer = self.make_staff("work-api-reviewer", StaffRole.REVIEWER)
        admin = self.make_staff("work-api-admin", StaffRole.ADMIN)
        intake = self.make_staff("work-api-intake", StaffRole.INTAKE_CLERK)
        collection = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        assign_work_item(
            collection,
            assignee=self.liaison,
            actor=self.officer,
            expected_version=1,
            idempotency_key="api-targets:assign-liaison",
        )

        response = self.client.get(
            f"/api/staff/work-items/{collection.pk}/assignment-targets/"
        )

        self.assertEqual(response.status_code, 200, response.content)
        usernames = {row["username"] for row in response.json()}
        self.assertSetEqual(
            usernames,
            {
                self.officer.username,
                self.second_officer.username,
                self.liaison.username,
            },
        )
        self.assertNotIn(reviewer.username, usernames)
        self.assertNotIn(admin.username, usernames)
        self.assertNotIn(intake.username, usernames)
        self.assertNotIn("email", str(response.json()).lower())
        self.assertNotIn("roles", str(response.json()).lower())

        self.client.force_login(reviewer)
        denied = self.client.get(
            f"/api/staff/work-items/{collection.pk}/assignment-targets/"
        )
        self.assertEqual(denied.status_code, 404, denied.content)

    def test_secondary_roles_cannot_read_free_form_no_action_reason(self):
        reviewer = self.make_staff("reason-reviewer", StaffRole.REVIEWER)
        review = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item
        secret = "Requester Jane Doe supplied sensitive medical context."
        set_no_action_required(
            review,
            actor=self.officer,
            category=WorkItem.NoActionCategory.INFORMATIONAL_ONLY,
            reason=secret,
            expected_version=review.version,
            idempotency_key="api-reason-redaction",
        )

        officer_response = self.client.get(f"/api/staff/work-items/{review.pk}/")
        self.assertContains(officer_response, secret)

        self.client.force_login(reviewer)
        reviewer_response = self.client.get(f"/api/staff/work-items/{review.pk}/")

        self.assertEqual(reviewer_response.status_code, 200, reviewer_response.content)
        self.assertNotIn(secret, str(reviewer_response.json()))
        self.assertEqual(reviewer_response.json()["no_action_reason"], "")
        self.assertNotIn(
            "reason",
            reviewer_response.json()["events"][-1]["metadata"],
        )

    def test_review_admin_intake_and_multi_role_queue_visibility_is_a_union(self):
        reviewer = self.make_staff("work-visibility-reviewer", StaffRole.REVIEWER)
        admin = self.make_staff("work-visibility-admin", StaffRole.ADMIN)
        intake = self.make_staff("work-visibility-intake", StaffRole.INTAKE_CLERK)
        review = materialize_work_item(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.RECORD_REVIEW,
            request=self.request,
        ).work_item
        delivery = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        ).work_item

        self.client.force_login(reviewer)
        reviewer_counts = self.client.get("/api/staff/work-items/counts/")
        self.assertEqual(reviewer_counts.status_code, 200, reviewer_counts.content)
        self.assertEqual(reviewer_counts.json()["review"], 1)
        self.assertEqual(reviewer_counts.json()["inbox_all"], 0)

        self.client.force_login(admin)
        admin_counts = self.client.get("/api/staff/work-items/counts/")
        self.assertEqual(admin_counts.json()["review"], 0)
        self.assertEqual(admin_counts.json()["inbox_delivery_exceptions"], 1)
        self.assertEqual(
            self.client.get(
                "/api/staff/work-items/",
                {"queue": "inbox_delivery_exceptions"},
            ).json()["results"][0]["id"],
            str(delivery.pk),
        )

        apply_staff_roles(admin, {StaffRole.ADMIN, StaffRole.REVIEWER})
        union_counts = self.client.get("/api/staff/work-items/counts/")
        self.assertEqual(union_counts.json()["review"], 1)
        self.assertEqual(union_counts.json()["inbox_delivery_exceptions"], 1)
        self.assertEqual(union_counts.json()["team"], 2)

        self.client.force_login(intake)
        self.assertEqual(
            self.client.get("/api/staff/work-items/counts/").status_code,
            403,
        )
        self.assertTrue(review.pk)

    def test_assignment_disposition_reopen_and_exact_retry_return_attributed_events(self):
        assigned = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/assignment/",
            {
                "assignee_id": self.second_officer.pk,
                "expected_version": 1,
                "idempotency_key": "api-assignment:item:1",
            },
            content_type="application/json",
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)
        event_id = assigned.json()["mutation_event_id"]
        retry = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/assignment/",
            {
                "assignee_id": self.second_officer.pk,
                "expected_version": 1,
                "idempotency_key": "api-assignment:item:1",
            },
            content_type="application/json",
        )
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["idempotent"])
        self.assertEqual(retry.json()["mutation_event_id"], event_id)

        handled = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.HANDLED,
                "expected_version": 2,
                "idempotency_key": "api-disposition:item:2",
            },
            content_type="application/json",
        )
        self.assertEqual(handled.status_code, 200, handled.content)
        handled_detail = self.client.get(
            f"/api/staff/work-items/{self.item.pk}/"
        )
        self.assertEqual(handled_detail.status_code, 200)
        self.assertEqual(
            handled_detail.json()["disposed_by_username"],
            self.officer.username,
        )
        counts = self.client.get("/api/staff/work-items/counts/").json()
        self.assertEqual(counts["unresolved"], 0)
        self.assertEqual(counts["handled"], 1)
        self.assertEqual(self.client.get("/api/staff/work-items/").json()["count"], 0)

        search = self.client.get(
            "/api/staff/work-items/",
            {
                "disposition": "all",
                "search": self.item.source_id,
            },
        )
        self.assertEqual(search.json()["count"], 1)
        self.assertEqual(
            self.client.get(f"/api/staff/work-items/{self.item.pk}/").status_code,
            200,
        )
        reopened = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/reopen/",
            {
                "expected_version": 3,
                "idempotency_key": "api-reopen:item:3",
            },
            content_type="application/json",
        )
        self.assertEqual(reopened.status_code, 200, reopened.content)
        self.assertEqual(
            reopened.json()["disposition"],
            WorkItem.Disposition.NEEDS_ATTENTION,
        )

    def test_version_and_idempotency_conflicts_are_explicit(self):
        first = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.WAITING,
                "expected_version": 1,
                "idempotency_key": "api-conflict:item:1",
            },
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        changed_expected = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.WAITING,
                "expected_version": 2,
                "idempotency_key": "api-conflict:item:1",
            },
            content_type="application/json",
        )
        exact_retry = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.WAITING,
                "expected_version": 1,
                "idempotency_key": "api-conflict:item:1",
            },
            content_type="application/json",
        )
        stale = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.HANDLED,
                "expected_version": 1,
                "idempotency_key": "api-conflict:item:stale",
            },
            content_type="application/json",
        )
        reused = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.HANDLED,
                "expected_version": 2,
                "idempotency_key": "api-conflict:item:1",
            },
            content_type="application/json",
        )

        self.assertEqual(changed_expected.status_code, 409)
        self.assertEqual(
            changed_expected.json()["code"],
            "idempotency_conflict",
        )
        self.assertEqual(exact_retry.status_code, 200)
        self.assertTrue(exact_retry.json()["idempotent"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "version_conflict")
        self.assertEqual(stale.json()["current_version"], 2)
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(reused.json()["code"], "idempotency_conflict")

    def test_no_action_required_is_session_officer_only_and_token_callers_handoff_before_lookup(self):
        legal_before = (
            self.request.status,
            self.request.statutory_response_due,
            StatusEvent.objects.filter(request=self.request).count(),
        )
        reason = "The message is informational and requests no district response."
        response = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/no-action-required/",
            {
                "category": WorkItem.NoActionCategory.INFORMATIONAL_ONLY,
                "reason": reason,
                "expected_version": 1,
                "idempotency_key": "api-no-action:item:1",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.item.no_action_by, self.officer)
        self.assertEqual(
            (
                self.request.status,
                self.request.statutory_response_due,
                StatusEvent.objects.filter(request=self.request).count(),
            ),
            legal_before,
        )
        event = WorkItemEvent.objects.get(
            work_item=self.item,
            event_type=WorkItemEvent.EventType.DISPOSITIONED,
        )
        self.assertEqual(event.metadata["reason"], reason)
        audit = AuditLog.objects.get(action="work_queue.item_no_action_required")
        self.assertNotIn("reason", audit.detail)
        self.assertEqual(audit.detail["reason_length"], len(event.metadata["reason"]))
        self.assertEqual(len(audit.detail["reason_sha256"]), 64)

        missing = uuid.uuid4()
        self.client.logout()
        for auth in (self.personal_token_auth(), self.shared_agent_auth()):
            with self.subTest(auth=next(iter(auth.values()))[:20]):
                handoff = self.client.post(
                    f"/api/staff/work-items/{missing}/no-action-required/",
                    {
                        "category": WorkItem.NoActionCategory.SUPERSEDED,
                        "reason": "This should hand off before object lookup.",
                        "expected_version": 1,
                        "idempotency_key": f"handoff:{uuid.uuid4()}",
                    },
                    content_type="application/json",
                    **auth,
                )
                self.assertEqual(handoff.status_code, 403, handoff.content)
                self.assertEqual(handoff.json()["code"], "human_approval_required")
                self.assertEqual(
                    handoff.json()["handoff"]["capability_id"],
                    "work_items.no_action_required",
                )

    def test_personal_token_can_use_attributed_ordinary_work_item_primitives(self):
        self.client.logout()
        auth = self.personal_token_auth()

        listing = self.client.get("/api/staff/work-items/", **auth)
        detail = self.client.get(
            f"/api/staff/work-items/{self.item.pk}/",
            **auth,
        )
        read = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/read/",
            {"idempotency_key": "pat:read:item:1"},
            content_type="application/json",
            **auth,
        )
        assigned = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/assignment/",
            {
                "assignee_id": self.second_officer.pk,
                "expected_version": 1,
                "idempotency_key": "pat:assign:item:1",
            },
            content_type="application/json",
            **auth,
        )
        handled = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/disposition/",
            {
                "disposition": WorkItem.Disposition.HANDLED,
                "expected_version": 2,
                "idempotency_key": "pat:handled:item:2",
            },
            content_type="application/json",
            **auth,
        )

        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(read.status_code, 200, read.content)
        self.assertEqual(assigned.status_code, 200, assigned.content)
        self.assertEqual(handled.status_code, 200, handled.content)
        event_ids = [
            read.json()["mutation_event_id"],
            assigned.json()["mutation_event_id"],
            handled.json()["mutation_event_id"],
        ]
        self.assertEqual(
            WorkItemEvent.objects.filter(
                pk__in=event_ids,
                actor=self.officer,
            ).count(),
            3,
        )

    def test_shared_agent_is_denied_all_ordinary_work_item_endpoints(self):
        self.client.logout()
        auth = self.shared_agent_auth()
        requests = [
            self.client.get("/api/staff/work-items/", **auth),
            self.client.get(
                f"/api/staff/work-items/{self.item.pk}/",
                **auth,
            ),
            self.client.post(
                f"/api/staff/work-items/{self.item.pk}/read/",
                {"idempotency_key": "shared:read:item:1"},
                content_type="application/json",
                **auth,
            ),
            self.client.post(
                f"/api/staff/work-items/{self.item.pk}/assignment/",
                {
                    "assignee_id": self.officer.pk,
                    "expected_version": 1,
                    "idempotency_key": "shared:assign:item:1",
                },
                content_type="application/json",
                **auth,
            ),
            self.client.post(
                f"/api/staff/work-items/{self.item.pk}/disposition/",
                {
                    "disposition": WorkItem.Disposition.HANDLED,
                    "expected_version": 1,
                    "idempotency_key": "shared:handled:item:1",
                },
                content_type="application/json",
                **auth,
            ),
            self.client.post(
                "/api/staff/work-items/bulk-metadata/",
                {
                    "items": [
                        {
                            "work_item_id": str(self.item.pk),
                            "action": "read",
                            "expected_version": 1,
                            "idempotency_key": "shared:bulk:item:1",
                        }
                    ]
                },
                content_type="application/json",
                **auth,
            ),
        ]

        self.assertTrue(requests)
        for response in requests:
            self.assertEqual(response.status_code, 403, response.content)

    def test_liaison_cannot_see_inbound_work_and_cannot_assign_it(self):
        self.client.force_login(self.liaison)

        self.assertEqual(self.client.get("/api/staff/work-items/").json()["count"], 0)
        response = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/assignment/",
            {
                "assignee_id": self.liaison.pk,
                "expected_version": 1,
                "idempotency_key": "liaison:cannot-assign",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_liaison_unassign_and_reassign_away_return_committed_responses(self):
        second_liaison = self.make_staff(
            "work-api-second-liaison",
            StaffRole.LIAISON,
        )
        unassign_item = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        reassign_item = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=uuid.uuid4(),
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        for index, item in enumerate((unassign_item, reassign_item), start=1):
            assign_work_item(
                item,
                assignee=self.liaison,
                actor=self.officer,
                expected_version=1,
                idempotency_key=f"officer:liaison-setup:{index}",
            )
        self.client.force_login(self.liaison)

        unassigned = self.client.post(
            f"/api/staff/work-items/{unassign_item.pk}/assignment/",
            {
                "assignee_id": None,
                "expected_version": 2,
                "idempotency_key": "liaison:unassign-away:2",
            },
            content_type="application/json",
        )
        reassigned = self.client.post(
            f"/api/staff/work-items/{reassign_item.pk}/assignment/",
            {
                "assignee_id": second_liaison.pk,
                "expected_version": 2,
                "idempotency_key": "liaison:reassign-away:2",
            },
            content_type="application/json",
        )

        self.assertEqual(unassigned.status_code, 200, unassigned.content)
        self.assertIsNone(unassigned.json()["assignee_id"])
        self.assertEqual(reassigned.status_code, 200, reassigned.content)
        self.assertEqual(reassigned.json()["assignee_id"], second_liaison.pk)
        unassign_item.refresh_from_db()
        reassign_item.refresh_from_db()
        self.assertIsNone(unassign_item.assignee)
        self.assertEqual(reassign_item.assignee, second_liaison)

    def test_bulk_metadata_is_capped_atomic_versioned_and_rejects_legal_fields(self):
        second_item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        ).work_item
        too_many = [
            {
                "work_item_id": str(self.item.pk),
                "action": "read",
                "expected_version": 1,
                "idempotency_key": f"bulk:too-many:{index}",
            }
            for index in range(51)
        ]
        capped = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            {"items": too_many},
            content_type="application/json",
        )
        self.assertEqual(capped.status_code, 400)

        illegal = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            {
                "items": [
                    {
                        "work_item_id": str(self.item.pk),
                        "action": "handled",
                        "expected_version": 1,
                        "idempotency_key": "bulk:illegal:1",
                        "request_status": "fulfilled",
                    }
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(illegal.status_code, 400)

        rolled_back = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            {
                "items": [
                    {
                        "work_item_id": str(self.item.pk),
                        "action": "waiting",
                        "expected_version": 1,
                        "idempotency_key": "bulk:atomic:first",
                    },
                    {
                        "work_item_id": str(second_item.pk),
                        "action": "handled",
                        "expected_version": 99,
                        "idempotency_key": "bulk:atomic:stale",
                    },
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(rolled_back.status_code, 409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.version, 1)
        self.assertEqual(
            self.item.disposition,
            WorkItem.Disposition.NEEDS_ATTENTION,
        )

    def test_bulk_read_exact_retry_survives_a_later_shared_version_change(self):
        payload = {
            "items": [
                {
                    "work_item_id": str(self.item.pk),
                    "action": "read",
                    "expected_version": 1,
                    "idempotency_key": "bulk:read:exact-retry",
                }
            ]
        }
        first = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            payload,
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        assigned = self.client.post(
            f"/api/staff/work-items/{self.item.pk}/assignment/",
            {
                "assignee_id": self.second_officer.pk,
                "expected_version": 1,
                "idempotency_key": "bulk:read:later-assignment",
            },
            content_type="application/json",
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)

        changed_expected_payload = {
            "items": [
                {
                    **payload["items"][0],
                    "expected_version": 2,
                }
            ]
        }
        changed_expected = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            changed_expected_payload,
            content_type="application/json",
        )

        retry = self.client.post(
            "/api/staff/work-items/bulk-metadata/",
            payload,
            content_type="application/json",
        )

        self.assertEqual(changed_expected.status_code, 409)
        self.assertEqual(
            changed_expected.json()["code"],
            "idempotency_conflict",
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertTrue(retry.json()["results"][0]["idempotent"])
        self.assertEqual(
            retry.json()["results"][0]["mutation_event_id"],
            first.json()["results"][0]["mutation_event_id"],
        )

    def test_openapi_contract_documents_work_item_inputs_and_outputs(self):
        response = self.client.get(
            "/api/schema/",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        schema = response.json()
        paths = schema["paths"]
        list_operation = paths["/api/staff/work-items/"]["get"]
        parameters = {
            parameter["name"]: parameter
            for parameter in list_operation["parameters"]
        }
        self.assertTrue(
            {"disposition", "search"}.issubset(parameters),
            parameters,
        )
        self.assertSetEqual(
            set(parameters["disposition"]["schema"]["enum"]),
            {"all", *WorkItem.Disposition.values},
        )

        mutation_requests = {
            "read": "Idempotency",
            "unread": "Idempotency",
            "assignment": "Assignment",
            "disposition": "Disposition",
            "reopen": "VersionedMutation",
            "no-action-required": "NoActionRequired",
        }
        for path_suffix, request_component in mutation_requests.items():
            with self.subTest(path_suffix=path_suffix):
                operation = paths[
                    f"/api/staff/work-items/{{id}}/{path_suffix}/"
                ]["post"]
                request_ref = operation["requestBody"]["content"][
                    "application/json"
                ]["schema"]["$ref"]
                response_ref = operation["responses"]["200"]["content"][
                    "application/json"
                ]["schema"]["$ref"]
                self.assertTrue(
                    request_ref.endswith(f"/{request_component}"),
                    request_ref,
                )
                self.assertTrue(
                    response_ref.endswith("/WorkItemMutationResponse"),
                    response_ref,
                )

        counts_ref = paths["/api/staff/work-items/counts/"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"]
        self.assertTrue(counts_ref.endswith("/WorkItemCounts"), counts_ref)
        bulk_operation = paths["/api/staff/work-items/bulk-metadata/"]["post"]
        bulk_request_ref = bulk_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        bulk_response_ref = bulk_operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        self.assertTrue(bulk_request_ref.endswith("/BulkMetadata"))
        self.assertTrue(bulk_response_ref.endswith("/BulkMetadataResponse"))
