# ABOUTME: Contract tests for the role-safe nested case workspace read models.
# ABOUTME: Summary, activity, and closure readiness never mutate legal state.
import datetime as dt

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.resources.requests import (
    INITIAL_RESPONSE_STEPS,
    CaseClosureBlockerSerializer,
)
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
)
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    EstimateRevision,
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CaseWorkspaceReadModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.officer = cls._user("officer", StaffRole.RECORDS_OFFICER)
        cls.liaison = cls._user("liaison", StaffRole.LIAISON)
        cls.reviewer = cls._user("reviewer", StaffRole.REVIEWER)
        cls.admin = cls._user("admin", StaffRole.ADMIN)
        cls.intake_clerk = cls._user("intake", StaffRole.INTAKE_CLERK)
        cls.requester = Requester.objects.create(
            name="Protected Requester",
            email="protected@example.org",
            phone="253-555-0100",
        )
        cls.prr = PublicRecordsRequest.objects.create(
            requester=cls.requester,
            assigned_officer=cls.officer,
            description="Immutable request for board records.",
            status=RequestStatus.GATHERING,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=2),
            estimated_completion=timezone.localdate() + dt.timedelta(days=20),
        )
        cls.task = CollectionTask.objects.create(
            request=cls.prr,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.PENDING,
            instructions="Search the department drive.",
            assigned_to=cls.liaison,
        )
        StatusEvent.objects.create(
            request=cls.prr,
            actor=cls.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.GATHERING,
            note="Requester supplied a private student name.",
        )
        EstimateRevision.objects.create(
            request=cls.prr,
            previous_date=timezone.localdate() + dt.timedelta(days=10),
            new_date=timezone.localdate() + dt.timedelta(days=20),
            reason="Delay caused by a private student-record search.",
            revised_by=cls.officer,
        )

        cls.request_work = cls._work_item(
            WorkItem.SourceType.REQUEST,
            str(cls.prr.pk),
            WorkItem.WorkKind.REQUEST_ACTION,
        )
        cls.collection_work = cls._work_item(
            WorkItem.SourceType.COLLECTION_TASK,
            str(cls.task.pk),
            WorkItem.WorkKind.COLLECTION,
            assignee=cls.liaison,
        )
        cls.review_work = cls._work_item(
            WorkItem.SourceType.RECORD_ITEM,
            "review-source",
            WorkItem.WorkKind.RECORD_REVIEW,
        )
        WorkItemReceipt.objects.create(
            work_item=cls.collection_work,
            user=cls.liaison,
            read_at=timezone.now(),
        )
        WorkItemEvent.objects.create(
            work_item=cls.review_work,
            event_type=WorkItemEvent.EventType.ASSIGNED,
            actor=cls.officer,
            resulting_version=1,
            metadata={"reason": "Contains protected student material."},
        )

        cls.outbound = Correspondence.objects.create(
            request=cls.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.QUEUED,
            approved_subject="Private requester subject",
            approved_body="Private requester message body.",
            approved_by=cls.officer,
            approved_at=timezone.now(),
            queued_by=cls.officer,
            queued_at=timezone.now(),
        )
        cls.delivery = CorrespondenceDelivery.objects.create(
            correspondence=cls.outbound,
            status=CorrespondenceDelivery.Status.PENDING,
            idempotency_key="case-workspace-delivery",
        )
        cls.installment = Installment.objects.create(
            request=cls.prr,
            sequence=1,
            fee_amount="5.00",
        )
        cls.record_item = RecordItem.objects.create(
            request=cls.prr,
            original_filename="protected.pdf",
            storage_key="records/protected.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.UNREVIEWED,
        )

        cls.agent = get_user_model().objects.create_user("workspace-mcp")
        cls.agent.set_unusable_password()
        cls.agent.save(update_fields=["password"])
        cls.agent.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="records",
                codename="read_staff_api",
            )
        )
        cls.agent_token = Token.objects.create(user=cls.agent)

    @classmethod
    def _user(cls, username, role):
        user = get_user_model().objects.create_user(
            username,
            email=f"{username}@psd401.net",
            is_staff=True,
        )
        apply_staff_roles(user, {role})
        return user

    @classmethod
    def _work_item(cls, source_type, source_id, work_kind, assignee=None):
        return WorkItem.objects.create(
            source_type=source_type,
            source_id=source_id,
            work_kind=work_kind,
            request=cls.prr,
            assignee=assignee,
        )

    def get_as(self, user, suffix):
        self.client.force_login(user)
        return self.client.get(
            f"/api/staff/requests/{self.prr.number}/{suffix}/"
        )

    def test_workspace_summary_is_bounded_role_scoped_and_server_derived(self):
        response = self.get_as(self.officer, "workspace-summary")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {
                "number",
                "legal_status",
                "requester",
                "owner",
                "statutory_response",
                "estimated_completion",
                "counts",
                "next_action",
                "stages",
                "title",
                "summary",
                "matter_kind",
                "next_due",
                "next_due_basis",
                "ai_first_pass",
            },
        )
        # The stage rail (#45) is server-derived: seven stages in order, each
        # with a state, and exactly one current or blocked while the case is open.
        self.assertEqual(
            [stage["id"] for stage in payload["stages"]],
            ["intake", "scope", "collect", "review", "redact", "release", "close"],
        )
        self.assertEqual(
            set(payload["stages"][0]),
            {"id", "label", "state", "open_count", "summary", "reason"},
        )
        self.assertEqual(
            sum(stage["state"] in {"current", "blocked"} for stage in payload["stages"]),
            1,
        )
        # The Messages tab renders this tracker, so a field silently dropped by
        # the summary serializer would remove its call to action.
        self.assertEqual(
            set(payload["statutory_response"]),
            {"state", "due_date", "next_action", "steps", "correspondence_id"},
        )
        self.assertEqual(
            [step["key"] for step in payload["statutory_response"]["steps"]],
            ["prepare", "approve", "queue", "deliver"],
        )
        self.assertEqual(
            [step["state"] for step in payload["statutory_response"]["steps"]],
            ["current", "pending", "pending", "pending"],
        )
        self.assertEqual(payload["requester"]["name"], "Protected Requester")
        self.assertEqual(payload["owner"]["username"], "officer")
        self.assertEqual(payload["counts"]["needs_attention"], 3)
        self.assertEqual(payload["counts"]["new"], 3)
        self.assertEqual(payload["next_action"]["label"], "Respond to request")
        self.assertEqual(payload["next_action"]["tab"], "overview")

        for user in (self.liaison, self.reviewer, self.admin):
            with self.subTest(role=user.username):
                scoped = self.get_as(user, "workspace-summary")
                self.assertEqual(scoped.status_code, 200, scoped.content)
                self.assertIsNone(scoped.json()["requester"])
        liaison_payload = self.get_as(self.liaison, "workspace-summary").json()
        self.assertEqual(liaison_payload["counts"]["needs_attention"], 1)
        self.assertEqual(liaison_payload["counts"]["new"], 0)
        self.assertEqual(
            liaison_payload["next_action"]["label"],
            "Complete record search",
        )
        reviewer_payload = self.get_as(self.reviewer, "workspace-summary").json()
        self.assertEqual(reviewer_payload["counts"]["needs_attention"], 1)
        self.assertEqual(reviewer_payload["next_action"]["tab"], "records")
        admin_payload = self.get_as(self.admin, "workspace-summary").json()
        self.assertEqual(admin_payload["counts"]["needs_attention"], 0)
        self.assertIsNone(admin_payload["next_action"])

    def test_summary_leads_with_a_title_and_a_short_summary(self):
        # The case page opens on what was asked, not on the whole email. The
        # model's title and summary are officer-only advisory text; everyone
        # else reads the opening of the description, which is never edited.
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
            request=self.prr,
            model_identifier="test-model",
            input_sha256="0" * 64,
            requested_by=self.officer,
            proposal={
                "short_title": "Board records",
                "summary": "Board minutes and agendas for the spring term.",
            },
        )

        officer = self.get_as(self.officer, "workspace-summary").json()
        self.assertEqual(officer["title"], "Board records")
        self.assertEqual(
            officer["summary"], "Board minutes and agendas for the spring term.",
        )
        self.assertEqual(officer["matter_kind"], "public_records")
        self.assertEqual(officer["next_due"], self.prr.statutory_response_due.isoformat())
        self.assertEqual(officer["next_due_basis"], "statutory")

        liaison = self.get_as(self.liaison, "workspace-summary").json()
        self.assertEqual(liaison["title"], "Immutable request for board records.")
        self.assertEqual(liaison["summary"], "Immutable request for board records.")
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.description, "Immutable request for board records.")

    @override_settings(
        PRR_AI_ADVISORY_ENABLED=True,
        PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
    )
    def test_summary_counts_how_many_records_the_ai_has_read(self):
        case = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Every email about the bus route.",
            status=RequestStatus.GATHERING,
        )

        def record(name, **extra):
            return RecordItem.objects.create(
                request=case,
                source_system="google_vault_gmail",
                original_filename=name,
                storage_key=f"records/{name}",
                content_length=100,
                content_type="application/pdf",
                sha256=name[0] * 64,
                **extra,
            )

        read = record("a.pdf")
        waiting = record("b.pdf")
        record("c.pdf", review_state=RecordItem.ReviewState.SUPERSEDED)
        for item, state in (
            (read, AdvisoryRun.State.COMPLETE),
            (waiting, AdvisoryRun.State.PENDING),
        ):
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                trigger=AdvisoryRun.Trigger.AUTOMATIC,
                request=case,
                record_item=item,
                model_identifier="district-approved-model-v1",
                input_sha256="0" * 64,
                state=state,
            )
        self.client.force_login(self.reviewer)

        payload = self.client.get(
            f"/api/staff/requests/{case.number}/workspace-summary/"
        ).json()

        # Superseded records are out of review, so they are not counted.
        self.assertEqual(payload["ai_first_pass"], {"enabled": True, "read": 1, "total": 2})

    @override_settings(PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=False)
    def test_summary_says_when_the_ai_first_pass_is_off(self):
        self.client.force_login(self.officer)

        payload = self.get_as(self.officer, "workspace-summary").json()

        self.assertFalse(payload["ai_first_pass"]["enabled"])

    def test_summary_without_a_model_answer_is_the_opening_of_the_request(self):
        long_text = "Every email about the bus route change. " * 20
        case = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description=long_text,
            status=RequestStatus.RECEIVED,
        )
        self.client.force_login(self.officer)

        payload = self.client.get(
            f"/api/staff/requests/{case.number}/workspace-summary/"
        ).json()

        self.assertTrue(payload["summary"].endswith("…"))
        self.assertLessEqual(len(payload["summary"]), 281)
        self.assertTrue(payload["summary"].startswith("Every email about the bus route change."))

    def test_liaison_with_assigned_draft_work_can_open_its_case_workspace(self):
        draft_only = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Draft-only liaison case.",
            status=RequestStatus.GATHERING,
        )
        draft = Correspondence.objects.create(
            request=draft_only,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Assigned draft",
            draft_body="Prepare this exact response.",
            created_by=self.liaison,
        )
        draft_work = WorkItem.objects.create(
            source_type=WorkItem.SourceType.CORRESPONDENCE,
            source_id=str(draft.pk),
            work_kind=WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
            request=draft_only,
            assignee=self.liaison,
        )
        self.client.force_login(self.liaison)

        summary = self.client.get(
            f"/api/staff/requests/{draft_only.number}/workspace-summary/"
        )
        detail = self.client.get(f"/api/staff/requests/{draft_only.number}/")
        messages = self.client.get(
            "/api/staff/correspondence/",
            {"request": draft_only.number},
        )
        selected_message = self.client.get(
            f"/api/staff/correspondence/{draft.pk}/"
        )
        personal_work = self.client.get(
            "/api/staff/work-items/",
            {"queue": "personal"},
        )

        self.assertEqual(summary.status_code, 200, summary.content)
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(summary.json()["next_action"]["tab"], "messages")
        self.assertEqual(messages.status_code, 200, messages.content)
        self.assertEqual(
            [row["id"] for row in messages.json()["results"]],
            [str(draft.pk)],
        )
        self.assertEqual(
            selected_message.status_code,
            200,
            selected_message.content,
        )
        self.assertEqual(selected_message.json()["id"], str(draft.pk))
        self.assertEqual(personal_work.status_code, 200, personal_work.content)
        work_row = next(
            row
            for row in personal_work.json()["results"]
            if row["id"] == str(draft_work.pk)
        )
        self.assertEqual(
            work_row["source_url"],
            f"/requests/{draft_only.number}/messages?message={draft.pk}",
        )

    def test_workspace_next_action_uses_the_canonical_queue_urgency_order(self):
        self.task.due_date = timezone.localdate() - dt.timedelta(days=1)
        self.task.save(update_fields=["due_date"])

        response = self.get_as(self.officer, "workspace-summary")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["next_action"]["label"], "Complete record search")
        self.assertEqual(response.json()["next_action"]["tab"], "collection")

    def test_workspace_new_count_uses_only_the_current_users_read_receipt(self):
        WorkItemReceipt.objects.create(
            work_item=self.request_work,
            user=self.officer,
        )
        WorkItemReceipt.objects.create(
            work_item=self.request_work,
            user=self.liaison,
            read_at=timezone.now(),
        )

        response = self.get_as(self.officer, "workspace-summary")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["counts"]["new"], 3)

    def test_delivery_exception_next_action_is_actionable_or_an_explicit_handoff(self):
        WorkItem.objects.update(
            disposition=WorkItem.Disposition.HANDLED,
            disposed_at=timezone.now(),
            disposed_by=self.officer,
        )
        self._work_item(
            WorkItem.SourceType.CORRESPONDENCE,
            str(self.outbound.pk),
            WorkItem.WorkKind.DELIVERY_EXCEPTION,
        )

        officer = self.get_as(self.officer, "workspace-summary").json()
        admin = self.get_as(self.admin, "workspace-summary").json()

        self.assertEqual(officer["next_action"]["tab"], "messages")
        self.assertEqual(admin["next_action"]["tab"], "overview")
        self.assertEqual(
            admin["next_action"]["label"],
            "Records Officer handoff required",
        )

    def test_activity_is_typed_and_hides_correspondence_and_notes_from_reviewer(self):
        officer = self.get_as(self.officer, "activity")
        self.assertEqual(officer.status_code, 200, officer.content)
        officer_rows = officer.json()["results"]
        self.assertTrue({"lifecycle", "work", "delivery"}.issubset(
            {row["type"] for row in officer_rows}
        ))
        self.assertTrue(all(
            set(row) == {
                "id",
                "type",
                "occurred_at",
                "label",
                "actor_username",
                "detail",
                "work_item_id",
                "delivery_status",
                "flagged",
            }
            for row in officer_rows
        ))
        # Every entry says whether it is a migration compliance note, so a
        # bounded excerpt can seat those regardless of how old they are.
        self.assertTrue(all(
            isinstance(row["flagged"], bool) for row in officer_rows
        ))
        self.assertTrue(any(
            row["actor_username"] == self.officer.username
            for row in officer_rows
        ))

        reviewer = self.get_as(self.reviewer, "activity")
        self.assertEqual(reviewer.status_code, 200, reviewer.content)
        reviewer_rows = reviewer.json()["results"]
        self.assertNotIn("delivery", {row["type"] for row in reviewer_rows})
        reviewer_text = str(reviewer_rows)
        self.assertNotIn("Private requester", reviewer_text)
        self.assertNotIn("protected student", reviewer_text.lower())
        self.assertTrue(all(
            row["actor_username"] is None
            for row in reviewer_rows
        ))
        self.assertTrue(any(row["type"] == "work" for row in reviewer_rows))

    def test_activity_retains_each_append_only_delivery_attempt_result(self):
        failed_at = timezone.now() - dt.timedelta(minutes=2)
        sent_at = timezone.now() - dt.timedelta(minutes=1)
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=self.delivery,
            attempt_number=1,
            claim_owner="delivery-worker",
            claimed_at=failed_at - dt.timedelta(seconds=1),
            result_at=failed_at,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            provider_response_metadata={"raw": "requester PII sentinel"},
            error_category="provider_unavailable",
            result_fingerprint="b" * 64,
            actor=self.officer,
        )
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=self.delivery,
            attempt_number=2,
            claim_owner="delivery-worker",
            claimed_at=sent_at - dt.timedelta(seconds=1),
            result_at=sent_at,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id="provider-case-activity",
            provider_thread_id="thread-case-activity",
            provider_timestamp=sent_at,
            result_fingerprint="c" * 64,
            actor=self.officer,
        )
        self.delivery.status = CorrespondenceDelivery.Status.SENT
        self.delivery.attempt_count = 2
        self.delivery.successful_attempt_number = 2
        self.delivery.provider_message_id = "provider-case-activity"
        self.delivery.completed_at = sent_at
        self.delivery.save(update_fields=[
            "status",
            "attempt_count",
            "successful_attempt_number",
            "provider_message_id",
            "completed_at",
        ])

        response = self.get_as(self.officer, "activity")

        self.assertEqual(response.status_code, 200, response.content)
        labels = [row["label"] for row in response.json()["results"]]
        self.assertIn("General correspondence delivery attempt failed", labels)
        self.assertIn("General correspondence delivery attempt sent", labels)
        self.assertIn("General correspondence delivery queued", labels)
        self.assertNotIn("requester PII sentinel", str(response.json()))

    def test_overview_detail_strips_internal_lifecycle_narrative_for_non_officers(self):
        self.client.force_login(self.officer)
        officer = self.client.get(f"/api/staff/requests/{self.prr.number}/")
        self.assertEqual(officer.status_code, 200, officer.content)
        self.assertIn(
            "private student name",
            officer.json()["events"][0]["note"].lower(),
        )
        self.assertIn(
            "private student-record search",
            officer.json()["estimate_revisions"][0]["reason"].lower(),
        )

        for user in (self.liaison, self.reviewer, self.admin):
            with self.subTest(role=user.username):
                self.client.force_login(user)
                response = self.client.get(
                    f"/api/staff/requests/{self.prr.number}/"
                )
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["events"][0]["note"], "")
                self.assertIsNone(response.json()["events"][0]["actor"])
                self.assertEqual(
                    response.json()["estimate_revisions"][0]["reason"],
                    "",
                )
                self.assertIsNone(
                    response.json()["estimate_revisions"][0]["revised_by"]
                )

    def test_closure_readiness_matches_releases_tab_roles(self):
        liaison = self.get_as(self.liaison, "closure-readiness")
        reviewer = self.get_as(self.reviewer, "closure-readiness")
        admin = self.get_as(self.admin, "closure-readiness")

        self.assertEqual(liaison.status_code, 403)
        self.assertEqual(reviewer.status_code, 200, reviewer.content)
        self.assertEqual(admin.status_code, 200, admin.content)

        for response in (reviewer, admin):
            delivery = next(
                blocker
                for blocker in response.json()["blockers"]
                if blocker["code"] == "pending_failed_deliveries"
            )
            self.assertEqual(delivery["tab"], "overview")
            self.assertIn("Records Officer", delivery["label"])

        reviewer_task = next(
            blocker
            for blocker in reviewer.json()["blockers"]
            if blocker["code"] == "open_tasks"
        )
        admin_task = next(
            blocker
            for blocker in admin.json()["blockers"]
            if blocker["code"] == "open_tasks"
        )
        self.assertEqual(reviewer_task["tab"], "overview")
        self.assertIn("Records Officer", reviewer_task["label"])
        self.assertEqual(admin_task["tab"], "collection")

    def test_closure_blocker_schema_declares_every_emitted_case_tab(self):
        choices = CaseClosureBlockerSerializer().fields["tab"].choices

        self.assertIn("overview", choices)

    def test_closure_readiness_reports_blockers_without_authorizing_closure(self):
        before = {
            "status": self.prr.status,
            "deadline": self.prr.statutory_response_due,
            "events": self.prr.events.count(),
            "work_disposition": self.request_work.disposition,
            "delivery_status": self.delivery.status,
        }

        response = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["counts"], {
            "open_tasks": 1,
            "unresolved_clarifications": 0,
            "unreviewed_records": 1,
            "pending_second_review": 0,
            "responsive_records_unreleased": 0,
            "fee_installment_blockers": 1,
            "open_deposit_gates": 0,
            "undelivered_release_notices": 0,
            "pending_failed_deliveries": 1,
        })
        self.assertEqual(
            {row["code"] for row in payload["blockers"]},
            {
                "open_tasks",
                "unreviewed_records",
                "fee_installment_blockers",
                "pending_failed_deliveries",
            },
        )

        self.prr.refresh_from_db()
        self.request_work.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.prr.status, before["status"])
        self.assertEqual(self.prr.statutory_response_due, before["deadline"])
        self.assertEqual(self.prr.events.count(), before["events"])
        self.assertEqual(self.request_work.disposition, before["work_disposition"])
        self.assertEqual(self.delivery.status, before["delivery_status"])
        post = self.client.post(
            f"/api/staff/requests/{self.prr.number}/closure-readiness/",
            {},
            content_type="application/json",
        )
        self.assertEqual(post.status_code, 405)

    def test_closure_readiness_names_an_unresolved_deposit(self):
        """Closing must not silently drop a deposit the District demanded."""
        from datetime import date
        from decimal import Decimal

        from apps.records.fees import activate_fee_schedule, apply_deposit_gate
        from apps.records.models import FeeDepositGate, FeeSchedule

        activate_fee_schedule(
            actor=self.officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2026, 8, 1),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )
        apply_deposit_gate(
            self.prr.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated review time.",
        )

        payload = self.get_as(self.officer, "closure-readiness").json()

        self.assertEqual(payload["counts"]["open_deposit_gates"], 1)
        blocker = next(
            row for row in payload["blockers"]
            if row["code"] == "open_deposit_gates"
        )
        self.assertEqual(blocker["tab"], "releases")

    def test_closure_readiness_blocks_responsive_records_until_released(self):
        responsive = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/responsive-unreleased.pdf",
            sha256="f" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
        )

        blocked = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(blocked.status_code, 200, blocked.content)
        self.assertEqual(
            blocked.json()["counts"]["responsive_records_unreleased"],
            1,
        )
        released = Installment.objects.create(
            request=self.prr,
            sequence=2,
            released_at=timezone.now(),
        )
        released.items.add(responsive)

        covered = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(
            covered.json()["counts"]["responsive_records_unreleased"],
            0,
        )

    def test_closure_readiness_requires_confirmed_installment_notice_delivery(self):
        now = timezone.now()
        self.installment.released_at = now
        self.installment.save(update_fields=["released_at"])

        missing = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(
            missing.json()["counts"]["undelivered_release_notices"],
            1,
        )
        notice = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.INSTALLMENT_NOTICE,
            status=Correspondence.Status.DRAFT,
            draft_subject="Records installment available",
            draft_body="Use the approved secure download link.",
            draft_metadata={"installment_id": self.installment.pk},
            created_by=self.officer,
        )

        blocked = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(blocked.status_code, 200, blocked.content)
        self.assertEqual(
            blocked.json()["counts"]["undelivered_release_notices"],
            1,
        )
        admin_blocked = self.get_as(self.admin, "closure-readiness")
        admin_notice = next(
            blocker
            for blocker in admin_blocked.json()["blockers"]
            if blocker["code"] == "undelivered_release_notices"
        )
        self.assertEqual(admin_notice["tab"], "overview")
        self.assertIn("Records Officer", admin_notice["label"])

        notice.status = Correspondence.Status.SENT
        notice.approved_subject = notice.draft_subject
        notice.approved_body = notice.draft_body
        notice.approved_metadata = notice.draft_metadata
        notice.approved_by = self.officer
        notice.approved_at = now
        notice.queued_by = self.officer
        notice.queued_at = now
        notice.sent_at = now
        notice.save()
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=notice,
            status=CorrespondenceDelivery.Status.SENT,
            idempotency_key="case-workspace-installment-notice",
            attempt_count=1,
            successful_attempt_number=1,
            provider_message_id="provider-installment-notice",
            completed_at=now,
        )
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="delivery-worker",
            claimed_at=now - dt.timedelta(seconds=1),
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id="provider-installment-notice",
            provider_thread_id="thread-installment-notice",
            provider_timestamp=now,
            result_fingerprint="d" * 64,
            actor=self.officer,
        )

        delivered = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(
            delivered.json()["counts"]["undelivered_release_notices"],
            0,
        )

        Installment.objects.create(
            request=self.prr,
            sequence=2,
            released_at=now,
        )
        unrelated = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(
            unrelated.json()["counts"]["undelivered_release_notices"],
            1,
        )

    def test_closure_readiness_blocks_a_released_installment_with_only_cancelled_notice(self):
        now = timezone.now()
        released = Installment.objects.create(
            request=self.prr,
            sequence=2,
            released_at=now,
        )
        Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.INSTALLMENT_NOTICE,
            status=Correspondence.Status.CANCELLED,
            approved_subject="Cancelled installment notice",
            approved_body="This notice was cancelled before delivery.",
            approved_metadata={"installment_id": released.pk},
            approved_by=self.officer,
            approved_at=now,
            cancelled_at=now,
        )

        response = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["counts"]["undelivered_release_notices"],
            1,
        )

    def test_closure_readiness_counts_every_distinct_second_review_source(self):
        requires_review = ExemptionCitation.objects.create(
            citation="FERPA 34 CFR 99.30",
            short_name="Student education records",
            requires_second_review=True,
        )
        ordinary = ExemptionCitation.objects.create(
            citation="RCW 42.56.210(3)",
            short_name="Exemption log",
        )
        primary = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/primary-review.pdf",
            sha256="b" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=requires_review,
        )
        primary.withholding_exemptions.add(requires_review)
        supplemental = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/supplemental-review.pdf",
            sha256="c" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=ordinary,
        )
        supplemental.withholding_exemptions.add(requires_review)
        redacted = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/redacted-review.pdf",
            sha256="d" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
        )
        Redaction.objects.create(
            record_item=redacted,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=requires_review,
            created_by=self.officer,
        )
        voided = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/voided-review.pdf",
            sha256="e" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
        )
        Redaction.objects.create(
            record_item=voided,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=requires_review,
            created_by=self.officer,
            voided_by=self.reviewer,
            voided_at=timezone.now(),
            void_reason="Duplicate region retained as voided evidence.",
        )

        response = self.get_as(self.officer, "closure-readiness")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["counts"]["pending_second_review"], 3)

    def test_case_read_models_enforce_intake_and_shared_agent_boundaries(self):
        for suffix in ("workspace-summary", "activity", "closure-readiness"):
            with self.subTest(endpoint=suffix):
                denied = self.get_as(self.intake_clerk, suffix)
                self.assertEqual(denied.status_code, 403)

                self.client.logout()
                agent = self.client.get(
                    f"/api/staff/requests/{self.prr.number}/{suffix}/",
                    HTTP_AUTHORIZATION=f"Token {self.agent_token.key}",
                )
                self.assertEqual(agent.status_code, 200, agent.content)
                agent_text = str(agent.json())
                self.assertNotIn("Protected Requester", agent_text)
                self.assertNotIn("protected@example.org", agent_text)
                self.assertNotIn("private requester", agent_text.lower())
                # The username guard below is a bare substring scan, and the
                # fixed statutory step labels legitimately name the Records
                # Officer *role*. Remove those constants first so the scan
                # still means "no staff username reached a shared agent".
                scanned = agent_text.lower()
                for _key, label in INITIAL_RESPONSE_STEPS:
                    scanned = scanned.replace(label.lower(), "")
                self.assertNotIn("officer", scanned)
                if suffix == "workspace-summary":
                    self.assertIsNone(agent.json()["owner"])
                if suffix == "activity":
                    self.assertTrue(
                        all(
                            row["actor_username"] is None
                            for row in agent.json()["results"]
                        )
                    )

    def test_installments_can_be_scoped_to_one_case_without_a_second_api(self):
        other = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="A different request.",
        )
        Installment.objects.create(request=other, sequence=1)
        self.client.force_login(self.officer)

        response = self.client.get(
            f"/api/staff/installments/?request={self.prr.number}"
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        self.assertEqual([row["request_number"] for row in rows], [self.prr.number])

    def test_case_owner_choices_are_limited_to_active_records_officers(self):
        self.client.force_login(self.officer)

        response = self.client.get(
            "/api/staff/staff-users/?role=records_officer"
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["results"]
        self.assertEqual([row["username"] for row in rows], ["officer"])
