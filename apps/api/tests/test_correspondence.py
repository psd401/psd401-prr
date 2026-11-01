import datetime as dt
import hashlib
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
)
from apps.records.models import (
    AdequateSearchAttestation,
    CollectionTask,
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
)
from apps.records.production import (
    current_installment_manifest_sha256,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CorrespondenceApiTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "liaison",
            "liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.reviewer = User.objects.create_user(
            "reviewer",
            "reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.admin = User.objects.create_user(
            "admin",
            "admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        requester = Requester.objects.create(
            name="Jane Requester",
            email="jane@example.org",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All transportation-plan records.",
            estimated_completion=dt.date(2026, 8, 3),
        )
        self.other_prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Other request.",
        )
        CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Assigned request.",
            assigned_to=self.liaison,
        )
        self.denial_exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )

    def login(self, user=None):
        self.client.force_login(user or self.officer)

    def token_headers(self, user):
        token = Token.objects.create(user=user)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    def general_draft(self, *, actor=None, prr=None):
        return services.create_authored_draft(
            prr or self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Case update",
            body="The search is continuing.",
            actor=actor or self.liaison,
        )

    def approve_and_queue(self, correspondence):
        correspondence = services.approve_draft(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
        )
        delivery = services.queue_approved(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
        )
        correspondence.refresh_from_db()
        return correspondence, delivery

    def released_installment(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key="productions/request/1/package.zip",
            package_sha256="b" * 64,
            package_size=123,
            package_manifest_sha256="c" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )
        item = RecordItem.objects.create(
            request=self.prr,
            storage_key=f"records/{self.prr.pk}/installment.pdf",
            sha256="a" * 64,
            content_length=1,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        installment.items.add(item)
        installment.package_manifest_sha256 = current_installment_manifest_sha256(
            installment
        )
        installment.save(update_fields=["package_manifest_sha256"])
        return installment


class CorrespondenceReadApiTests(CorrespondenceApiTestCase):
    def test_list_requires_staff_and_is_pii_minimized(self):
        self.general_draft()
        anonymous = self.client.get("/api/staff/correspondence/")
        self.assertEqual(anonymous.status_code, 403)

        self.login()
        response = self.client.get("/api/staff/correspondence/")

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["request_number"], self.prr.number)
        self.assertEqual(row["recipient_count"], 1)
        serialized = str(row)
        self.assertNotIn("jane@example.org", serialized)
        self.assertNotIn("Jane Requester", serialized)
        # A Records Officer is authorized for every case's full
        # correspondence, so a cross-case row carries the subject line and a
        # bounded snippet for Inbox triage. Requester identity and raw content
        # fields stay out regardless of role.
        self.assertEqual(row["subject"], "Case update")
        self.assertIn("search is continuing", row["snippet"])
        for sensitive_field in (
            "draft_subject",
            "draft_body",
            "approved_subject",
            "approved_body",
            "received_subject",
            "received_body",
            "failure_description",
            "recipients",
        ):
            self.assertNotIn(sensitive_field, row)

    def test_cross_case_content_stays_blank_for_non_officers(self):
        self.general_draft()
        self.login(self.liaison)

        response = self.client.get("/api/staff/correspondence/")

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["request_number"], self.prr.number)
        self.assertEqual(row["subject"], "")
        self.assertEqual(row["snippet"], "")
        serialized = str(row)
        self.assertNotIn("Case update", serialized)
        self.assertNotIn("search is continuing", serialized)

    def test_detail_returns_exact_staff_record_but_not_secrets_or_storage_keys(self):
        draft = self.general_draft()
        self.login()

        response = self.client.get(f"/api/staff/correspondence/{draft.id}/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["draft_subject"], "Case update")
        self.assertEqual(body["draft_body"], "The search is continuing.\n")
        self.assertEqual(body["recipients"][0]["email"], "jane@example.org")
        self.assertIsNone(body["resulting_status_event_id"])
        self.assertIsNone(body["resulting_status"])
        self.assertNotIn("reply_token", body)
        self.assertNotIn("storage_key", str(body))
        self.assertNotIn("idempotency_key", str(body))

    def test_request_scoped_summary_defers_exact_routing_to_detail(self):
        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Records request follow-up",
            received_body="I am helping the requester clarify the date range.",
            received_sender=(
                "Community Advocate <advocate@community.example.org>"
            ),
            received_recipients=[
                "Public Records <publicrecords@psd401.net>",
                "Records Officer <officer@psd401.net>",
            ],
            received_at=timezone.now(),
            matched_at=timezone.now(),
            matched_by=self.officer,
        )
        unmatched = Correspondence.objects.create(
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject="Unassociated message",
            received_body="This message has not been matched to a request.",
            received_sender="Unknown Case <unmatched@example.org>",
            received_recipients=["publicrecords@psd401.net"],
            received_at=timezone.now(),
        )
        self.login(self.liaison)

        listed = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        )
        detail = self.client.get(f"/api/staff/correspondence/{inbound.id}/")

        self.assertEqual(listed.status_code, 200)
        list_row = next(
            row
            for row in listed.json()["results"]
            if row["id"] == str(inbound.id)
        )
        self.assertEqual(list_row["subject"], "Records request follow-up")
        self.assertNotIn("received_sender", list_row)
        self.assertNotIn("received_recipients", list_row)
        self.assertNotIn(
            str(unmatched.id),
            {row["id"] for row in listed.json()["results"]},
        )

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["received_sender"],
            "Community Advocate <advocate@community.example.org>",
        )
        self.assertEqual(
            detail.json()["received_recipients"],
            [
                "Public Records <publicrecords@psd401.net>",
                "Records Officer <officer@psd401.net>",
            ],
        )

        self.login(self.officer)
        global_list = self.client.get("/api/staff/correspondence/")
        unmatched_detail = self.client.get(
            f"/api/staff/correspondence/{unmatched.id}/"
        )
        self.assertEqual(unmatched_detail.status_code, 404)
        self.assertNotIn("unmatched@example.org", str(global_list.json()))

    def test_filters_and_delivery_attempts_exclude_raw_provider_metadata(self):
        draft = self.general_draft()
        correspondence, delivery = self.approve_and_queue(draft)
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
            result_at=timezone.now(),
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            provider_response_metadata={"raw": "requester PII"},
            error_category="provider_unavailable",
            result_fingerprint="f" * 64,
        )
        self.login()

        listed = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
            "&direction=outbound&status=queued&kind=general"
        )
        attempts = self.client.get(
            f"/api/staff/correspondence/{correspondence.id}/attempts/"
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(attempts.status_code, 200)
        row = attempts.json()[0]
        self.assertEqual(row["attempt_number"], 1)
        self.assertEqual(row["error_category"], "provider_unavailable")
        self.assertNotIn("provider_response_metadata", row)
        self.assertNotIn("requester PII", str(row))

    def test_liaison_sees_only_assigned_request_correspondence(self):
        own = self.general_draft()
        other = self.general_draft(prr=self.other_prr)
        self.login(self.liaison)

        rows = self.client.get("/api/staff/correspondence/").json()["results"]
        hidden_detail = self.client.get(f"/api/staff/correspondence/{other.id}/")

        self.assertEqual([row["id"] for row in rows], [str(own.id)])
        self.assertEqual(hidden_detail.status_code, 404)

    def test_nonstaff_readonly_identity_cannot_read_correspondence_pii(self):
        draft = self.general_draft()
        agent = get_user_model().objects.create_user("mcp", is_staff=False)
        permission = Permission.objects.get(codename="read_staff_api")
        agent.user_permissions.add(permission)

        response = self.client.get(
            f"/api/staff/correspondence/{draft.id}/",
            **self.token_headers(agent),
        )

        self.assertEqual(response.status_code, 403)


class DraftActionApiTests(CorrespondenceApiTestCase):
    def test_no_records_closure_is_session_only_and_retains_search_evidence(self):
        task = self.prr.collection_tasks.get()
        task.state = CollectionTask.State.COMPLETE
        task.completed_at = timezone.now()
        task.save(update_fields=["state", "completed_at"])
        payload = {
            "request_number": self.prr.number,
            "determination": "no_responsive_records",
            "search_completed_at": timezone.now().isoformat(),
            "search_steps": [
                {
                    "location": "Google Workspace",
                    "custodian": "Transportation Director",
                    "method": "Vault search for transportation-plan terms",
                    "date_range": "2025-01-01 through 2025-12-31",
                    "result": "No responsive records located",
                }
            ],
            "adequacy_reason": (
                "Every location reasonably likely to contain responsive "
                "records was searched and obvious leads were followed."
            ),
            "retention_schedule_reference": "",
            "incident_hold_checked": False,
            "prior_exports_checked": False,
            "email_drive_copies_checked": False,
            "other_copies_checked": False,
            "counsel_approval_reference": "Counsel memo PRA-2026-18",
        }

        token_response = self.client.post(
            "/api/staff/correspondence/drafts/no-records-closure/",
            payload,
            content_type="application/json",
            **self.token_headers(self.officer),
        )
        self.assertEqual(token_response.status_code, 403)
        self.assertEqual(
            token_response.json()["code"],
            "human_approval_required",
        )
        self.assertEqual(AdequateSearchAttestation.objects.count(), 0)

        self.login(self.officer)
        created = self.client.post(
            "/api/staff/correspondence/drafts/no-records-closure/",
            payload,
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["kind"], "no_records_closure")
        attestation = AdequateSearchAttestation.objects.get()
        self.assertEqual(attestation.request, self.prr)
        self.assertEqual(attestation.attested_by, self.officer)
        self.assertEqual(attestation.search_steps, payload["search_steps"])

    def test_phase_four_closure_and_notice_drafts_are_session_only(self):
        """Each new kind is an accountable human action, not an agent call."""
        endpoints = {
            "completion-closure": {
                "request_number": self.prr.number,
                "production_summary": (
                    "The District produced 214 pages across three "
                    "installments and withheld nothing."
                ),
            },
            "nonpayment-closure": {
                "request_number": self.prr.number,
                "amount_due": "125.00",
                "payment_requested_on": timezone.localdate().isoformat(),
            },
            "third-party-notice": {
                "request_number": self.prr.number,
                "employee_name": "Alex Doe",
                "employee_email": "alex.doe@psd401.net",
            },
        }
        expected_kinds = {
            "completion-closure": "completion_closure",
            "nonpayment-closure": "nonpayment_closure",
            "third-party-notice": "third_party_notice",
        }

        agent_headers = self.token_headers(self.officer)

        for slug, payload in endpoints.items():
            with self.subTest(slug=slug):
                url = f"/api/staff/correspondence/drafts/{slug}/"
                self.client.logout()
                refused = self.client.post(
                    url,
                    payload,
                    content_type="application/json",
                    **agent_headers,
                )
                self.assertEqual(refused.status_code, 403)
                self.assertEqual(
                    refused.json()["code"],
                    "human_approval_required",
                )

                self.login(self.liaison)
                wrong_role = self.client.post(
                    url,
                    payload,
                    content_type="application/json",
                )
                self.assertEqual(wrong_role.status_code, 403)

                self.login(self.officer)
                created = self.client.post(
                    url,
                    payload,
                    content_type="application/json",
                )
                self.assertEqual(created.status_code, 201, created.content)
                self.assertEqual(created.json()["kind"], expected_kinds[slug])

    def test_third_party_notice_addresses_the_employee_not_the_requester(self):
        self.login(self.officer)

        created = self.client.post(
            "/api/staff/correspondence/drafts/third-party-notice/",
            {
                "request_number": self.prr.number,
                "employee_name": "Alex Doe",
                "employee_email": "alex.doe@psd401.net",
                "attorney_name": "Pat Counsel",
                "attorney_email": "pat@counsel.example",
            },
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201, created.content)
        addresses = {
            recipient["recipient_type"]: recipient["email"]
            for recipient in created.json()["recipients"]
        }
        self.assertEqual(addresses["to"], "alex.doe@psd401.net")
        self.assertEqual(addresses["cc"], "pat@counsel.example")
        self.assertNotIn(
            self.prr.requester.email,
            set(addresses.values()),
        )

    def test_batch_drafts_then_sends_and_refuses_a_denial(self):
        """The two-step batch: drafts an officer can read, then one send."""
        self.login(self.officer)

        drafted = self.client.post(
            "/api/staff/correspondence/batch/closure-drafts/",
            {
                "request_numbers": [self.prr.number, self.other_prr.number],
                "kind": "completion_closure",
                "confirmed": True,
                "production_summary": (
                    "The District produced every responsive record it "
                    "located and withheld nothing."
                ),
            },
            content_type="application/json",
        )

        self.assertEqual(drafted.status_code, 200, drafted.content)
        payload = drafted.json()
        self.assertEqual(len(payload["succeeded"]), 2)
        self.assertEqual(payload["failed"], [])
        self.assertEqual(
            {row["request_number"] for row in payload["succeeded"]},
            {self.prr.number, self.other_prr.number},
        )
        # Each case's own rendered text comes back, so the officer can read
        # every notice before the one send covers all of them.
        for row in payload["succeeded"]:
            draft = Correspondence.objects.get(pk=row["correspondence_id"])
            self.assertEqual(row["subject"], draft.draft_subject)
            self.assertEqual(row["body"], draft.draft_body)
            self.assertIn(row["request_number"], row["body"])
        self.assertFalse(CorrespondenceDelivery.objects.exists())

        denial = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial",
            body="The responsive records are exempt in full.",
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        refused = self.client.post(
            "/api/staff/correspondence/batch/approve-and-send/",
            {"correspondence_ids": [str(denial.id)], "confirmed": True},
            content_type="application/json",
        )
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

        sent = self.client.post(
            "/api/staff/correspondence/batch/approve-and-send/",
            {
                "correspondence_ids": [
                    row["correspondence_id"] for row in payload["succeeded"]
                ],
                "confirmed": True,
            },
            content_type="application/json",
        )

        self.assertEqual(sent.status_code, 200, sent.content)
        self.assertEqual(len(sent.json()["succeeded"]), 2)
        self.assertEqual(CorrespondenceDelivery.objects.count(), 2)

    def test_batch_endpoints_refuse_an_agent_token(self):
        agent_headers = self.token_headers(self.officer)

        for url, body in (
            (
                "/api/staff/correspondence/batch/closure-drafts/",
                {
                    "request_numbers": [self.prr.number],
                    "kind": "general",
                    "confirmed": True,
                    "subject": "Subject",
                    "body": "Body",
                },
            ),
            (
                "/api/staff/correspondence/batch/approve-and-send/",
                {
                    "correspondence_ids": [
                        "00000000-0000-0000-0000-000000000000"
                    ],
                    "confirmed": True,
                },
            ),
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    url,
                    body,
                    content_type="application/json",
                    **agent_headers,
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["code"],
                    "human_approval_required",
                )

    def test_scope_conversation_is_drafted_only_by_a_signed_in_officer(self):
        """The count letter is a judgment call, not a report an agent files.

        Telling a requester their request matched N items, and choosing
        whether that message stops the District's clock, is exactly the kind
        of decision rule 6 keeps with an accountable human.
        """
        payload = {
            "request_number": self.prr.number,
            "form": "clarification",
            "match_count": 29380,
            "query_summary": (
                "district Gmail for the transportation terms, through the "
                "date the request arrived"
            ),
            "narrowing_options": [
                {
                    "terms": ["transportation plan", "2025"],
                    "rationale": "Only the 2025 plan and its revisions.",
                }
            ],
            "response_by": "2026-08-24",
        }

        token_response = self.client.post(
            "/api/staff/correspondence/drafts/scope-clarification/",
            payload,
            content_type="application/json",
            **self.token_headers(self.officer),
        )
        self.assertEqual(token_response.status_code, 403)
        self.assertEqual(
            token_response.json()["code"],
            "human_approval_required",
        )
        self.assertFalse(
            Correspondence.objects.filter(
                kind=Correspondence.Kind.SCOPE_CLARIFICATION
            ).exists()
        )

        self.login(self.liaison)
        refused = self.client.post(
            "/api/staff/correspondence/drafts/scope-clarification/",
            payload,
            content_type="application/json",
        )
        self.assertEqual(refused.status_code, 403)

        self.login(self.officer)
        created = self.client.post(
            "/api/staff/correspondence/drafts/scope-clarification/",
            payload,
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(body["kind"], "scope_clarification")
        self.assertIn("29,380 items", body["draft_body"])
        self.assertIn("RCW 42.56.520(3)", body["draft_body"])
        self.assertEqual(body["draft_metadata"]["form"], "clarification")

    def test_scope_conversation_refuses_a_fifth_narrowing_option(self):
        self.login(self.officer)

        response = self.client.post(
            "/api/staff/correspondence/drafts/scope-clarification/",
            {
                "request_number": self.prr.number,
                "form": "informational",
                "match_count": 12,
                "query_summary": "district Gmail for the transport terms",
                "narrowing_options": [
                    {"terms": [f"term {index}"], "rationale": "A rationale."}
                    for index in range(5)
                ],
                "response_by": "2026-08-24",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("narrowing_options", response.json())

    def test_denial_draft_requires_and_returns_structured_exemption_basis(self):
        self.login()
        payload = {
            "request_number": self.prr.number,
            "subject": "Draft denial",
            "body": "The requested records are fully exempt from disclosure.",
        }

        missing = self.client.post(
            "/api/staff/correspondence/drafts/denial/",
            payload,
            content_type="application/json",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("exemption_citation_ids", missing.json())

        created = self.client.post(
            "/api/staff/correspondence/drafts/denial/",
            payload | {
                "exemption_citation_ids": [self.denial_exemption.pk],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(
            created.json()["denial_exemption_citations"],
            [
                {
                    "id": self.denial_exemption.pk,
                    "citation": "RCW 42.56.250",
                    "short_name": "Examination questions",
                    "description": "",
                    "active": True,
                    "requires_second_review": False,
                }
            ],
        )

    def test_patch_openapi_contract_has_disjoint_required_edit_shapes(self):
        self.login(self.officer)

        response = self.client.get(
            "/api/schema/",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        schema = response.json()["paths"][
            "/api/staff/correspondence/{id}/"
        ]["patch"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertNotIn("$ref", schema)
        self.assertEqual(len(schema["oneOf"]), 7)
        self.assertSetEqual(
            {
                frozenset(branch["required"])
                for branch in schema["oneOf"]
            },
            {
                frozenset({"expected_version", "subject", "body"}),
                frozenset(
                    {
                        "expected_version",
                        "subject",
                        "body",
                        "exemption_citation_ids",
                    }
                ),
                frozenset({"expected_version", "estimated_completion"}),
                frozenset(
                    {
                        "expected_version",
                        "clarification_questions",
                        "clarification_due",
                    }
                ),
                frozenset({"expected_version", "new_date", "reason"}),
                frozenset(
                    {
                        "expected_version",
                        "form",
                        "match_count",
                        "query_summary",
                        "narrowing_options",
                        "response_by",
                    }
                ),
                frozenset(
                    {"expected_version", "is_final"}
                ),
            },
        )
        self.assertTrue(
            all(
                branch["additionalProperties"] is False
                for branch in schema["oneOf"]
            )
        )

    def test_liaison_can_create_and_edit_general_draft_for_assigned_request(self):
        self.login(self.liaison)
        created = self.client.post(
            "/api/staff/correspondence/drafts/general/",
            {
                "request_number": self.prr.number,
                "subject": "Initial subject",
                "body": "Initial body",
                "recipients": [
                    {
                        "recipient_type": "to",
                        "email": "jane@example.org",
                        "display_name": "Jane Requester",
                    },
                    {
                        "recipient_type": "cc",
                        "email": "counsel@psd401.net",
                        "display_name": "District Counsel",
                    },
                ],
            },
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201)
        correspondence_id = created.json()["id"]
        version = created.json()["version"]
        edited = self.client.patch(
            f"/api/staff/correspondence/{correspondence_id}/",
            {
                "expected_version": version,
                "subject": "Edited subject",
                "body": "Edited body",
            },
            content_type="application/json",
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["draft_subject"], "Edited subject")
        self.assertEqual(
            Correspondence.objects.get(pk=correspondence_id).created_by,
            self.liaison,
        )

    def test_stale_draft_edit_returns_conflict(self):
        draft = self.general_draft()
        stale_version = draft.version
        services.edit_draft(
            draft,
            subject="Concurrent edit",
            body="Another staff member saved first.",
            actor=self.liaison,
            expected_version=stale_version,
        )
        self.login(self.liaison)

        response = self.client.patch(
            f"/api/staff/correspondence/{draft.id}/",
            {
                "expected_version": stale_version,
                "subject": "Stale",
                "body": "Stale body",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")

    def test_structured_patch_rerenders_and_preserves_requester_delivery(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions=(
                "Please identify the employee names and date range requested."
            ),
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )
        self.login(self.liaison)

        rejected = self.client.patch(
            f"/api/staff/correspondence/{draft.id}/",
            {
                "expected_version": draft.version,
                "clarification_questions": (
                    "Please identify the departments and date range requested."
                ),
                "clarification_due": "2026-08-14",
                "recipients": [
                    {
                        "recipient_type": "to",
                        "email": "counsel@example.org",
                    }
                ],
            },
            content_type="application/json",
        )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["code"], "validation_error")
        self.assertIn("current requester", rejected.json()["detail"])
        draft.refresh_from_db()
        self.assertEqual(draft.version, 1)
        self.assertEqual(draft.recipients.get().email, self.prr.requester.email)

        edited = self.client.patch(
            f"/api/staff/correspondence/{draft.id}/",
            {
                "expected_version": draft.version,
                "clarification_questions": (
                    "Please identify the departments and precise date range "
                    "that should be searched."
                ),
                "clarification_due": "2026-08-14",
                "recipients": [
                    {
                        "recipient_type": "to",
                        "email": self.prr.requester.email,
                    },
                    {
                        "recipient_type": "cc",
                        "email": "counsel@example.org",
                    },
                ],
            },
            content_type="application/json",
        )

        self.assertEqual(edited.status_code, 200)
        body = edited.json()
        self.assertEqual(
            body["draft_metadata"],
            {
                "clarification_questions": (
                    "Please identify the departments and precise date range "
                    "that should be searched."
                ),
                "clarification_due": "2026-08-14",
            },
        )
        self.assertIn("departments and precise date range", body["draft_body"])
        self.assertIn("August 14, 2026", body["draft_body"])

    def test_structured_patch_rejects_fields_outside_its_contract(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )
        self.login(self.liaison)

        response = self.client.patch(
            f"/api/staff/correspondence/{draft.id}/",
            {
                "expected_version": draft.version,
                "clarification_questions": "Which departments are in scope?",
                "clarification_due": "2026-08-14",
                "subject": "This authored field must not be ignored",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("subject", response.json())
        draft.refresh_from_db()
        self.assertEqual(draft.version, 1)

    def test_liaison_cannot_edit_officer_only_legal_drafts(self):
        self.login(self.liaison)

        for kind in (Correspondence.Kind.CLOSURE, Correspondence.Kind.DENIAL):
            with self.subTest(kind=kind):
                denial_basis = (
                    {
                        "denial_exemption_citation_ids": [
                            self.denial_exemption.pk
                        ]
                    }
                    if kind == Correspondence.Kind.DENIAL
                    else {}
                )
                draft = services.create_authored_draft(
                    self.prr,
                    kind=kind,
                    subject="Officer legal draft",
                    body="Records Officer review required.",
                    actor=self.officer,
                    **denial_basis,
                )
                response = self.client.patch(
                    f"/api/staff/correspondence/{draft.id}/",
                    {
                        "expected_version": draft.version,
                        "subject": "Liaison rewrite",
                        "body": "This edit must be rejected.",
                    },
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 403)
                draft.refresh_from_db()
                self.assertEqual(draft.draft_subject, "Officer legal draft")
                self.assertEqual(
                    draft.draft_body,
                    "Records Officer review required.\n",
                )

    def test_liaison_cannot_edit_released_installment_notice(self):
        installment = self.released_installment()
        draft = services.create_installment_notice_draft(
            self.prr,
            installment=installment,
            is_final=False,
            actor=self.officer,
        )
        self.login(self.liaison)

        response = self.client.patch(
            f"/api/staff/correspondence/{draft.id}/",
            {
                "expected_version": draft.version,
                "is_final": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        draft.refresh_from_db()
        self.assertFalse(draft.draft_metadata["is_final"])

    def test_supported_structured_and_authored_draft_actions(self):
        installment = self.released_installment()
        cases = [
            (
                "acknowledgment",
                {"request_number": self.prr.number},
                Correspondence.Kind.ACKNOWLEDGMENT,
            ),
            (
                "clarification",
                {
                    "request_number": self.prr.number,
                    "clarification_questions": "Which school years are in scope?",
                    "clarification_due": "2026-08-01",
                },
                Correspondence.Kind.CLARIFICATION_REQUEST,
            ),
            (
                "estimate-revision",
                {
                    "request_number": self.prr.number,
                    "new_date": "2026-08-17",
                    "reason": "An additional archive requires legal review.",
                },
                Correspondence.Kind.ESTIMATE_REVISION,
            ),
            (
                "installment",
                {
                    "request_number": self.prr.number,
                    "installment_id": installment.id,
                    "is_final": False,
                },
                Correspondence.Kind.INSTALLMENT_NOTICE,
            ),
            (
                "responsive-records",
                {
                    "request_number": self.prr.number,
                    "subject": "Responsive records available",
                    "body": "Use the secure application link.",
                },
                Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE,
            ),
            (
                "closure",
                {
                    "request_number": self.prr.number,
                    "subject": "Request complete",
                    "body": "Records Officer review required.",
                },
                Correspondence.Kind.CLOSURE,
            ),
            (
                "denial",
                {
                    "request_number": self.prr.number,
                    "subject": "Draft denial",
                    "body": "Records Officer and counsel review required.",
                    "exemption_citation_ids": [self.denial_exemption.pk],
                },
                Correspondence.Kind.DENIAL,
            ),
        ]
        self.login()

        for path, payload, kind in cases:
            with self.subTest(path=path):
                response = self.client.post(
                    f"/api/staff/correspondence/drafts/{path}/",
                    payload,
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(response.json()["kind"], kind)
                self.assertEqual(response.json()["status"], Correspondence.Status.DRAFT)

    def test_attributed_agent_can_draft_estimate_but_integration_cannot(self):
        payload = {
            "request_number": self.prr.number,
            "new_date": "2026-08-17",
            "reason": "An additional archive requires legal review.",
        }
        token_response = self.client.post(
            "/api/staff/correspondence/drafts/estimate-revision/",
            payload,
            content_type="application/json",
            **self.token_headers(self.liaison),
        )
        integration = get_user_model().objects.create_user(
            "integration",
            is_staff=False,
        )
        integration_response = self.client.post(
            "/api/staff/correspondence/drafts/estimate-revision/",
            payload,
            content_type="application/json",
            **self.token_headers(integration),
        )

        self.assertEqual(token_response.status_code, 201)
        self.assertEqual(integration_response.status_code, 403)


class ApprovalDeliveryActionApiTests(CorrespondenceApiTestCase):
    def test_approved_denial_rejects_taxonomy_rewrite_and_preserves_basis_text(self):
        self.denial_exemption.description = "Original legal description."
        self.denial_exemption.save(update_fields=["description"])
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        self.denial_exemption.citation = "RCW 42.56.999"
        self.denial_exemption.short_name = "Changed taxonomy label"
        self.denial_exemption.description = "Changed taxonomy description."
        with self.assertRaises(ValidationError):
            self.denial_exemption.save(
                update_fields=["citation", "short_name", "description"]
            )
        self.denial_exemption.refresh_from_db()
        self.login()

        detail = self.client.get(
            f"/api/staff/correspondence/{approved.id}/"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["denial_exemption_citations"],
            [
                {
                    "id": self.denial_exemption.pk,
                    "citation": "RCW 42.56.250",
                    "short_name": "Examination questions",
                    "description": "Original legal description.",
                    "active": True,
                    "requires_second_review": False,
                }
            ],
        )

        queued = self.client.post(
            f"/api/staff/correspondence/{approved.id}/queue/",
            {"expected_version": approved.version},
            content_type="application/json",
        )
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["status"], Correspondence.Status.QUEUED)

    def test_preview_includes_safe_attachment_delivery_descriptors(self):
        draft = self.general_draft()
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=draft,
            original_filename="supporting-records.pdf",
            content_type="application/pdf",
            size=42,
            sha256="a" * 64,
        )
        self.login()

        response = self.client.get(
            f"/api/staff/correspondence/{draft.id}/preview/"
        )

        self.assertEqual(response.status_code, 200)
        descriptor = response.json()["attachments"][0]
        self.assertEqual(
            descriptor,
            {
                "id": str(attachment.id),
                "original_filename": "supporting-records.pdf",
                "content_type": "application/pdf",
                "size": 42,
                "sha256": "a" * 64,
                "scan_status": CorrespondenceAttachment.ScanStatus.PENDING,
                "quarantined": True,
                "quarantine_reason": "",
                "is_downloadable": False,
                "created_at": descriptor["created_at"],
                "scanned_at": None,
            },
        )
        serialized = str(response.json())
        self.assertNotIn("storage_key", serialized)
        self.assertNotIn(attachment.storage_key, serialized)

    def test_preview_approve_and_queue_use_domain_services(self):
        draft = self.general_draft()
        self.login()

        preview = self.client.get(
            f"/api/staff/correspondence/{draft.id}/preview/"
        )
        approved = self.client.post(
            f"/api/staff/correspondence/{draft.id}/approve/",
            {"expected_version": draft.version},
            content_type="application/json",
        )
        queued = self.client.post(
            f"/api/staff/correspondence/{draft.id}/queue/",
            {"expected_version": approved.json()["version"]},
            content_type="application/json",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["subject"], "Case update")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], Correspondence.Status.APPROVED)
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["status"], Correspondence.Status.QUEUED)
        self.assertEqual(
            queued.json()["delivery"]["status"],
            CorrespondenceDelivery.Status.PENDING,
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_queue_succeeds_without_external_transport_configuration(self):
        draft = self.general_draft()
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        self.login()

        with (
            patch("apps.correspondence.tasks.enqueue_queued_delivery") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                f"/api/staff/correspondence/{approved.id}/queue/",
                {"expected_version": approved.version},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.QUEUED)
        delivery = CorrespondenceDelivery.objects.get(correspondence=approved)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    def test_queue_fails_closed_when_outbound_transport_is_disabled(self):
        draft = self.general_draft()
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        self.login()

        response = self.client.post(
            f"/api/staff/correspondence/{approved.id}/queue/",
            {"expected_version": approved.version},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "outbound_transport_disabled")
        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(
                correspondence=approved,
            ).exists()
        )

    def test_application_admin_cannot_approve_or_queue(self):
        draft = self.general_draft()
        self.login(self.admin)

        response = self.client.post(
            f"/api/staff/correspondence/{draft.id}/approve/",
            {"expected_version": draft.version},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_protected_approval_and_queue_return_token_handoffs(self):
        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires legal review.",
            actor=self.liaison,
        )
        auth = self.token_headers(self.officer)

        response = self.client.post(
            f"/api/staff/correspondence/{draft.id}/approve/",
            {"expected_version": draft.version},
            content_type="application/json",
            **auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertEqual(response.json()["handoff"]["required_role"], "records_officer")
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        queued = self.client.post(
            f"/api/staff/correspondence/{approved.id}/queue/",
            {"expected_version": approved.version},
            content_type="application/json",
            **auth,
        )
        self.assertEqual(queued.status_code, 403)
        self.assertEqual(
            queued.json()["handoff"]["capability_id"],
            "correspondence.queue",
        )
        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(
                correspondence=approved
            ).exists()
        )

    def test_cancel_and_retry_reuse_delivery(self):
        draft = self.general_draft()
        correspondence, _delivery = self.approve_and_queue(draft)
        self.login()
        cancelled = self.client.post(
            f"/api/staff/correspondence/{correspondence.id}/cancel/",
            {
                "expected_version": correspondence.version,
                "reason": "Superseded before it reached the transport.",
            },
            content_type="application/json",
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], Correspondence.Status.CANCELLED)

        retry_draft = self.general_draft()
        retry_correspondence, retry_delivery = self.approve_and_queue(retry_draft)
        failed_at = timezone.now()
        failure_category = "temporary_provider_failure"
        failure_description = (
            services.RETRYABLE_DELIVERY_FAILURES[failure_category]
        )
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=retry_delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(self.officer.pk),
            claimed_at=failed_at,
            result_at=failed_at,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            error_category=failure_category,
            provider_response_metadata={},
            result_fingerprint="b" * 64,
            actor=self.officer,
        )
        retry_delivery.status = CorrespondenceDelivery.Status.RETRYABLE
        retry_delivery.attempt_count = 1
        retry_delivery.last_failure_category = failure_category
        retry_delivery.last_failure_description = failure_description
        retry_delivery.completed_at = failed_at
        retry_delivery.save(
            update_fields=[
                "status",
                "attempt_count",
                "last_failure_category",
                "last_failure_description",
                "completed_at",
            ]
        )
        retry_correspondence.status = Correspondence.Status.FAILED
        retry_correspondence.failed_at = failed_at
        retry_correspondence.failure_category = failure_category
        retry_correspondence.failure_description = failure_description
        retry_correspondence.save(
            update_fields=[
                "status",
                "failed_at",
                "failure_category",
                "failure_description",
            ]
        )

        retried = self.client.post(
            f"/api/staff/correspondence/{retry_correspondence.id}/retry/",
            {"expected_delivery_version": retry_delivery.version},
            content_type="application/json",
        )

        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["delivery"]["id"], str(retry_delivery.id))
        self.assertEqual(
            retried.json()["delivery"]["status"],
            CorrespondenceDelivery.Status.PENDING,
        )


class CorrespondenceAttachmentApiTests(CorrespondenceApiTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self.general_draft()
        self.attachment = CorrespondenceAttachment.objects.create(
            correspondence=self.draft,
            original_filename="requester-context.txt",
            content_type="text/plain",
            size=4,
            sha256=hashlib.sha256(b"safe").hexdigest(),
            uploaded_by=self.liaison,
        )

    def release_attachment(self):
        self.attachment = CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=self.attachment.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{self.draft.pk}/"
                f"{self.attachment.pk}"
            ),
        )

    def test_unscanned_attachment_download_fails_closed(self):
        self.login(self.liaison)
        response = self.client.post(
            f"/api/staff/correspondence-attachments/{self.attachment.id}/download/"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "attachment_not_clean")

    @patch("apps.correspondence.services.open_clean_attachment")
    def test_clean_download_is_short_lived_user_bound_and_audited(self, open_file):
        self.release_attachment()
        open_file.return_value = BytesIO(b"safe")
        self.login(self.liaison)

        authorized = self.client.post(
            f"/api/staff/correspondence-attachments/{self.attachment.id}/download/"
        )
        self.login(self.officer)
        wrong_user = self.client.get(authorized.json()["url"])
        self.login(self.liaison)
        downloaded = self.client.get(authorized.json()["url"])

        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()["expires_in"], 300)
        self.assertEqual(wrong_user.status_code, 404)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(b"".join(downloaded.streaming_content), b"safe")
        self.assertEqual(downloaded["Cache-Control"], "no-store")
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence_attachment.downloaded",
                actor=self.liaison,
            ).exists()
        )

    @patch("apps.correspondence.services.open_clean_attachment")
    def test_download_rechecks_quarantine_after_authorization(self, open_file):
        self.release_attachment()
        self.login(self.liaison)
        authorized = self.client.post(
            f"/api/staff/correspondence-attachments/{self.attachment.id}/download/"
        )
        CorrespondenceAttachment.objects.filter(
            pk=self.attachment.pk
        ).requarantine_locked(
            quarantine_reason="Clean release was revoked after security review.",
            quarantined_at=timezone.now(),
        )

        downloaded = self.client.get(authorized.json()["url"])

        self.assertEqual(downloaded.status_code, 404)
        self.assertEqual(downloaded.json()["code"], "not_found")
        open_file.assert_not_called()

    @patch(
        "apps.correspondence.services.open_clean_attachment",
        side_effect=OSError("private storage diagnostic"),
    )
    def test_download_sanitizes_retryable_storage_failures(self, open_file):
        self.release_attachment()
        self.login(self.liaison)
        authorized = self.client.post(
            f"/api/staff/correspondence-attachments/{self.attachment.id}/download/"
        )

        downloaded = self.client.get(authorized.json()["url"])

        self.assertEqual(downloaded.status_code, 503)
        self.assertEqual(downloaded.json()["code"], "attachment_unavailable")
        self.assertNotIn("private storage diagnostic", str(downloaded.json()))
        self.assertEqual(downloaded["Retry-After"], "5")
        open_file.assert_called_once()


class ClarificationCycleApiTests(CorrespondenceApiTestCase):
    def setUp(self):
        super().setUp()
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 8, 1),
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        now = timezone.now()
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(self.officer.pk),
            claimed_at=now,
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id="gmail-clarification",
            provider_thread_id="thread-clarification",
            provider_timestamp=now,
            result_fingerprint="a" * 64,
            actor=self.officer,
        )
        delivery.status = CorrespondenceDelivery.Status.SENT
        delivery.provider_message_id = "gmail-clarification"
        delivery.provider_thread_id = "thread-clarification"
        delivery.attempt_count = 1
        delivery.successful_attempt_number = 1
        delivery.completed_at = now
        delivery.save()
        correspondence.status = Correspondence.Status.SENT
        correspondence.sent_at = now
        correspondence.provider_message_id = "gmail-clarification"
        correspondence.provider_thread_id = "thread-clarification"
        correspondence.save()
        services.apply_successful_delivery_effects(correspondence)
        self.cycle = ClarificationCycle.objects.get(request=self.prr)
        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        self.cycle.inbound_response_correspondence = inbound
        self.cycle.responded_at = timezone.now()
        self.cycle.save()

    def test_list_and_detail_expose_cycle_state(self):
        self.login(self.reviewer)
        listed = self.client.get(
            f"/api/staff/clarification-cycles/?request={self.prr.number}"
        )
        detail = self.client.get(
            f"/api/staff/clarification-cycles/{self.cycle.id}/"
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["results"][0]["request_number"], self.prr.number)
        self.assertNotIn("resolution_notes", listed.json()["results"][0])
        self.assertEqual(
            detail.json()["inbound_response_correspondence_id"],
            str(self.cycle.inbound_response_correspondence_id),
        )

    def test_token_agent_gets_handoff_and_session_officer_resolves(self):
        payload = {
            "resolution_notes": (
                "Scope confirmed as school years 2024 through 2026."
            )
        }
        token_response = self.client.post(
            f"/api/staff/clarification-cycles/{self.cycle.id}/resolve/",
            payload,
            content_type="application/json",
            **self.token_headers(self.officer),
        )
        self.assertEqual(token_response.status_code, 403)
        self.assertEqual(token_response.json()["code"], "human_approval_required")

        self.login()
        resolved = self.client.post(
            f"/api/staff/clarification-cycles/{self.cycle.id}/resolve/",
            payload,
            content_type="application/json",
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertIsNotNone(resolved.json()["resolved_at"])
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)
