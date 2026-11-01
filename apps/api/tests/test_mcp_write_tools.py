# ABOUTME: End-to-end guardrails for hosted MCP draft/proposal write tools.
# ABOUTME: Personal attribution is required and every legal human gate remains.
import hashlib
import json
from datetime import timedelta

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from apps.api.models import PersonalAccessToken
from apps.audit.models import AuditLog
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceDelivery,
)
from apps.records.models import (
    CollectionTask,
    EstimateRevision,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.records.service_identities import provision_integration_identity
from config.asgi import create_application
from integrations.mcp.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES


def issue_personal_token(user, *, suffix):
    prefix = hashlib.sha256(suffix.encode()).hexdigest()[:12]
    presented = f"prr_pat_{prefix}_{suffix}"
    PersonalAccessToken.objects.create(
        user=user,
        name=f"MCP write {suffix}",
        prefix=prefix,
        token_hash=hashlib.sha256(presented.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(days=30),
    )
    return presented


def rendered_result(result):
    return "\n".join(
        item.text
        if getattr(item, "type", None) == "text"
        else json.dumps(item.model_dump(mode="json"), sort_keys=True)
        for item in result.content
    )


class MCPWriteToolAcceptanceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "mcp-write-officer",
            "mcp-write-officer@psd401.net",
            is_staff=True,
        )
        self.personal_token = issue_personal_token(
            self.officer,
            suffix="personal-write-token",
        )
        self.requester = Requester.objects.create(
            name="MCP Write Requester",
            email="mcp-write-requester@example.org",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Records requiring clarification and review.",
            status=RequestStatus.ACKNOWLEDGED,
        )
        self.task = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Search the original date range.",
            assigned_to=self.officer,
        )
        self.record_item = RecordItem.objects.create(
            request=self.prr,
            collection_task=self.task,
            source_system="google-workspace",
            original_filename="student-record.pdf",
            storage_key="records/test/student-record.pdf",
            sha256="a" * 64,
        )
        self.student_citation = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA",
            description="Student education records.",
            active=True,
        )
        self.inactive_citation = ExemptionCitation.objects.create(
            citation="RCW test inactive",
            short_name="Inactive",
            description="No longer active.",
            active=False,
        )
        provisioned = provision_integration_identity(
            "mcp_readonly",
            "mcp-write-shared-readonly",
        )
        self.shared_readonly_token = provisioned.token.key

    async def call_tools(self, token, calls, *, authorization_scheme="Token"):
        application = create_application()
        transport = httpx.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={
                    "Authorization": f"{authorization_scheme} {token}",
                },
            ) as http_client:
                async with streamable_http_client(
                    "https://localhost/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        results = []
                        for name, arguments in calls:
                            results.append(
                                await session.call_tool(name, arguments)
                            )
                        return {tool.name for tool in listed.tools}, results

    def test_personal_token_creates_acknowledgment_draft_without_sending_it(self):
        """Issue #12's acceptance criteria, as one test.

        A records-officer token drafts the statutory acknowledgment over MCP;
        the row lands as an unsent draft attributed to the token's user, with
        an audit row naming that user, and nothing about the request moves --
        the five-business-day clock is answered by a human-approved delivery,
        not by drafting.
        """
        received = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Board meeting minutes for the last school year.",
            status=RequestStatus.RECEIVED,
        )
        original_event_count = StatusEvent.objects.filter(
            request=received,
        ).count()

        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_acknowledgment_draft",
                    {"request_number": received.number},
                )
            ],
        )

        self.assertFalse(results[0].isError, rendered_result(results[0]))
        draft = Correspondence.objects.get(
            request=received,
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
        )
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(draft.created_by, self.officer)
        # Rule 6 allows exactly two automated requester-facing paths, and a
        # tool-created draft is neither of them.
        self.assertFalse(draft.automated)
        self.assertIsNone(draft.approved_at)
        self.assertIsNone(draft.queued_at)
        self.assertIsNone(draft.sent_at)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(
                correspondence=draft,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.draft_created",
                actor=self.officer,
            ).exists(),
            "the draft must be attributable to the token's user",
        )

        received.refresh_from_db()
        self.assertEqual(received.status, RequestStatus.RECEIVED)
        self.assertIsNone(received.acknowledgment_sent_at)
        self.assertFalse(received.initial_response_delivered)
        self.assertEqual(
            StatusEvent.objects.filter(request=received).count(),
            original_event_count,
        )

    def test_acknowledgment_response_withholds_the_requester_s_own_wording(self):
        """The letter quotes the request description; the tool result must not.

        `templates/correspondence/acknowledgment/v2.txt` renders
        `{{ request.description }}` verbatim, and the description is the
        unbounded legal record of what was asked (rule 9) -- it can name a
        student. A tool result leaves for the caller's model provider, and the
        data owner's sign-off covers the subject and requester contact, not a
        whole letter body. So the draft has to be created with the description
        in it and reported without it.
        """
        description = (
            "Every email mentioning the suspension of student Jordan Rivera."
        )
        received = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description=description,
            status=RequestStatus.RECEIVED,
        )

        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_acknowledgment_draft",
                    {"request_number": received.number},
                )
            ],
        )

        self.assertFalse(results[0].isError, rendered_result(results[0]))
        payload = rendered_result(results[0])
        # The draft itself must still carry the statutory wording.
        draft = Correspondence.objects.get(
            request=received,
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
        )
        self.assertIn(description, draft.draft_body)
        # The response must not, by any key or nesting.
        self.assertNotIn("Jordan Rivera", payload)
        self.assertNotIn(description, payload)
        self.assertNotIn("draft_body", payload)
        self.assertNotIn("draft_subject", payload)
        # And it must say where the letter is instead of going silent.
        self.assertIn("letter_text", payload)
        self.assertIn("Messages tab", payload)

    def test_acknowledgment_response_still_proves_the_draft_is_unsent(self):
        # Withholding the letter must not cost the caller the compliance
        # facts: an agent has to be able to report that nothing was sent.
        received = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Transportation routing records.",
            status=RequestStatus.RECEIVED,
        )

        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_acknowledgment_draft",
                    {"request_number": received.number},
                )
            ],
        )

        payload = json.loads(rendered_result(results[0]))
        self.assertEqual(payload["status"], Correspondence.Status.DRAFT)
        self.assertEqual(payload["request_number"], received.number)
        self.assertFalse(payload["automated"])
        for key in ("approved_at", "queued_at", "sent_at"):
            with self.subTest(key=key):
                self.assertIsNone(payload[key])
        self.assertEqual(
            payload["kind"],
            Correspondence.Kind.ACKNOWLEDGMENT,
        )
        # The estimate the letter states is a statutory date, not requester
        # text, so it stays -- an agent reports the date without the letter.
        self.assertIn("estimated_completion", payload["draft_metadata"])

    def test_acknowledgment_draft_cannot_revise_a_published_estimate(self):
        """A drafting tool may not move an estimate as a side effect.

        The estimate a requester has already been given changes only through
        the human estimate-revision workflow, so a disagreeing
        `estimated_completion` has to fail rather than quietly re-publish.
        """
        published_estimate = timezone.localdate() + timedelta(days=20)
        self.prr.estimated_completion = published_estimate
        self.prr.save(update_fields=["estimated_completion"])

        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_acknowledgment_draft",
                    {
                        "request_number": self.prr.number,
                        "estimated_completion": (
                            published_estimate + timedelta(days=30)
                        ).isoformat(),
                    },
                )
            ],
        )

        self.assertTrue(results[0].isError, rendered_result(results[0]))
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.estimated_completion, published_estimate)
        self.assertFalse(
            Correspondence.objects.filter(
                request=self.prr,
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
            ).exists()
        )
        self.assertFalse(
            EstimateRevision.objects.filter(request=self.prr).exists()
        )

    def test_acknowledgment_draft_rejects_a_malformed_request_number(self):
        # The number reaches a REST body, and the error has to teach what a
        # request number is instead of returning a stack trace.
        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_acknowledgment_draft",
                    {"request_number": "../record-items"},
                )
            ],
        )

        self.assertTrue(results[0].isError)
        self.assertIn(
            "request number",
            rendered_result(results[0]).lower(),
        )
        self.assertFalse(
            Correspondence.objects.filter(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
            ).exists()
        )

    def test_personal_token_creates_clarification_draft_without_legal_effect(self):
        original_status = self.prr.status
        original_estimated_completion = self.prr.estimated_completion
        original_event_count = StatusEvent.objects.filter(
            request=self.prr,
        ).count()
        tomorrow = timezone.localdate() + timedelta(days=1)

        names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_clarification_letter_draft",
                    {
                        "request_number": self.prr.number,
                        "clarification_questions": (
                            "Which school years should the search cover?"
                        ),
                        "clarification_due": tomorrow.isoformat(),
                    },
                )
            ],
        )

        self.assertSetEqual(
            names,
            set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES),
        )
        self.assertFalse(results[0].isError, rendered_result(results[0]))
        draft = Correspondence.objects.get(
            request=self.prr,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
        )
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(draft.created_by, self.officer)
        self.assertIsNone(draft.approved_at)
        self.assertIsNone(draft.queued_at)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(
                correspondence=draft,
            ).exists()
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, original_status)
        self.assertEqual(
            self.prr.estimated_completion,
            original_estimated_completion,
        )
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr).count(),
            original_event_count,
        )
        self.assertFalse(
            EstimateRevision.objects.filter(request=self.prr).exists()
        )
        self.assertFalse(
            ClarificationCycle.objects.filter(request=self.prr).exists()
        )

        approval = self.client.post(
            f"/api/staff/correspondence/{draft.pk}/approve/",
            {"expected_version": draft.version},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.personal_token}",
        )
        self.assertEqual(approval.status_code, 403)
        self.assertEqual(
            approval.json()["code"],
            "human_approval_required",
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_lowercase_token_scheme_keeps_personal_write_attribution(self):
        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_collection_task_notes_propose",
                    {
                        "task_id": self.task.pk,
                        "proposed_instructions": "Search the archived mailbox.",
                    },
                )
            ],
            authorization_scheme="token",
        )

        self.assertFalse(results[0].isError, rendered_result(results[0]))
        self.assertIn(
            '"applied": false',
            rendered_result(results[0]).lower(),
        )
        self.task.refresh_from_db()
        self.assertEqual(
            self.task.instructions,
            "Search the original date range.",
        )

    def test_personal_token_proposals_do_not_patch_or_attach_legal_evidence(self):
        names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_collection_task_notes_propose",
                    {
                        "task_id": self.task.pk,
                        "proposed_instructions": (
                            "Also search the superintendent's archived mail."
                        ),
                    },
                ),
                (
                    "psd_prr_exemption_citations_suggest",
                    {
                        "record_item_id": str(self.record_item.pk),
                        "candidate_citation_ids": [
                            self.student_citation.pk,
                        ],
                        "rationale": (
                            "The reviewed content appears to identify a "
                            "student education record."
                        ),
                    },
                ),
            ],
        )

        self.assertSetEqual(
            names,
            set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES),
        )
        self.assertTrue(all(not result.isError for result in results))
        task_proposal = rendered_result(results[0])
        citation_proposal = rendered_result(results[1])
        self.assertIn("superintendent", task_proposal)
        self.assertIn('"applied": false', task_proposal.lower())
        self.assertIn(self.student_citation.citation, citation_proposal)
        self.assertIn('"applied": false', citation_proposal.lower())

        self.task.refresh_from_db()
        self.record_item.refresh_from_db()
        self.assertEqual(
            self.task.instructions,
            "Search the original date range.",
        )
        self.assertEqual(
            self.record_item.review_state,
            RecordItem.ReviewState.UNREVIEWED,
        )
        self.assertEqual(self.record_item.withholding_exemptions.count(), 0)
        self.assertEqual(self.record_item.redactions.count(), 0)
        self.assertFalse(
            Correspondence.objects.filter(request=self.prr).exists()
        )

    def test_inactive_citation_is_rejected_without_attaching_it(self):
        _names, results = async_to_sync(self.call_tools)(
            self.personal_token,
            [
                (
                    "psd_prr_exemption_citations_suggest",
                    {
                        "record_item_id": str(self.record_item.pk),
                        "candidate_citation_ids": [
                            self.inactive_citation.pk,
                        ],
                        "rationale": "Candidate supplied for validation.",
                    },
                )
            ],
        )

        self.assertTrue(results[0].isError)
        self.assertIn("active", rendered_result(results[0]).lower())
        self.assertEqual(self.record_item.withholding_exemptions.count(), 0)

    def test_shared_readonly_identity_is_rejected_by_every_write_tool(self):
        calls = [
            (
                "psd_prr_acknowledgment_draft",
                {"request_number": self.prr.number},
            ),
            (
                "psd_prr_clarification_letter_draft",
                {
                    "request_number": self.prr.number,
                    "clarification_questions": "Which date range?",
                    "clarification_due": (
                        timezone.localdate() + timedelta(days=1)
                    ).isoformat(),
                },
            ),
            (
                "psd_prr_collection_task_notes_propose",
                {
                    "task_id": self.task.pk,
                    "proposed_instructions": "Search an additional archive.",
                },
            ),
            (
                "psd_prr_exemption_citations_suggest",
                {
                    "record_item_id": str(self.record_item.pk),
                    "candidate_citation_ids": [self.student_citation.pk],
                    "rationale": "Candidate supplied for human review.",
                },
            ),
        ]

        names, results = async_to_sync(self.call_tools)(
            self.shared_readonly_token,
            calls,
        )

        self.assertSetEqual(
            names,
            set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES),
        )
        self.assertTrue(all(result.isError for result in results))
        for result in results:
            self.assertIn("personal", rendered_result(result).lower())
        self.assertFalse(
            Correspondence.objects.filter(request=self.prr).exists()
        )
        self.task.refresh_from_db()
        self.assertEqual(
            self.task.instructions,
            "Search the original date range.",
        )
        self.assertEqual(self.record_item.withholding_exemptions.count(), 0)

    def test_hosted_write_surface_has_no_approval_or_legal_effect_tools(self):
        names, _results = async_to_sync(self.call_tools)(
            self.personal_token,
            [],
        )

        self.assertSetEqual(
            set(WRITE_TOOL_NAMES),
            {
                "psd_prr_acknowledgment_draft",
                "psd_prr_clarification_letter_draft",
                "psd_prr_collection_task_notes_propose",
                "psd_prr_exemption_citations_suggest",
            },
        )
        forbidden_fragments = {
            "approve",
            "queue",
            "send",
            "deny",
            "transition",
            "resolve",
            "estimate",
            "exclude_camera",
            "attach",
        }
        self.assertFalse(
            {
                name
                for name in names
                if any(fragment in name for fragment in forbidden_fragments)
            }
        )
