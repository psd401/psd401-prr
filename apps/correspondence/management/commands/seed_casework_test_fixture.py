"""Seed deterministic synthetic casework data for local and CI browser tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from apps.api.identity import apply_staff_roles, role_group_name, staff_roles
from apps.api.models import StaffIdentity, StaffRole
from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.correspondence.inbound import _provider_thread_evidence
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
    _validated_message_id_domain,
)
from apps.correspondence.receiving import _payload_digest
from apps.correspondence.services import (
    PERMANENT_DELIVERY_FAILURES,
    _content_hash,
    _result_fingerprint,
)
from apps.records.models import (
    AdvisoryRun,
    Channel,
    CollectionTask,
    EvaluationRun,
    EvaluationSet,
    KnowledgeRoleAccount,
    PublicRecordsRequest,
    RecordItem,
    RecordItemCustodian,
    RecordsEstimate,
    RecordSystem,
    Requester,
    RequestStatus,
    StatusEvent,
    VaultCountRun,
)
from apps.records.service_identities import (
    SYSTEM_INBOUND_TRANSPORT_USERNAME,
    SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
    is_exact_inbound_identity,
    is_exact_outbound_identity,
)
from apps.work_queue.models import WorkItem
from apps.work_queue.reconciliation import reconcile_inbound_receipt
from apps.work_queue.services import assign_work_item
from apps.work_queue.source_reconciliation import (
    reconcile_collection_task_work,
    reconcile_record_item_work,
    reconcile_request_work,
)

FIXTURE_MESSAGE_COUNT = 2_000
_FIXTURE_DRAFT_INDEXES = frozenset({1, 3, 6, 7, 8, 9, 10, 11})
FIXTURE_SEARCHABLE_MESSAGE_COUNT = FIXTURE_MESSAGE_COUNT - len(
    _FIXTURE_DRAFT_INDEXES
)
FIXTURE_REQUEST_NUMBER = "PRR-E2E-2000"
#: A second, deliberately small case that is RECEIVED with no delivered
#: initial response. It is the only fixture case in the day-5 backlog, so the
#: Inbox statutory view and the guided send steps have real data to prove.
FIXTURE_AWAITING_REQUEST_NUMBER = "PRR-E2E-0005"
FIXTURE_AWAITING_DUE_DATE = date(2026, 1, 20)
FIXTURE_AWAITING_DESCRIPTION = (
    "Bus camera footage and dispatch logs for the afternoon route on "
    "January 6, 2026."
)
#: The pair the "add this email to an existing case" flow needs: one case that
#: intake opened automatically from an emailed follow-up, and one real case it
#: belongs to. Deliberately isolated from the primary fixture case so moving
#: the message between them disturbs no other browser flow's counts.
FIXTURE_EMAIL_INTAKE_REQUEST_NUMBER = "PRR-E2E-0006"
FIXTURE_EMAIL_INTAKE_SUBJECT = "Re: bus route records — one more thing"
FIXTURE_EMAIL_INTAKE_DESCRIPTION = (
    "One more thing on the bus route records I already asked you about: "
    "please include the January 6 dispatch notes as well."
)
FIXTURE_REDIRECT_TARGET_REQUEST_NUMBER = "PRR-E2E-0007"
#: A case that arrived by forward, so intake recorded the relaying mailbox as
#: the requester. It exists to prove the requester-identity correction end to
#: end, and is deliberately isolated from every other fixture case: correcting
#: its identity and sending its acknowledgment must not move a case another
#: flow counts on being in the day-5 backlog.
FIXTURE_FORWARDED_REQUEST_NUMBER = "PRR-E2E-0008"
#: A case whose completed Vault collection swept in mail nobody has reviewed.
#: It exists to prove superseding a collection end to end, and is isolated so
#: taking its records out of review moves no count another flow depends on.
FIXTURE_SUPERSEDE_REQUEST_NUMBER = "PRR-E2E-0009"
FIXTURE_SUPERSEDE_DESCRIPTION = (
    "Email about the spring facilities survey between March 1 and March 31, "
    "2026."
)
FIXTURE_SUPERSEDE_RECORD_COUNT = 3
#: A case whose interpretation-and-estimate letter the system has drafted
#: (#42). Isolated so sending it changes no count another flow reads.
FIXTURE_ESTIMATE_LETTER_REQUEST_NUMBER = "PRR-E2E-0010"
#: A case whose records carry AI suggestions for the trust-ladder flows (#39):
#: a newsletter cluster below the discard floor, for sampled cluster approval,
#: and a high-confidence non-responsive pile, for the elusion sign-off.
FIXTURE_TRUST_REQUEST_NUMBER = "PRR-E2E-0011"
FIXTURE_TRUST_CLUSTER_DOMAIN = "newsletter.example"
FIXTURE_TRUST_CLUSTER_COUNT = 4
FIXTURE_TRUST_DISCARD_DOMAIN = "vendor-ads.example"
FIXTURE_TRUST_DISCARD_COUNT = 5
#: A case whose responsive records carry AI redaction proposals (#40), for
#: the group-acceptance flow. Its PDFs live in the e2e filesystem store.
FIXTURE_REDACTION_REQUEST_NUMBER = "PRR-E2E-0012"
FIXTURE_REDACTION_RECORD_COUNT = 3
#: A case of unreviewed PDFs with AI suggestions for the keyboard Review tab
#: (#46). Its PDFs live in the e2e filesystem store.
FIXTURE_FLIGHT_REQUEST_NUMBER = "PRR-E2E-0013"
FIXTURE_FLIGHT_RECORDS = (
    ("flight-bus-video", "Route 12 bus video request log, March 3.", "responsive", 0.94),
    ("flight-lunch-menu", "Cafeteria lunch menu for the week of March 2.", "non_responsive", 0.97),
    ("flight-route-memo", "Memo about route 12 driver staffing.", "responsive", 0.71),
)
#: A case with a finished Vault collection and a newer, narrower proposed
#: scope, for the re-scope flow (#37).
FIXTURE_RESCOPE_REQUEST_NUMBER = "PRR-E2E-0014"
FIXTURE_ESTIMATE_LETTER_INTENT = (
    "Route 12 bus video from March 3, 2026, between 3 and 4 p.m."
)
#: Uploaded (not Vault-collected) mail on the same isolated case, from one
#: sender domain, so the bulk-review-by-filter flow has a set to decide that
#: superseding the Vault collection never touches.
FIXTURE_BULK_FILTER_DOMAIN = "bulk.example.org"
FIXTURE_BULK_FILTER_RECORD_COUNT = 3
#: A synthetic registry system and one role account for the knowledge-pack
#: flow. The system has no triage keywords, so it routes no fixture case.
FIXTURE_KNOWLEDGE_SYSTEM_SLUG = "fixture-shared-mailbox"
FIXTURE_KNOWLEDGE_ROLE = "Principal, Fixture Elementary"
FIXTURE_FORWARDED_DESCRIPTION = (
    "Copies of the traffic-safety study for the Harborview crossing, and any "
    "district correspondence about it from February 2026."
)
#: Deliberately a different date from the other backlog case: two fixture
#: cases sharing one due date makes the Inbox's own due-date text ambiguous
#: for any flow that asserts on it.
FIXTURE_FORWARDED_DUE_DATE = date(2026, 1, 22)
FIXTURE_FORWARDED_REQUESTER_NAME = "District Info Mailbox"
FIXTURE_FORWARDED_REQUESTER_EMAIL = "info-mailbox@example.org"
FIXTURE_REDIRECT_TARGET_DESCRIPTION = (
    "Bus route records and dispatch notes for the afternoon route in "
    "January 2026."
)
FIXTURE_USERNAME = "e2e-officer"
FIXTURE_PASSWORD = "e2e-local-only"
FIXTURE_OBSERVER_USERNAME = "e2e-observer"
FIXTURE_OBSERVER_PASSWORD = "e2e-observer-local-only"
FIXTURE_LIAISON_USERNAME = "e2e-liaison"
FIXTURE_LIAISON_PASSWORD = "e2e-liaison-local-only"
FIXTURE_REVIEWER_USERNAME = "e2e-reviewer"
FIXTURE_REVIEWER_PASSWORD = "e2e-reviewer-local-only"
FIXTURE_INTAKE_USERNAME = "e2e-intake"
FIXTURE_INTAKE_PASSWORD = "e2e-intake-local-only"
FIXTURE_ADMIN_USERNAME = "e2e-admin"
FIXTURE_ADMIN_PASSWORD = "e2e-admin-local-only"
FIXTURE_DESCRIPTION = (
    "All district correspondence about the synthetic Harborview planning "
    "project from January through March 2026."
)
_FIXTURE_NAMESPACE = uuid.UUID("760148cf-eb74-54d6-9b0d-264847a4c663")
_PACIFIC = ZoneInfo("America/Los_Angeles")
_BASE_TIME = datetime(2026, 1, 5, 9, 0, tzinfo=_PACIFIC)
_FAILURE_CATEGORY = "message_rejected"
_FAILURE_DESCRIPTION = PERMANENT_DELIVERY_FAILURES[_FAILURE_CATEGORY]
_SENT_PROVIDER_METADATA = {"http_status": 200, "provider_code": "fixture"}
_FAILED_PROVIDER_METADATA = {"http_status": 400, "provider_code": "fixture"}
_INBOUND_MESSAGE_INDEXES = frozenset({0, 5})


def _seed_scope_conversation(request) -> None:
    """The state a case is in when the scope conversation is worth having.

    An intent proposal that ran on arrival, and the entire-organization count
    it queued. Neither was requested by a person, which is the point: the
    officer opens the case and the number is already there. Nothing here
    reaches the requester.
    """
    AdvisoryRun.objects.update_or_create(
        pk=_fixture_uuid("scope-intent-advisory"),
        defaults={
            "kind": AdvisoryRun.Kind.REQUEST_INTENT,
            "state": AdvisoryRun.State.COMPLETE,
            "request": request,
            "trigger": AdvisoryRun.Trigger.AUTOMATIC,
            "requested_by": None,
            "model_identifier": "synthetic-fixture-model",
            "input_sha256": "e" * 64,
            "proposal": {
                "summary": (
                    "Synthetic transportation-plan correspondence request."
                ),
                "required_search_terms": ["transportation plan"],
                "search_terms": ["bus route", "bell schedule"],
                "record_system_slugs": [],
                "clarification_questions": [],
                "complexity_flags": ["broad_scope", "potentially_large_volume"],
                "date_range": {"start": "2025-09-01", "end": "2026-01-05"},
                "narrowing_options": [
                    {
                        "terms": ["transportation plan", "2025"],
                        "rationale": (
                            "Only the 2025 transportation plan and its "
                            "revisions."
                        ),
                    },
                    {
                        "terms": ["bus route"],
                        "rationale": (
                            "Only route-level correspondence, not the plan "
                            "itself."
                        ),
                    },
                ],
            },
            "completed_at": _BASE_TIME,
        },
    )
    VaultCountRun.objects.update_or_create(
        pk=_fixture_uuid("scope-vault-count"),
        defaults={
            "request": request,
            "trigger": VaultCountRun.Trigger.AUTOMATIC,
            "requested_by": None,
            "state": VaultCountRun.State.COMPLETE,
            "search_scope": VaultCountRun.SearchScope.ENTIRE_ORG,
            "terms": (
                '"transportation plan" AND ("bus route" OR "bell schedule")'
            ),
            "custodian_emails": [],
            "date_range_start": date(2025, 9, 1),
            "date_range_end": date(2026, 1, 5),
            "input_sha256": "f" * 64,
            "total_count": 29380,
            "queried_accounts_count": 1200,
            "matching_accounts_count": 318,
            "non_queryable_accounts_count": 0,
            "accounts_with_errors_count": 0,
            "completed_at": _BASE_TIME,
        },
    )


def _fixture_uuid(label: str) -> uuid.UUID:
    return uuid.uuid5(_FIXTURE_NAMESPACE, label)


def _body(index: int) -> str:
    body = (
        f"Synthetic casework message {index:04d}. This record contains no "
        "requester or district production data."
    )
    if index % 199 == 0:
        body += "\n\nLONG-BODY-CONTENT\n" + ("Synthetic paragraph. " * 1_200)
    return body


def _message(index: int, *, request, officer, inbound_transport) -> Correspondence:
    message_time = _BASE_TIME + timedelta(minutes=index)
    direction = (
        Correspondence.Direction.INBOUND
        if index in _INBOUND_MESSAGE_INDEXES
        else Correspondence.Direction.OUTBOUND
    )
    received_at = {
        0: _BASE_TIME + timedelta(minutes=5),
        5: _BASE_TIME + timedelta(minutes=7),
    }.get(index)
    message_id = _fixture_uuid(f"correspondence-{index}")
    body = _body(index)
    common = {
        "id": message_id,
        "request": request,
        "direction": direction,
        "kind": Correspondence.Kind.GENERAL,
        "status": Correspondence.Status.RECEIVED,
        "created_by": (
            officer
            if direction == Correspondence.Direction.OUTBOUND
            else inbound_transport
        ),
        "provider": "gmail",
        "provider_account": "default",
        "provider_message_id": "",
        "provider_thread_id": "",
        "reply_token": _fixture_uuid(f"reply-token-{index}"),
        "draft_metadata": {"fixture_index": index},
        "created_at": (
            received_at + timedelta(seconds=1)
            if direction == Correspondence.Direction.INBOUND
            else message_time - timedelta(seconds=2)
        ),
    }

    if direction == Correspondence.Direction.INBOUND:
        common.update(
            received_subject=f"Synthetic inbound message {index:04d}",
            received_body=body,
            received_sender=f"synthetic-sender-{index % 9}@example.org",
            received_recipients=["publicrecords@example.org"],
            received_headers={"x-fixture-index": str(index)},
            received_at=received_at,
            provider_message_id=f"fixture-message-{index:04d}",
            provider_thread_id=(
                "fixture-thread-02"
                if index == 0
                else f"fixture-thread-{index % 23:02d}"
            ),
        )
        if index == 0:
            common["kind"] = Correspondence.Kind.CLARIFICATION_RESPONSE
        elif index == 5:
            common["kind"] = Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
        common.update(
            matched_at=received_at + timedelta(seconds=2),
            version=2,
        )
        return Correspondence(**common)

    if index == 2:
        status = Correspondence.Status.SENT
    elif index == 4:
        status = Correspondence.Status.FAILED
    elif index in _FIXTURE_DRAFT_INDEXES:
        status = Correspondence.Status.DRAFT
    else:
        status = Correspondence.Status.APPROVED
    subject = f"Synthetic outbound message {index:04d}"
    common.update(
        kind=(
            Correspondence.Kind.CLARIFICATION_REQUEST
            if index == 2
            else Correspondence.Kind.GENERAL
        ),
        status=status,
        draft_subject=subject,
        draft_body=body,
        rfc_message_id=(
            f"<{message_id}.correspondence@{_validated_message_id_domain()}>"
        ),
    )
    if status != Correspondence.Status.DRAFT:
        common.update(
            approved_subject=subject,
            approved_body=body,
            approved_metadata={"fixture_index": index},
            approved_by=officer,
            approved_at=message_time,
            version=(
                4
                if status in {
                    Correspondence.Status.SENT,
                    Correspondence.Status.FAILED,
                }
                else 2
            ),
        )
    if status in {Correspondence.Status.SENT, Correspondence.Status.FAILED}:
        common.update(
            queued_by=officer,
            queued_at=message_time + timedelta(minutes=1),
        )
    if status == Correspondence.Status.SENT:
        common.update(
            sent_at=message_time + timedelta(minutes=2),
            provider_message_id=f"fixture-message-{index:04d}",
            provider_thread_id=f"fixture-thread-{index % 23:02d}",
        )
    elif status == Correspondence.Status.FAILED:
        common.update(
            failed_at=message_time + timedelta(minutes=2),
            failure_category=_FAILURE_CATEGORY,
            failure_description=_FAILURE_DESCRIPTION,
        )
    return Correspondence(**common)


def _seed_users():
    User = get_user_model()
    officer = User.objects.filter(username=FIXTURE_USERNAME).first()
    if officer is None:
        officer = User.objects.create_user(
            username=FIXTURE_USERNAME,
            email="e2e-officer@example.org",
            first_name="Synthetic",
            last_name="Officer",
            password=FIXTURE_PASSWORD,
            is_active=True,
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=officer,
            source=StaffIdentity.Source.LOCAL,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        return officer

    identity = StaffIdentity.objects.filter(user=officer).first()
    group_names = set(officer.groups.values_list("name", flat=True))
    expected_group = role_group_name(StaffRole.RECORDS_OFFICER)
    if (
        officer.email != "e2e-officer@example.org"
        or officer.first_name != "Synthetic"
        or officer.last_name != "Officer"
        or not officer.is_active
        or not officer.is_staff
        or officer.is_superuser
        or not officer.check_password(FIXTURE_PASSWORD)
        or officer.user_permissions.exists()
        or officer.groups.filter(permissions__isnull=False).exists()
        or identity is None
        or identity.source != StaffIdentity.Source.LOCAL
        or identity.google_subject
        or identity.allow_next_google_link
        or group_names != {expected_group}
        or staff_roles(officer) != {StaffRole.RECORDS_OFFICER}
    ):
        raise CommandError(
            f"{FIXTURE_USERNAME} exists but is not the designated fixture identity."
        )
    return officer


def _seed_observer():
    User = get_user_model()
    observer = User.objects.filter(username=FIXTURE_OBSERVER_USERNAME).first()
    if observer is None:
        observer = User.objects.create_user(
            username=FIXTURE_OBSERVER_USERNAME,
            email="e2e-observer@example.org",
            first_name="Synthetic",
            last_name="Observer",
            password=FIXTURE_OBSERVER_PASSWORD,
            is_active=True,
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=observer,
            source=StaffIdentity.Source.LOCAL,
        )
        apply_staff_roles(observer, {StaffRole.RECORDS_OFFICER})
        return observer

    identity = StaffIdentity.objects.filter(user=observer).first()
    expected_group = role_group_name(StaffRole.RECORDS_OFFICER)
    if (
        observer.email != "e2e-observer@example.org"
        or observer.first_name != "Synthetic"
        or observer.last_name != "Observer"
        or not observer.is_active
        or not observer.is_staff
        or observer.is_superuser
        or not observer.check_password(FIXTURE_OBSERVER_PASSWORD)
        or observer.user_permissions.exists()
        or observer.groups.filter(permissions__isnull=False).exists()
        or identity is None
        or identity.source != StaffIdentity.Source.LOCAL
        or identity.google_subject
        or identity.allow_next_google_link
        or set(observer.groups.values_list("name", flat=True))
        != {expected_group}
        or staff_roles(observer) != {StaffRole.RECORDS_OFFICER}
    ):
        raise CommandError(
            f"{FIXTURE_OBSERVER_USERNAME} exists but is not the designated "
            "fixture identity."
        )
    return observer


def _seed_role_user(*, username, password, role, role_name):
    User = get_user_model()
    email = f"{username}@example.org"
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name="Synthetic",
            last_name=role_name,
            password=password,
            is_active=True,
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.LOCAL,
        )
        apply_staff_roles(user, {role})
        return user

    identity = StaffIdentity.objects.filter(user=user).first()
    expected_group = role_group_name(role)
    if (
        user.email != email
        or user.first_name != "Synthetic"
        or user.last_name != role_name
        or not user.is_active
        or not user.is_staff
        or user.is_superuser
        or not user.check_password(password)
        or user.user_permissions.exists()
        or user.groups.filter(permissions__isnull=False).exists()
        or identity is None
        or identity.source != StaffIdentity.Source.LOCAL
        or identity.google_subject
        or identity.allow_next_google_link
        or set(user.groups.values_list("name", flat=True)) != {expected_group}
        or staff_roles(user) != {role}
    ):
        raise CommandError(
            f"{username} exists but is not the designated fixture identity."
        )
    return user


def _seed_role_users():
    return {
        StaffRole.LIAISON: _seed_role_user(
            username=FIXTURE_LIAISON_USERNAME,
            password=FIXTURE_LIAISON_PASSWORD,
            role=StaffRole.LIAISON,
            role_name="Liaison",
        ),
        StaffRole.REVIEWER: _seed_role_user(
            username=FIXTURE_REVIEWER_USERNAME,
            password=FIXTURE_REVIEWER_PASSWORD,
            role=StaffRole.REVIEWER,
            role_name="Reviewer",
        ),
        StaffRole.INTAKE_CLERK: _seed_role_user(
            username=FIXTURE_INTAKE_USERNAME,
            password=FIXTURE_INTAKE_PASSWORD,
            role=StaffRole.INTAKE_CLERK,
            role_name="Intake",
        ),
        StaffRole.ADMIN: _seed_role_user(
            username=FIXTURE_ADMIN_USERNAME,
            password=FIXTURE_ADMIN_PASSWORD,
            role=StaffRole.ADMIN,
            role_name="Admin",
        ),
    }


def _reconcile_fixture_inbound_work(request):
    for receipt_id in (
        _fixture_uuid("primary-receipt"),
        _fixture_uuid("dsn-receipt"),
    ):
        reconcile_inbound_receipt(receipt_id)
    inbound_work = WorkItem.objects.filter(
        request=request,
        source_type=WorkItem.SourceType.INBOUND_RECEIPT,
    )
    if inbound_work.count() != 2:
        raise CommandError(
            "The casework fixture must have two receipt-backed inbound work items."
        )
    if WorkItem.objects.filter(
        request=request,
        source_type=WorkItem.SourceType.CORRESPONDENCE,
    ).exists():
        raise CommandError(
            "The casework fixture may not create competing correspondence work items."
        )


def _assign_fixture_work(work_item, *, assignee, officer, key):
    if work_item.assignee_id == assignee.pk:
        return work_item
    if work_item.assignee_id is not None:
        raise CommandError("Fixture work has an unexpected assignee.")
    return assign_work_item(
        work_item,
        assignee=assignee,
        actor=officer,
        expected_version=work_item.version,
        idempotency_key=key,
    ).work_item


def _seed_role_work(request, *, officer, role_users):
    liaison = role_users[StaffRole.LIAISON]
    reviewer = role_users[StaffRole.REVIEWER]

    officer_work = WorkItem.objects.get(
        source_type=WorkItem.SourceType.INBOUND_RECEIPT,
        source_id=str(_fixture_uuid("primary-receipt")),
    )
    _assign_fixture_work(
        officer_work,
        assignee=officer,
        officer=officer,
        key="casework-e2e-officer-assignment",
    )

    task, created = CollectionTask.objects.get_or_create(
        request=request,
        instructions="Synthetic liaison collection assignment.",
        defaults={
            "kind": CollectionTask.Kind.MANUAL,
            "state": CollectionTask.State.PENDING,
            "assigned_to": liaison,
            "due_date": date(2026, 1, 9),
        },
    )
    if not created and (
        task.kind != CollectionTask.Kind.MANUAL
        or task.state != CollectionTask.State.PENDING
        or task.assigned_to_id != liaison.pk
        or task.due_date != date(2026, 1, 9)
    ):
        raise CommandError("The liaison collection fixture is inconsistent.")
    collection_work = reconcile_collection_task_work(task)
    if collection_work.assignee_id != liaison.pk:
        raise CommandError("The liaison collection work projection is inconsistent.")

    _seed_scope_conversation(request)

    officer_task, created = CollectionTask.objects.get_or_create(
        request=request,
        instructions="Synthetic officer collection assignment.",
        defaults={
            "kind": CollectionTask.Kind.MANUAL,
            "state": CollectionTask.State.PENDING,
            "assigned_to": officer,
            "due_date": date(2026, 1, 8),
        },
    )
    if not created and (
        officer_task.kind != CollectionTask.Kind.MANUAL
        or officer_task.state != CollectionTask.State.PENDING
        or officer_task.assigned_to_id != officer.pk
        or officer_task.due_date != date(2026, 1, 8)
    ):
        raise CommandError("The officer collection fixture is inconsistent.")
    officer_collection_work = reconcile_collection_task_work(officer_task)
    if officer_collection_work.assignee_id != officer.pk:
        raise CommandError("The officer collection work projection is inconsistent.")

    record, created = RecordItem.objects.get_or_create(
        pk=_fixture_uuid("review-record"),
        defaults={
            "request": request,
            "collection_task": task,
            "source_system": "fixture",
            "original_filename": "synthetic-review-record.pdf",
            "storage_key": "fixtures/synthetic-review-record.pdf",
            "content_length": 128,
            # The browser fixture intentionally has no object-store dependency.
            # It exercises metadata/classification while preview fails closed as
            # a non-previewable retained record instead of reaching S3.
            "content_type": "application/octet-stream",
            "sha256": hashlib.sha256(b"synthetic-review-record").hexdigest(),
        },
    )
    if not created and (
        record.request_id != request.pk
        or record.collection_task_id != task.pk
        or record.review_state != RecordItem.ReviewState.UNREVIEWED
        or record.storage_key != "fixtures/synthetic-review-record.pdf"
    ):
        raise CommandError("The reviewer record fixture is inconsistent.")
    review_work = reconcile_record_item_work(record)
    _assign_fixture_work(
        review_work,
        assignee=reviewer,
        officer=officer,
        key="casework-e2e-reviewer-assignment",
    )

    uat_record, created = RecordItem.objects.get_or_create(
        pk=_fixture_uuid("uat-review-record"),
        defaults={
            "request": request,
            "collection_task": task,
            "source_system": "fixture",
            "original_filename": "synthetic-uat-review-record.pdf",
            "storage_key": "fixtures/synthetic-uat-review-record.pdf",
            "content_length": 128,
            "content_type": "application/octet-stream",
            "sha256": hashlib.sha256(b"synthetic-uat-review-record").hexdigest(),
        },
    )
    if not created and (
        uat_record.request_id != request.pk
        or uat_record.collection_task_id != task.pk
        or uat_record.review_state != RecordItem.ReviewState.UNREVIEWED
        or uat_record.storage_key != "fixtures/synthetic-uat-review-record.pdf"
    ):
        raise CommandError("The UAT reviewer record fixture is inconsistent.")

    _seed_auto_reply_records(request, task)


#: The out-of-office replies a real collection accumulates. Each is a fact
#: about a message -- an ``Auto-Submitted`` header the tag reads -- and none of
#: them decides anything. The second one was found in two mailboxes, so it is
#: one record naming both custodians rather than two records to review twice.
_AUTO_REPLY_FIXTURES = (
    ("auto-reply-facilities", "facilities@psd401.net", ("facilities@psd401.net",)),
    (
        "auto-reply-transportation",
        "transportation@psd401.net",
        ("transportation@psd401.net", "operations@psd401.net"),
    ),
    ("auto-reply-principal", "principal@psd401.net", ("principal@psd401.net",)),
)


def _seed_auto_reply_records(request, task):
    """Records that carry the deterministic ``auto_reply`` tag.

    The browser fixture needs more than one taggable record so the review
    queue's filter, its facet counts, and one bulk human decision over a
    visible set are exercised against real rows. Nothing here reviews,
    releases, or communicates.
    """
    for label, sender, custodians in _AUTO_REPLY_FIXTURES:
        filename = f"synthetic-{label}.eml"
        record, created = RecordItem.objects.get_or_create(
            pk=_fixture_uuid(label),
            defaults={
                "request": request,
                "collection_task": task,
                "source_system": "fixture",
                "custodian": custodians[0],
                "original_filename": filename,
                "storage_key": f"fixtures/{filename}",
                "content_length": 128,
                "content_type": "application/octet-stream",
                "sha256": hashlib.sha256(label.encode("utf-8")).hexdigest(),
                "email_message_id": f"<{label}@psd401.net>",
                "email_body_sha256": hashlib.sha256(
                    f"{label}-body".encode()
                ).hexdigest(),
                "email_from": sender,
                "email_sender_domain": "psd401.net",
                "email_auto_submitted": "auto-replied",
                "email_sent_at": _BASE_TIME,
            },
        )
        if not created and (
            record.request_id != request.pk
            or record.review_state != RecordItem.ReviewState.UNREVIEWED
            or record.email_auto_submitted != "auto-replied"
        ):
            raise CommandError(
                f"The {label} record fixture is inconsistent."
            )
        RecordItemCustodian.objects.bulk_create(
            [
                RecordItemCustodian(
                    record_item=record,
                    custodian=custodian,
                    collection_task=task,
                )
                for custodian in custodians
            ],
            ignore_conflicts=True,
        )
        # Deliberately not reconciled into the work queue. These rows exist so
        # the records tab has a taggable set to filter and decide over; the
        # fixture's work-queue shape is asserted elsewhere and is not what this
        # is testing.


def _system_transport_identities():
    User = get_user_model()
    outbound = User.objects.filter(
        username=SYSTEM_OUTBOUND_TRANSPORT_USERNAME
    ).first()
    inbound = User.objects.filter(
        username=SYSTEM_INBOUND_TRANSPORT_USERNAME
    ).first()
    if not is_exact_outbound_identity(outbound):
        raise CommandError(
            "The exact system outbound transport identity is unavailable."
        )
    if not is_exact_inbound_identity(inbound):
        raise CommandError(
            "The exact system inbound transport identity is unavailable."
        )
    return outbound, inbound


def _seed_case(officer):
    requester_created_at = _BASE_TIME - timedelta(days=1)
    requester, requester_created = Requester.objects.get_or_create(
        id=_fixture_uuid("requester"),
        defaults={
            "name": "Synthetic Requester",
            "email": "synthetic.requester@example.org",
        },
    )
    if requester_created:
        models.QuerySet(
            model=Requester,
            using=requester._state.db,
        ).filter(pk=requester.pk).update(created_at=requester_created_at)
        requester.created_at = requester_created_at
    expected_requester = {
        "name": "Synthetic Requester",
        "email": "synthetic.requester@example.org",
        "phone": "",
        "mailing_address": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "company": "",
        "created_at": requester_created_at,
    }
    if not requester_created and any(
        getattr(requester, field) != value
        for field, value in expected_requester.items()
    ):
        raise CommandError("The designated fixture requester identity is inconsistent.")
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("request"),
            "requester": requester,
            "description": FIXTURE_DESCRIPTION,
            "channel": Channel.EMAIL,
            "status": RequestStatus.CLARIFICATION,
            "received_at": _BASE_TIME,
            "statutory_response_due": date(2026, 1, 12),
            "assigned_officer": officer,
        },
    )
    expected = {
        "id": _fixture_uuid("request"),
        "requester_id": requester.pk,
        "description": FIXTURE_DESCRIPTION,
        "channel": Channel.EMAIL,
        "status": RequestStatus.CLARIFICATION,
        "received_at": _BASE_TIME,
        "statutory_response_due": date(2026, 1, 12),
        "estimated_completion": None,
        "closed_at": None,
        "assigned_officer_id": officer.pk,
        "tolled_business_days": 0,
        "acknowledgment_sent_at": None,
    }
    if not created and any(
        getattr(request, field) != value for field, value in expected.items()
    ):
        raise CommandError(
            f"{FIXTURE_REQUEST_NUMBER} exists but is not the designated fixture case."
        )
    return request


def _seed_awaiting_statutory_response_case(officer):
    """Seed one received case with no delivered five-business-day response."""
    requester, _ = Requester.objects.get_or_create(
        email="awaiting@example.org",
        defaults={
            "id": _fixture_uuid("awaiting-requester"),
            "name": "Awaiting Requester",
        },
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_AWAITING_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("awaiting-request"),
            "requester": requester,
            "description": FIXTURE_AWAITING_DESCRIPTION,
            "channel": Channel.PORTAL,
            "status": RequestStatus.RECEIVED,
            "received_at": _BASE_TIME,
            "statutory_response_due": FIXTURE_AWAITING_DUE_DATE,
            "assigned_officer": officer,
        },
    )
    expected = {
        "status": RequestStatus.RECEIVED,
        "statutory_response_due": FIXTURE_AWAITING_DUE_DATE,
        "acknowledgment_sent_at": None,
    }
    if not created and any(
        getattr(request, field) != value for field, value in expected.items()
    ):
        raise CommandError(
            f"{FIXTURE_AWAITING_REQUEST_NUMBER} exists but is no longer the "
            "designated awaiting-statutory-response fixture case."
        )
    if request.initial_response_delivered:
        raise CommandError(
            f"{FIXTURE_AWAITING_REQUEST_NUMBER} must have no delivered "
            "initial response."
        )
    work_item = reconcile_request_work(request)
    if work_item is None or work_item.disposition not in {
        WorkItem.Disposition.NEEDS_ATTENTION,
        WorkItem.Disposition.WAITING,
    }:
        raise CommandError(
            "The awaiting-statutory-response fixture case must hold one open "
            "request-action work item."
        )
    return request


def _seed_supersede_case(officer):
    """Seed one case with a completed Vault collection and unreviewed mail.

    Records are deliberately not reconciled into the work queue; the fixture's
    work-queue shape is asserted elsewhere and is not what this case proves.
    """
    requester, _ = Requester.objects.get_or_create(
        email="survey@example.org",
        defaults={
            "id": _fixture_uuid("supersede-requester"),
            "name": "Survey Requester",
        },
    )
    request, _ = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_SUPERSEDE_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("supersede-request"),
            "requester": requester,
            "description": FIXTURE_SUPERSEDE_DESCRIPTION,
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    task, _ = CollectionTask.objects.get_or_create(
        request=request,
        kind=CollectionTask.Kind.VAULT_MAIL,
        defaults={
            "state": CollectionTask.State.COMPLETE,
            "completed_at": _BASE_TIME,
            "instructions": "Create the confirmed Google Vault Gmail export.",
            "search_terms": '"facilities survey"',
        },
    )
    for index in range(FIXTURE_SUPERSEDE_RECORD_COUNT):
        label = f"supersede-newsletter-{index}"
        filename = f"synthetic-{label}.eml"
        RecordItem.objects.get_or_create(
            pk=_fixture_uuid(label),
            defaults={
                "request": request,
                "collection_task": task,
                "source_system": "fixture",
                "original_filename": filename,
                "storage_key": f"fixtures/{filename}",
                "content_length": 128,
                "content_type": "application/octet-stream",
                "sha256": hashlib.sha256(label.encode("utf-8")).hexdigest(),
                "email_sender_domain": "example.org",
            },
        )
    for index in range(FIXTURE_BULK_FILTER_RECORD_COUNT):
        label = f"bulk-filter-{index}"
        filename = f"synthetic-{label}.eml"
        RecordItem.objects.get_or_create(
            pk=_fixture_uuid(label),
            defaults={
                "request": request,
                "source_system": "upload",
                "original_filename": filename,
                "storage_key": f"fixtures/{filename}",
                "content_length": 128,
                "content_type": "application/octet-stream",
                "sha256": hashlib.sha256(label.encode("utf-8")).hexdigest(),
                "email_sender_domain": FIXTURE_BULK_FILTER_DOMAIN,
            },
        )
    return request


def _seed_estimate_letter_case(officer):
    """Seed a case with a system-prepared interpretation-and-estimate draft.

    AI is off in acceptance runs, so the intent summary and the estimate are
    written rather than computed; the draft itself goes through the real
    drafting service, exactly as the hourly task would call it.
    """
    from apps.correspondence.services import create_interpretation_estimate_draft

    requester, _ = Requester.objects.get_or_create(
        email="bus-video@example.org",
        defaults={"id": _fixture_uuid("estimate-letter-requester"), "name": "Bus Video Requester"},
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_ESTIMATE_LETTER_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("estimate-letter-request"),
            "requester": requester,
            "description": "Any bus video from route 12 on March 3, 2026.",
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    if not created:
        return request
    AdvisoryRun.objects.create(
        kind=AdvisoryRun.Kind.REQUEST_INTENT,
        request=request,
        model_identifier="synthetic-intent-model",
        input_sha256=hashlib.sha256(b"estimate-letter-intent").hexdigest(),
        state=AdvisoryRun.State.COMPLETE,
        requested_by=officer,
        proposal={"summary": FIXTURE_ESTIMATE_LETTER_INTENT},
    )

    def fee(total, files):
        return {"files": files, "lines": [], "total": total, "deposit": "0.00",
                "flat_fee_available": False, "below_waiver_floor": False}

    estimate = RecordsEstimate.objects.create(
        request=request,
        as_of=_BASE_TIME.date(),
        inputs={"synthetic": True},
        result={
            "responsive": {"low": 2, "expected": 3, "high": 5,
                           "reviewed_responsive": 1, "unreviewed": 4},
            "fee": {"low": fee("0.10", 2), "expected": fee("0.15", 3), "high": fee("0.25", 5)},
            "completion_date": (_BASE_TIME + timedelta(days=21)).date().isoformat(),
            "notes": [],
        },
    )
    create_interpretation_estimate_draft(request, estimate=estimate)
    return request


def _seed_scoping_session(officer, request):
    """A finished two-count scoping session (#36), written rather than run:
    Vault and AI are off in acceptance runs. Counts only."""
    from apps.records.models import ScopeIteration, ScopingSession

    session = ScopingSession.objects.create(
        request=request,
        trigger=ScopingSession.Trigger.AUTOMATIC,
        state=ScopingSession.State.COMPLETE,
        max_iterations=4,
        stop_reason="The scope is now specific to enrollment planning mail.",
        completed_at=_BASE_TIME,
    )
    steps = (
        (1, '"enrollment plan" AND ("spring" OR "projection")', "entire_org", [], 2340,
         [{"email": "communications@psd401.net", "count": 1510}, {"email": "enrollment@psd401.net", "count": 610}],
         "The request asks about spring enrollment planning."),
        (2, '"enrollment plan" AND ("spring" OR "projection") AND -from:newsletter.example',
         "accounts", ["enrollment@psd401.net"], 412,
         [{"email": "enrollment@psd401.net", "count": 412}],
         ("Most matches were the district newsletter in the communications mailbox; "
         "count the enrollment office and leave the newsletter out.")),
    )
    for number, terms, scope, custodians, total, accounts, reason in steps:
        run = VaultCountRun.objects.create(
            request=request,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
            state=VaultCountRun.State.COMPLETE,
            search_scope=scope,
            terms=terms,
            custodian_emails=custodians,
            input_sha256=hashlib.sha256(terms.encode()).hexdigest(),
            vault_matter_id=f"e2e-matter-{request.number}",
            total_count=total,
            account_counts=accounts,
            completed_at=_BASE_TIME,
        )
        ScopeIteration.objects.create(
            session=session, number=number, count_run=run, reason=reason,
            custodians=custodians,
        )
    AdvisoryRun.objects.create(
        kind=AdvisoryRun.Kind.REQUEST_INTENT,
        request=request,
        model_identifier="synthetic-intent-model",
        input_sha256=hashlib.sha256(b"trust-intent").hexdigest(),
        state=AdvisoryRun.State.COMPLETE,
        requested_by=officer,
        proposal={"summary": "Spring enrollment planning mail.",
                  "required_search_terms": ["enrollment plan"], "search_terms": ["spring"]},
    )


def _seed_rescope_case(officer):
    """A finished Vault collection from a broad count, two unreviewed records
    from it, and a newer officer re-run whose final count is narrower."""
    from apps.records.models import ScopeIteration, ScopingSession

    requester, _ = Requester.objects.get_or_create(
        email="rescope@example.org",
        defaults={"id": _fixture_uuid("rescope-requester"), "name": "Rescope Requester"},
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_RESCOPE_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("rescope-request"),
            "requester": requester,
            "description": "Board budget workshop email, February 2026.",
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    if not created:
        return request

    def count(terms, scope, custodians, total):
        return VaultCountRun.objects.create(
            request=request,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
            state=VaultCountRun.State.COMPLETE,
            search_scope=scope,
            terms=terms,
            custodian_emails=custodians,
            input_sha256=hashlib.sha256(terms.encode()).hexdigest(),
            vault_matter_id=f"e2e-matter-{request.number}",
            total_count=total,
            completed_at=_BASE_TIME,
        )

    broad = count('"budget workshop"', "entire_org", [], 2340)
    task = CollectionTask.objects.create(
        request=request,
        kind=CollectionTask.Kind.VAULT_MAIL,
        state=CollectionTask.State.COMPLETE,
        source_vault_count=broad,
        completed_at=_BASE_TIME,
        instructions="Create the confirmed Google Vault Gmail export.",
        search_terms=broad.terms,
    )
    for index in range(2):
        label = f"rescope-mail-{index}"
        RecordItem.objects.create(
            pk=_fixture_uuid(label),
            request=request,
            collection_task=task,
            source_system="fixture",
            original_filename=f"synthetic-{label}.eml",
            storage_key=f"fixtures/synthetic-{label}.eml",
            content_length=128,
            content_type="application/octet-stream",
            sha256=hashlib.sha256(label.encode()).hexdigest(),
        )
    session = ScopingSession.objects.create(
        request=request,
        trigger=ScopingSession.Trigger.MANUAL,
        actor=officer,
        reason="The broad search pulled in the district newsletter.",
        state=ScopingSession.State.COMPLETE,
        max_iterations=4,
        stop_reason="The scope is now the board office.",
        completed_at=_BASE_TIME,
    )
    narrow = count(
        '"budget workshop" AND -from:newsletter.example', "accounts", ["board@psd401.net"], 412,
    )
    ScopeIteration.objects.create(
        session=session, number=1, count_run=narrow,
        reason="Count the board office and leave the newsletter out.",
        custodians=["board@psd401.net"],
    )
    return request


def _seed_trust_ladder_case(officer):
    """Seed the case the sampled-batch and elusion flows decide.

    AI is off in acceptance runs, so the suggestions are written as completed
    runs. Nothing here reviews a record; the flows do, in a reviewer's name.
    """
    requester, _ = Requester.objects.get_or_create(
        email="trust-ladder@example.org",
        defaults={"id": _fixture_uuid("trust-requester"), "name": "Trust Ladder Requester"},
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_TRUST_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("trust-request"),
            "requester": requester,
            "description": "Email about the spring enrollment plan, March 2026.",
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    if not created:
        return request
    _seed_scoping_session(officer, request)
    sets = (
        ("trust-newsletter", FIXTURE_TRUST_CLUSTER_DOMAIN, FIXTURE_TRUST_CLUSTER_COUNT, 0.6),
        ("trust-ad", FIXTURE_TRUST_DISCARD_DOMAIN, FIXTURE_TRUST_DISCARD_COUNT, 0.97),
    )
    for prefix, domain, count, confidence in sets:
        for index in range(count):
            label = f"{prefix}-{index}"
            item = RecordItem.objects.create(
                pk=_fixture_uuid(label),
                request=request,
                source_system="fixture",
                original_filename=f"synthetic-{label}.eml",
                storage_key=f"fixtures/synthetic-{label}.eml",
                content_length=128,
                content_type="application/octet-stream",
                sha256=hashlib.sha256(label.encode()).hexdigest(),
                email_message_id=f"<{label}@{domain}>",
                email_from=f"news@{domain}",
                email_sender_domain=domain,
                email_sent_at=_BASE_TIME,
            )
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                request=request,
                record_item=item,
                model_identifier="synthetic-review-model",
                input_sha256=hashlib.sha256(f"{label}-run".encode()).hexdigest(),
                state=AdvisoryRun.State.COMPLETE,
                requested_by=officer,
                proposal={"suggested_state": "non_responsive", "confidence": confidence,
                          "matched_terms": [], "rationale": "Synthetic newsletter."},
            )
    return request


def _seed_redaction_proposal_case(officer):
    """Seed responsive PDFs with pending phone-number redaction proposals.

    The PDFs are real and stored in the e2e filesystem object store, and each
    proposal's advisory run carries the input fingerprint the live builder
    computes, so accepting one goes through the real create_redaction.
    """
    import io

    import pymupdf as fitz
    from django.conf import settings as django_settings

    # The PDFs need somewhere to live. Only the browser run has the local
    # store; the fixture's own unit tests seed without this case.
    if not getattr(django_settings, "PRR_E2E_FILESYSTEM_OBJECT_STORE", ""):
        return None

    from apps.records.advisory_ai import advisory_input_sha256
    from apps.records.models import ExemptionCitation, RedactionProposal
    from apps.records.storage import generated_record_storage_key
    from apps.secure_files import _object_store_client

    requester, _ = Requester.objects.get_or_create(
        email="staff-phones@example.org",
        defaults={"id": _fixture_uuid("redaction-requester"), "name": "Staff Phones Requester"},
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_REDACTION_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("redaction-request"),
            "requester": requester,
            "description": "Coaching staff contact lists for spring 2026.",
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    if not created:
        return request
    exemption, _ = ExemptionCitation.objects.get_or_create(
        citation="RCW 42.56.250(4)",
        defaults={"short_name": "Employee personal contact information"},
    )
    client = _object_store_client()
    for index in range(FIXTURE_REDACTION_RECORD_COUNT):
        label = f"redaction-roster-{index}"
        document = fitz.open()
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 96), f"Coach roster {index}: cell 555-01{index:02d}.", fontsize=12)
        pdf = document.tobytes()
        document.close()
        # A fresh generated key per seed, as ingest would make, so a store
        # left over from an earlier run never collides.
        key = generated_record_storage_key(request.pk, original_filename=f"{label}.pdf")
        client.put_object(
            Bucket=django_settings.OBJECT_STORE["bucket"], Key=key,
            Body=io.BytesIO(pdf), IfNoneMatch="*",
        )
        item = RecordItem.objects.create(
            pk=_fixture_uuid(label),
            request=request,
            source_system="fixture",
            original_filename=f"synthetic-{label}.pdf",
            storage_key=key,
            content_length=len(pdf),
            content_type="application/pdf",
            sha256=hashlib.sha256(pdf).hexdigest(),
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=officer,
            reviewed_at=_BASE_TIME,
        )
        run = AdvisoryRun(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            request=request,
            record_item=item,
            model_identifier="synthetic-redaction-model",
            input_sha256="",
            state=AdvisoryRun.State.COMPLETE,
            requested_by=officer,
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save()
        RedactionProposal.objects.create(
            advisory_run=run,
            record_item=item,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.5, y1=0.15,
            exemption=exemption,
            pattern=RedactionProposal.Pattern.PHONE,
            confidence=0.93,
            rationale="An employee's personal cell number.",
            source_span_ids=["p1s1"],
            source_text_sha256=hashlib.sha256(label.encode()).hexdigest(),
        )
    return request


def _seed_review_flight_case(officer):
    """Seed unreviewed PDFs with written AI suggestions for keyboard review.

    Seeded only when the e2e filesystem store exists, like the redaction
    case; nothing here reviews a record.
    """
    import io

    import pymupdf as fitz
    from django.conf import settings as django_settings

    if not getattr(django_settings, "PRR_E2E_FILESYSTEM_OBJECT_STORE", ""):
        return None

    from apps.records.storage import generated_record_storage_key
    from apps.secure_files import _object_store_client

    requester, _ = Requester.objects.get_or_create(
        email="bus-route@example.org",
        defaults={"id": _fixture_uuid("flight-requester"), "name": "Bus Route Requester"},
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_FLIGHT_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("flight-request"),
            "requester": requester,
            "description": "Records about route 12 in March 2026.",
            "channel": Channel.PORTAL,
            "status": RequestStatus.ACKNOWLEDGED,
            "received_at": _BASE_TIME,
            "assigned_officer": officer,
        },
    )
    if not created:
        return request
    client = _object_store_client()
    for label, text, suggested, confidence in FIXTURE_FLIGHT_RECORDS:
        document = fitz.open()
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 96), text, fontsize=12)
        pdf = document.tobytes()
        document.close()
        key = generated_record_storage_key(request.pk, original_filename=f"{label}.pdf")
        client.put_object(
            Bucket=django_settings.OBJECT_STORE["bucket"], Key=key,
            Body=io.BytesIO(pdf), IfNoneMatch="*",
        )
        item = RecordItem.objects.create(
            pk=_fixture_uuid(label),
            request=request,
            source_system="fixture",
            original_filename=f"synthetic-{label}.pdf",
            storage_key=key,
            content_length=len(pdf),
            content_type="application/pdf",
            sha256=hashlib.sha256(pdf).hexdigest(),
        )
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=request,
            record_item=item,
            model_identifier="synthetic-review-model",
            input_sha256=hashlib.sha256(f"{label}-run".encode()).hexdigest(),
            state=AdvisoryRun.State.COMPLETE,
            requested_by=officer,
            proposal={"suggested_state": suggested, "confidence": confidence,
                      "matched_terms": [], "rationale": f"Synthetic: {text}"},
        )
    return request


def _seed_evaluation_run(officer, request):
    """One synthetic model-comparison run for the AI evaluation screen.

    AI is off in acceptance runs, so the metrics are written, not measured.
    The set references a fixture case and carries no text.
    """
    evaluation_set, _ = EvaluationSet.objects.get_or_create(
        pk=_fixture_uuid("evaluation-set"),
        defaults={
            "kind": EvaluationSet.Kind.INTAKE,
            "examples": [
                {"request_id": str(request.pk), "label": "new", "target_case_number": None},
            ],
            "label_counts": {"new": 1},
            "created_by": officer,
        },
    )
    result = {
        "items": 1, "errors": 0, "safety_recall": 1.0, "target_accuracy": None,
        "confusion": {"new": {"new": 1}}, "input_tokens_mean": 1200,
        "output_tokens_mean": 80, "latency_p50_ms": 900, "latency_p95_ms": 900,
        "predictions": [],
    }
    EvaluationRun.objects.get_or_create(
        pk=_fixture_uuid("evaluation-run"),
        defaults={
            "evaluation_set": evaluation_set,
            "model_identifiers": ["synthetic-model-a", "synthetic-model-b"],
            "results": {
                "synthetic-model-a": {**result, "accuracy": 1.0, "cost_per_item_usd": 0.0048},
                "synthetic-model-b": {**result, "accuracy": 1.0, "cost_per_item_usd": None},
            },
            "created_by": officer,
        },
    )


def _seed_knowledge_pack():
    """Seed what the knowledge-pack flow edits: one registry system with no
    search notes yet and one open-ended role account. Neither touches a case
    or the work queue."""
    RecordSystem.objects.get_or_create(
        slug=FIXTURE_KNOWLEDGE_SYSTEM_SLUG,
        defaults={
            "name": "Fixture shared mailbox",
            "category": "Email & documents",
            "custodian_title": "Fixture office manager",
            "triage_keywords": [],
        },
    )
    KnowledgeRoleAccount.objects.get_or_create(
        role=FIXTURE_KNOWLEDGE_ROLE,
        defaults={
            "account_email": "fixture.principal@example.org",
            "starts_on": date(2024, 7, 1),
        },
    )


def _seed_forwarded_requester_case(officer):
    """Seed a case whose recorded requester is the mailbox that forwarded it.

    This is the real failure the correction exists for: the acknowledgment
    would greet the info mailbox and be delivered to it, while the person who
    actually asked -- and whose five-business-day clock is running -- never
    hears from the district.
    """
    requester, _ = Requester.objects.get_or_create(
        email=FIXTURE_FORWARDED_REQUESTER_EMAIL,
        defaults={
            "id": _fixture_uuid("forwarded-requester"),
            "name": FIXTURE_FORWARDED_REQUESTER_NAME,
        },
    )
    request, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_FORWARDED_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("forwarded-request"),
            "requester": requester,
            "description": FIXTURE_FORWARDED_DESCRIPTION,
            "channel": Channel.EMAIL,
            "status": RequestStatus.RECEIVED,
            "received_at": _BASE_TIME,
            "statutory_response_due": FIXTURE_FORWARDED_DUE_DATE,
            "assigned_officer": officer,
        },
    )
    if not created and request.status != RequestStatus.RECEIVED:
        raise CommandError(
            f"{FIXTURE_FORWARDED_REQUEST_NUMBER} exists but is no longer the "
            "designated forwarded-requester fixture case."
        )
    reconcile_request_work(request)
    return request


def _seed_email_intake_redirect_cases(officer, inbound_transport):
    """Seed the pair a redirect needs: one auto-created case, one real case.

    Kept entirely separate from the primary fixture case so moving a message
    between these two changes nothing another browser flow counts on.
    """
    target_requester, _ = Requester.objects.get_or_create(
        email="existing-case@example.org",
        defaults={
            "id": _fixture_uuid("redirect-target-requester"),
            "name": "Existing Case Requester",
        },
    )
    target, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_REDIRECT_TARGET_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("redirect-target-request"),
            "requester": target_requester,
            "description": FIXTURE_REDIRECT_TARGET_DESCRIPTION,
            "channel": Channel.PORTAL,
            "status": RequestStatus.GATHERING,
            "received_at": _BASE_TIME,
            "statutory_response_due": FIXTURE_AWAITING_DUE_DATE,
            "assigned_officer": officer,
        },
    )
    if not created and target.status != RequestStatus.GATHERING:
        raise CommandError(
            f"{FIXTURE_REDIRECT_TARGET_REQUEST_NUMBER} exists but is no longer "
            "the designated open destination case."
        )

    intake_requester, _ = Requester.objects.get_or_create(
        email="follow-up@example.org",
        defaults={
            "id": _fixture_uuid("email-intake-requester"),
            "name": "Follow Up Requester",
        },
    )
    source, created = PublicRecordsRequest.objects.get_or_create(
        number=FIXTURE_EMAIL_INTAKE_REQUEST_NUMBER,
        defaults={
            "id": _fixture_uuid("email-intake-request"),
            "requester": intake_requester,
            "description": FIXTURE_EMAIL_INTAKE_DESCRIPTION,
            "channel": Channel.EMAIL,
            "status": RequestStatus.RECEIVED,
            "received_at": _BASE_TIME,
            "statutory_response_due": FIXTURE_AWAITING_DUE_DATE,
        },
    )
    if not created and source.status != RequestStatus.RECEIVED:
        raise CommandError(
            f"{FIXTURE_EMAIL_INTAKE_REQUEST_NUMBER} exists but is no longer "
            "the designated auto-created email case."
        )
    if InboundCorrespondenceReceipt.objects.filter(
        pk=_fixture_uuid("email-intake-receipt"),
    ).exists():
        return source, target

    # Deliberately older than every message on the primary fixture case. Work
    # queues and message lists are newest-first, and a dozen browser flows
    # assert on the *first* row; a message seeded "now" would silently take
    # that position away from the row those flows are actually about.
    received_at = _BASE_TIME - timedelta(days=2)
    message = Correspondence.objects.create(
        id=_fixture_uuid("email-intake-correspondence"),
        request=source,
        direction=Correspondence.Direction.INBOUND,
        kind=Correspondence.Kind.GENERAL,
        status=Correspondence.Status.RECEIVED,
        received_subject=FIXTURE_EMAIL_INTAKE_SUBJECT,
        received_body=FIXTURE_EMAIL_INTAKE_DESCRIPTION,
        received_sender="Follow Up Requester <follow-up@example.org>",
        received_recipients=["publicrecords@example.org"],
        received_headers={"x-fixture-case": "email-intake"},
        received_at=received_at,
        matched_at=received_at + timedelta(seconds=2),
        provider="gmail",
        provider_account="default",
        provider_message_id="fixture-email-intake-message",
        provider_thread_id="fixture-email-intake-thread",
        reply_token=_fixture_uuid("email-intake-reply-token"),
        created_by=inbound_transport,
        version=2,
    )
    # created_at is auto_now_add, so the ordering the queues actually use has
    # to be restored after the insert, exactly as the primary case does it.
    _restore_created_at(
        [message],
        [received_at + timedelta(seconds=1)],
        using=message._state.db,
    )
    message.refresh_from_db()
    raw_hash = hashlib.sha256(b"synthetic email intake message").hexdigest()
    InboundCorrespondenceReceipt.objects.create(
        id=_fixture_uuid("email-intake-receipt"),
        provider=message.provider,
        provider_account=message.provider_account,
        provider_message_id=message.provider_message_id,
        provider_thread_id=message.provider_thread_id,
        received_at=received_at,
        envelope_sender=message.received_sender,
        envelope_recipients=message.received_recipients,
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
        matched_request=source,
        matched_correspondence=message,
        matched_at=message.matched_at,
        quarantined=False,
        raw_message_size=31,
        raw_message_sha256=raw_hash,
        raw_storage_key=(
            "quarantine/correspondence/raw/"
            f"{_fixture_uuid('email-intake-receipt-storage').hex}"
        ),
        payload_sha256=_payload_digest(
            raw_sha256=raw_hash,
            provider=message.provider,
            provider_account=message.provider_account,
            provider_message_id=message.provider_message_id,
            provider_thread_id=message.provider_thread_id,
            envelope_sender=message.received_sender,
            envelope_recipients=message.received_recipients,
            received_at=received_at,
        ),
        idempotency_key="casework-e2e-email-intake-receipt",
        submitted_by=inbound_transport,
        version=2,
        completed_at=received_at + timedelta(seconds=1),
    )
    work_item = reconcile_inbound_receipt(
        _fixture_uuid("email-intake-receipt"),
    ).work_item
    if work_item is None or work_item.request_id != source.pk:
        raise CommandError(
            "The auto-created email case must hold one receipt-backed work item."
        )
    return source, target


def _restore_created_at(objects, desired_values, *, using):
    if not objects:
        return
    for item, created_at in zip(objects, desired_values, strict=True):
        item.created_at = created_at
    models.QuerySet(
        model=type(objects[0]),
        using=using,
    ).bulk_update(objects, ["created_at"], batch_size=500)


def _seed_related_evidence(
    request,
    officer,
    outbound_transport,
    inbound_transport,
    messages,
):
    message_indexes = {message.pk: index for index, message in enumerate(messages)}
    outbound = [
        message
        for message in messages
        if message.direction == Correspondence.Direction.OUTBOUND
    ]
    recipients = [
        CorrespondenceRecipient(
            id=_fixture_uuid(f"recipient-{message_indexes[message.pk]}"),
            correspondence=message,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="synthetic.requester@example.org",
            display_name="Synthetic Requester",
            requester=request.requester,
            created_at=message.created_at + timedelta(seconds=1),
        )
        for message in outbound
    ]
    # The production manager freezes recipient snapshots once a message leaves
    # draft. This test-only command is constructing the already-finalized graph,
    # so insert the validated deterministic snapshots without replaying sends.
    recipient_created_at = [recipient.created_at for recipient in recipients]
    models.QuerySet(
        model=CorrespondenceRecipient,
        using=request._state.db,
    ).bulk_create(recipients, batch_size=500)
    _restore_created_at(
        recipients,
        recipient_created_at,
        using=request._state.db,
    )
    recipient_by_correspondence = {
        recipient.correspondence_id: recipient for recipient in recipients
    }

    sent = next(message for message in messages if message.status == "sent")
    failed = next(message for message in messages if message.status == "failed")
    sent_delivery = CorrespondenceDelivery(
        id=_fixture_uuid("sent-delivery"),
        correspondence=sent,
        status=CorrespondenceDelivery.Status.SENT,
        provider=sent.provider,
        provider_account=sent.provider_account,
        idempotency_key=f"correspondence-delivery:{sent.pk}",
        attempt_count=1,
        successful_attempt_number=1,
        provider_message_id=sent.provider_message_id,
        provider_thread_id=sent.provider_thread_id,
        queued_at=sent.queued_at,
        completed_at=sent.sent_at,
        recovery_not_before=(
            sent.queued_at
            + timedelta(seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"))
        ),
        version=3,
    )
    failed_delivery = CorrespondenceDelivery(
        id=_fixture_uuid("failed-delivery"),
        correspondence=failed,
        status=CorrespondenceDelivery.Status.FAILED,
        provider=failed.provider,
        provider_account=failed.provider_account,
        idempotency_key=f"correspondence-delivery:{failed.pk}",
        attempt_count=1,
        last_failure_category=_FAILURE_CATEGORY,
        last_failure_description=_FAILURE_DESCRIPTION,
        queued_at=failed.queued_at,
        completed_at=failed.failed_at,
        recovery_not_before=(
            failed.queued_at
            + timedelta(seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"))
        ),
        version=3,
    )
    CorrespondenceDelivery.objects.bulk_create([sent_delivery, failed_delivery])

    attempts = [
        CorrespondenceDeliveryAttempt(
            id=_fixture_uuid("sent-attempt"),
            delivery=sent_delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(outbound_transport.pk),
            claimed_at=sent.queued_at,
            result_at=sent.sent_at,
            result_category=(
                CorrespondenceDeliveryAttempt.ResultCategory.SENT
            ),
            provider_response_metadata=_SENT_PROVIDER_METADATA,
            provider_message_id=sent.provider_message_id,
            provider_thread_id=sent.provider_thread_id,
            provider_timestamp=sent.sent_at,
            result_fingerprint=_result_fingerprint(
                outcome=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                provider_message_id=sent.provider_message_id,
                provider_thread_id=sent.provider_thread_id,
                provider_timestamp=sent.sent_at,
                failure_category="",
                provider_metadata=_SENT_PROVIDER_METADATA,
            ),
            actor=outbound_transport,
            created_at=sent.queued_at,
        ),
        CorrespondenceDeliveryAttempt(
            id=_fixture_uuid("failed-attempt"),
            delivery=failed_delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(outbound_transport.pk),
            claimed_at=failed.queued_at,
            result_at=failed.failed_at,
            result_category=(
                CorrespondenceDeliveryAttempt.ResultCategory.FAILED
            ),
            provider_response_metadata=_FAILED_PROVIDER_METADATA,
            error_category=_FAILURE_CATEGORY,
            result_fingerprint=_result_fingerprint(
                outcome=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                provider_message_id="",
                provider_thread_id="",
                provider_timestamp=None,
                failure_category=_FAILURE_CATEGORY,
                provider_metadata=_FAILED_PROVIDER_METADATA,
            ),
            actor=outbound_transport,
            created_at=failed.queued_at,
        ),
    ]
    # ``raw=True`` preserves the historical auto_now_add timestamp on insert;
    # completed delivery attempts remain append-only after creation.
    for attempt in attempts:
        attempt.save_base(
            raw=True,
            force_insert=True,
            using=request._state.db,
        )

    draft_messages = [
        message for message in messages
        if message.status == Correspondence.Status.DRAFT
    ][:8]
    attachments = [
        CorrespondenceAttachment(
            id=_fixture_uuid(f"attachment-{index}"),
            correspondence=message,
            original_filename=f"fixture-attachment-{index}.pdf",
            content_type="application/pdf",
            size=1_024 + index,
            sha256=hashlib.sha256(f"fixture-{index}".encode()).hexdigest(),
            storage_key=(
                "quarantine/correspondence/attachments/"
                f"{_fixture_uuid(f'attachment-storage-{index}').hex}"
            ),
            uploaded_by=officer,
            created_at=message.created_at + timedelta(seconds=1),
        )
        for index, message in enumerate(draft_messages, start=1)
    ]
    attachment_created_at = [attachment.created_at for attachment in attachments]
    models.QuerySet(
        model=CorrespondenceAttachment,
        using=request._state.db,
    ).bulk_create(attachments)
    _restore_created_at(
        attachments,
        attachment_created_at,
        using=request._state.db,
    )

    receipt_hash = hashlib.sha256(b"synthetic raw message").hexdigest()
    primary_message = next(
        message for message in messages
        if message.pk == _fixture_uuid("correspondence-0")
    )
    dsn_message = next(
        message for message in messages
        if message.kind == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
    )
    primary_received_at = primary_message.received_at
    primary_matched_at = primary_message.matched_at
    primary_payload_hash = _payload_digest(
        raw_sha256=receipt_hash,
        provider=primary_message.provider,
        provider_account=primary_message.provider_account,
        provider_message_id=primary_message.provider_message_id,
        provider_thread_id=primary_message.provider_thread_id,
        envelope_sender=primary_message.received_sender,
        envelope_recipients=primary_message.received_recipients,
        received_at=primary_received_at,
    )
    primary_receipt = InboundCorrespondenceReceipt(
        id=_fixture_uuid("primary-receipt"),
        provider=primary_message.provider,
        provider_account=primary_message.provider_account,
        provider_message_id=primary_message.provider_message_id,
        provider_thread_id=primary_message.provider_thread_id,
        received_at=primary_received_at,
        envelope_sender=primary_message.received_sender,
        envelope_recipients=primary_message.received_recipients,
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        matched_request=request,
        matched_correspondence=primary_message,
        matched_at=primary_matched_at,
        quarantined=False,
        raw_message_size=21,
        raw_message_sha256=receipt_hash,
        raw_storage_key=(
            "quarantine/correspondence/raw/"
            f"{_fixture_uuid('primary-receipt-storage').hex}"
        ),
        payload_sha256=primary_payload_hash,
        idempotency_key="casework-e2e-primary-receipt",
        submitted_by=inbound_transport,
        version=3,
        completed_at=primary_received_at + timedelta(seconds=1),
        created_at=primary_received_at,
    )
    duplicate_received_at = primary_received_at + timedelta(seconds=1)
    duplicate_provider_message_id = "fixture-receipt-duplicate"
    duplicate_payload_hash = _payload_digest(
        raw_sha256=receipt_hash,
        provider=primary_message.provider,
        provider_account=primary_message.provider_account,
        provider_message_id=duplicate_provider_message_id,
        provider_thread_id=primary_message.provider_thread_id,
        envelope_sender=primary_message.received_sender,
        envelope_recipients=primary_message.received_recipients,
        received_at=duplicate_received_at,
    )
    duplicate_receipt = InboundCorrespondenceReceipt(
        id=_fixture_uuid("duplicate-receipt"),
        provider=primary_message.provider,
        provider_account=primary_message.provider_account,
        provider_message_id=duplicate_provider_message_id,
        provider_thread_id=primary_message.provider_thread_id,
        received_at=duplicate_received_at,
        envelope_sender=primary_message.received_sender,
        envelope_recipients=primary_message.received_recipients,
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
        duplicate_of=primary_receipt,
        quarantined=True,
        quarantine_reason="Duplicate synthetic inbound receipt.",
        raw_message_size=21,
        raw_message_sha256=receipt_hash,
        raw_storage_key=(
            "quarantine/correspondence/raw/"
            f"{_fixture_uuid('duplicate-receipt-storage').hex}"
        ),
        payload_sha256=duplicate_payload_hash,
        idempotency_key="casework-e2e-duplicate-receipt",
        submitted_by=inbound_transport,
        version=2,
        completed_at=duplicate_received_at + timedelta(seconds=1),
        created_at=duplicate_received_at,
    )
    dsn_hash = hashlib.sha256(b"synthetic delivery status message").hexdigest()
    dsn_received_at = dsn_message.received_at
    dsn_matched_at = dsn_message.matched_at
    dsn_payload_hash = _payload_digest(
        raw_sha256=dsn_hash,
        provider=dsn_message.provider,
        provider_account=dsn_message.provider_account,
        provider_message_id=dsn_message.provider_message_id,
        provider_thread_id=dsn_message.provider_thread_id,
        envelope_sender=dsn_message.received_sender,
        envelope_recipients=dsn_message.received_recipients,
        received_at=dsn_received_at,
    )
    dsn_receipt = InboundCorrespondenceReceipt(
        id=_fixture_uuid("dsn-receipt"),
        provider=dsn_message.provider,
        provider_account=dsn_message.provider_account,
        provider_message_id=dsn_message.provider_message_id,
        provider_thread_id=dsn_message.provider_thread_id,
        received_at=dsn_received_at,
        envelope_sender=dsn_message.received_sender,
        envelope_recipients=dsn_message.received_recipients,
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
        delivery_status_original_message_id=sent.rfc_message_id,
        matched_request=request,
        matched_delivery=sent_delivery,
        matched_correspondence=dsn_message,
        matched_at=dsn_matched_at,
        quarantined=False,
        raw_message_size=33,
        raw_message_sha256=dsn_hash,
        raw_storage_key=(
            "quarantine/correspondence/raw/"
            f"{_fixture_uuid('dsn-receipt-storage').hex}"
        ),
        payload_sha256=dsn_payload_hash,
        idempotency_key="casework-e2e-dsn-receipt",
        submitted_by=inbound_transport,
        version=3,
        completed_at=dsn_received_at + timedelta(seconds=1),
        created_at=dsn_received_at,
    )
    # Preserve transport receipt chronology without adding any update path to
    # these immutable evidence rows.
    for receipt in (primary_receipt, duplicate_receipt, dsn_receipt):
        receipt.save_base(
            raw=True,
            force_insert=True,
            using=request._state.db,
        )

    notification = CorrespondenceDeliveryNotification(
        id=_fixture_uuid("dsn-notification"),
        inbound_receipt=dsn_receipt,
        delivery=sent_delivery,
        recipient_index=1,
        recipient=recipient_by_correspondence[sent.pk],
        recipient_match_status=(
            CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
        ),
        action=CorrespondenceDeliveryNotification.Action.FAILED,
        enhanced_status="5.1.1",
        created_at=dsn_matched_at,
    )
    notification.full_clean(validate_unique=False)
    notification.save_base(
        raw=True,
        force_insert=True,
        using=request._state.db,
    )

    _seed_clarification_evidence(
        request=request,
        officer=officer,
        messages=messages,
    )
    _seed_audit_evidence(
        request=request,
        officer=officer,
        outbound_transport=outbound_transport,
        inbound_transport=inbound_transport,
        messages=messages,
        sent_delivery=sent_delivery,
        failed_delivery=failed_delivery,
        attempts=attempts,
    )


def _seed_clarification_evidence(*, request, officer, messages):
    clarification_request = next(
        message for message in messages
        if message.pk == _fixture_uuid("correspondence-2")
    )
    clarification_response = next(
        message for message in messages
        if message.pk == _fixture_uuid("correspondence-0")
    )
    event = StatusEvent.objects.create(
        request=request,
        actor=officer,
        from_status=RequestStatus.RECEIVED,
        to_status=RequestStatus.CLARIFICATION,
        note="Clarification request delivered to requester.",
        visible_to_requester=True,
        occurred_at=clarification_request.sent_at,
        correspondence_effect=(
            StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY
        ),
    )
    Correspondence.objects.filter(
        pk=clarification_request.pk,
    )._bind_locked_resulting_status_event(status_event=event)
    clarification_request.resulting_status_event_id = event.pk

    cycle = ClarificationCycle(
        id=_fixture_uuid("clarification-cycle"),
        request=request,
        outbound_correspondence=clarification_request,
        inbound_response_correspondence=clarification_response,
        opened_at=clarification_request.sent_at,
        responded_at=clarification_response.received_at,
        opened_status_event=event,
        created_at=clarification_request.sent_at,
    )
    cycle.full_clean(validate_unique=False)
    cycle.save_base(
        raw=True,
        force_insert=True,
        using=request._state.db,
    )

    status_audit = AuditLog(
        at=clarification_request.sent_at,
        actor=officer,
        action="request.status_changed",
        target=request.number,
        detail={
            "from_status": RequestStatus.RECEIVED,
            "to_status": RequestStatus.CLARIFICATION,
            "occurred_at": (
                clarification_request.sent_at.astimezone(UTC).isoformat()
            ),
        },
    )
    status_audit.save_base(
        raw=True,
        force_insert=True,
        using=request._state.db,
    )


def _recipient_snapshot_for_audit(index, *, requester_id):
    serialized = json.dumps(
        [
            {
                "id": str(_fixture_uuid(f"recipient-{index}")),
                "recipient_type": CorrespondenceRecipient.RecipientType.TO,
                "email": "synthetic.requester@example.org",
                "display_name": "Synthetic Requester",
                "requester_id": str(requester_id),
            }
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _content_hash(serialized), 1


def _expected_audit_evidence(
    *,
    request,
    officer,
    outbound_transport,
    inbound_transport,
    messages,
    sent_delivery,
    failed_delivery,
    attempts,
):
    deliveries_by_correspondence = {
        sent_delivery.correspondence_id: sent_delivery,
        failed_delivery.correspondence_id: failed_delivery,
    }
    attempts_by_delivery = {attempt.delivery_id: attempt for attempt in attempts}
    lease_seconds = runtime.get("PRR_CORRESPONDENCE_LEASE_SECONDS")
    evidence = []
    for index, message in enumerate(messages):
        if message.direction == Correspondence.Direction.INBOUND:
            is_dsn = (
                message.kind
                == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
            )
            receipt_label = "dsn-receipt" if is_dsn else "primary-receipt"
            evidence.append(
                {
                    "at": message.matched_at + timedelta(seconds=1),
                    "actor_id": inbound_transport.pk,
                    "action": "correspondence.inbound_received",
                    "target": str(_fixture_uuid(receipt_label)),
                    "detail": {
                        "receipt_id": str(_fixture_uuid(receipt_label)),
                        "correspondence_id": str(message.pk),
                        "provider": message.provider,
                        "parsing_status": (
                            InboundCorrespondenceReceipt.ParsingStatus.PARSED
                        ),
                        "matching_status": (
                            InboundCorrespondenceReceipt.MatchingStatus.MATCHED
                        ),
                        "match_method": (
                            InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
                            if is_dsn
                            else InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD
                        ),
                        "raw_message_size": 33 if is_dsn else 21,
                        "attachment_count": 0,
                    },
                    "ip_address": None,
                }
            )
            continue
        common_content = {
            "correspondence_id": str(message.pk),
            "subject_sha256": _content_hash(message.draft_subject),
            "body_sha256": _content_hash(message.draft_body),
        }
        evidence.append(
            {
                "at": message.created_at + timedelta(seconds=1),
                "actor_id": officer.pk,
                "action": "correspondence.draft_created",
                "target": request.number,
                "detail": common_content
                | {
                    "version": 1,
                    "kind": message.kind,
                    "automated": False,
                    "template_identifier": "",
                    "template_version": "",
                    "replaces_id": "",
                },
                "ip_address": None,
            }
        )
        if message.status != Correspondence.Status.DRAFT:
            recipient_sha256, recipient_count = _recipient_snapshot_for_audit(
                index,
                requester_id=request.requester_id,
            )
            evidence.append(
                {
                    "at": message.approved_at,
                    "actor_id": officer.pk,
                    "action": "correspondence.approved",
                    "target": request.number,
                    "detail": common_content
                    | {
                        "version": 2,
                        "automated": False,
                        "recipient_count": recipient_count,
                        "recipient_snapshot_sha256": recipient_sha256,
                    },
                    "ip_address": None,
                }
            )
        delivery = deliveries_by_correspondence.get(message.pk)
        if delivery is None:
            continue
        attempt = attempts_by_delivery[delivery.pk]
        evidence.extend(
            [
                {
                    "at": message.queued_at,
                    "actor_id": officer.pk,
                    "action": "correspondence.queued",
                    "target": request.number,
                    "detail": {
                        "correspondence_id": str(message.pk),
                        "delivery_id": str(delivery.pk),
                        "automated": False,
                    },
                    "ip_address": None,
                },
                {
                    "at": attempt.claimed_at,
                    "actor_id": outbound_transport.pk,
                    "action": "correspondence.delivery_claimed",
                    "target": str(delivery.pk),
                    "detail": {
                        "delivery_id": str(delivery.pk),
                        "correspondence_id": str(message.pk),
                        "attempt_id": str(attempt.pk),
                        "attempt_number": 1,
                        "claim_action": CorrespondenceDeliveryAttempt.Action.SEND,
                        "lease_expires_at": (
                            attempt.claimed_at
                            + timedelta(seconds=lease_seconds)
                        ).astimezone(UTC).isoformat(),
                    },
                    "ip_address": None,
                },
                {
                    "at": attempt.result_at,
                    "actor_id": outbound_transport.pk,
                    "action": "correspondence.delivery_result_recorded",
                    "target": str(delivery.pk),
                    "detail": {
                        "delivery_id": str(delivery.pk),
                        "correspondence_id": str(message.pk),
                        "attempt_id": str(attempt.pk),
                        "attempt_number": 1,
                        "claim_action": CorrespondenceDeliveryAttempt.Action.SEND,
                        "outcome": attempt.result_category,
                        "failure_category": attempt.error_category,
                        "provider_message_id": attempt.provider_message_id,
                        "provider_thread_id": attempt.provider_thread_id,
                    },
                    "ip_address": None,
                },
            ]
        )
    return evidence


def _seed_audit_evidence(**kwargs):
    for expected in _expected_audit_evidence(**kwargs):
        row = AuditLog(**expected)
        # Raw insertion preserves the historical auto_now_add value without
        # creating an update path for append-only audit evidence.
        row.save_base(
            raw=True,
            force_insert=True,
            using=kwargs["request"]._state.db,
        )


_MESSAGE_VALIDATION_FIELDS = (
    "request_id",
    "direction",
    "kind",
    "status",
    "created_by_id",
    "draft_subject",
    "draft_body",
    "draft_metadata",
    "approved_subject",
    "approved_body",
    "approved_metadata",
    "approved_by_id",
    "approved_at",
    "queued_by_id",
    "queued_at",
    "sent_at",
    "matched_by_id",
    "matched_at",
    "failed_at",
    "cancelled_at",
    "failure_category",
    "failure_description",
    "received_subject",
    "received_body",
    "received_sender",
    "received_recipients",
    "received_headers",
    "received_at",
    "provider",
    "provider_account",
    "provider_message_id",
    "provider_thread_id",
    "rfc_message_id",
    "reply_token",
    "template_identifier",
    "template_version",
    "automated",
    "resulting_status_event_id",
    "replaces_id",
    "version",
    "created_at",
)


def _assert_field_values(actual, expected, error_message):
    for field, value in expected.items():
        if getattr(actual, field) != value:
            raise CommandError(
                f"{error_message} Field {field} is inconsistent: "
                f"expected {value!r}, found {getattr(actual, field)!r}."
            )


def _assert_audit_evidence(**kwargs):
    expected_rows = _expected_audit_evidence(**kwargs)
    correspondence_ids = [
        str(message.pk)
        for message in kwargs["messages"]
    ]
    actual_rows = AuditLog.objects.filter(
        action__startswith="correspondence.",
        detail__correspondence_id__in=correspondence_ids,
    )
    expected_by_key = {
        (row["action"], row["detail"]["correspondence_id"]): row
        for row in expected_rows
    }
    actual_by_key = {
        (row.action, row.detail.get("correspondence_id")): row
        for row in actual_rows
    }
    if len(actual_by_key) != actual_rows.count() or set(actual_by_key) != set(
        expected_by_key
    ):
        raise CommandError(
            "The casework fixture audit evidence is inconsistent: "
            f"expected {len(expected_by_key)} keyed rows, found "
            f"{len(actual_by_key)} across {actual_rows.count()} rows."
        )
    for key, expected in expected_by_key.items():
        _assert_field_values(
            actual_by_key[key],
            expected,
            "The casework fixture audit evidence is inconsistent.",
        )


def _assert_clarification_evidence(*, request, officer, messages):
    clarification_request = next(
        message for message in messages
        if message.pk == _fixture_uuid("correspondence-2")
    )
    clarification_response = next(
        message for message in messages
        if message.pk == _fixture_uuid("correspondence-0")
    )
    events = request.events.all()
    if events.count() != 1:
        raise CommandError(
            "The casework fixture clarification status evidence is inconsistent."
        )
    event = events.get()
    _assert_field_values(
        event,
        {
            "request_id": request.pk,
            "actor_id": officer.pk,
            "from_status": RequestStatus.RECEIVED,
            "to_status": RequestStatus.CLARIFICATION,
            "note": "Clarification request delivered to requester.",
            "visible_to_requester": True,
            "occurred_at": clarification_request.sent_at,
            "correspondence_effect": (
                StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY
            ),
        },
        "The casework fixture clarification status evidence is inconsistent.",
    )
    if clarification_request.resulting_status_event_id != event.pk:
        raise CommandError(
            "The casework fixture clarification status evidence is inconsistent."
        )

    cycles = ClarificationCycle.objects.filter(request=request)
    if set(cycles.values_list("pk", flat=True)) != {
        _fixture_uuid("clarification-cycle")
    }:
        raise CommandError(
            "The casework fixture clarification-cycle evidence is inconsistent."
        )
    cycle = cycles.get()
    _assert_field_values(
        cycle,
        {
            "outbound_correspondence_id": clarification_request.pk,
            "inbound_response_correspondence_id": clarification_response.pk,
            "opened_at": clarification_request.sent_at,
            "responded_at": clarification_response.received_at,
            "resolved_at": None,
            "cancelled_at": None,
            "resolved_by_id": None,
            "resolution_notes": "",
            "opened_status_event_id": event.pk,
            "resolved_status_event_id": None,
            "created_at": clarification_request.sent_at,
        },
        "The casework fixture clarification-cycle evidence is inconsistent.",
    )

    audits = AuditLog.objects.filter(
        action="request.status_changed",
        target=request.number,
    )
    if audits.count() != 1:
        raise CommandError(
            "The casework fixture clarification audit evidence is inconsistent."
        )
    _assert_field_values(
        audits.get(),
        {
            "at": clarification_request.sent_at,
            "actor_id": officer.pk,
            "detail": {
                "from_status": RequestStatus.RECEIVED,
                "to_status": RequestStatus.CLARIFICATION,
                "occurred_at": (
                    clarification_request.sent_at.astimezone(UTC).isoformat()
                ),
            },
            "ip_address": None,
        },
        "The casework fixture clarification audit evidence is inconsistent.",
    )


def _assert_fixture(request, officer, outbound_transport, inbound_transport):
    messages = Correspondence.objects.filter(request=request)
    if messages.count() != FIXTURE_MESSAGE_COUNT:
        raise CommandError(
            f"{FIXTURE_REQUEST_NUMBER} must contain exactly "
            f"{FIXTURE_MESSAGE_COUNT} correspondence rows."
        )
    actual_by_id = {message.pk: message for message in messages}
    expected_messages = [
        _message(
            index,
            request=request,
            officer=officer,
            inbound_transport=inbound_transport,
        )
        for index in range(FIXTURE_MESSAGE_COUNT)
    ]
    expected_by_id = {message.pk: message for message in expected_messages}
    clarification_event_ids = list(
        request.events.values_list("pk", flat=True)[:2]
    )
    if len(clarification_event_ids) == 1:
        expected_by_id[
            _fixture_uuid("correspondence-2")
        ].resulting_status_event_id = clarification_event_ids[0]
    if set(actual_by_id) != set(expected_by_id):
        raise CommandError("The designated fixture correspondence IDs are inconsistent.")
    for expected in expected_messages:
        actual = actual_by_id[expected.pk]
        for field in _MESSAGE_VALIDATION_FIELDS:
            if getattr(actual, field) != getattr(expected, field):
                raise CommandError(
                    "The designated fixture correspondence is inconsistent: "
                    f"{expected.pk} field {field}."
                )

    outbound_indexes = [
        index for index, message in enumerate(expected_messages)
        if message.direction == Correspondence.Direction.OUTBOUND
    ]
    expected_recipient_ids = {
        _fixture_uuid(f"recipient-{index}") for index in outbound_indexes
    }
    recipient_indexes = {
        _fixture_uuid(f"recipient-{index}"): index for index in outbound_indexes
    }
    recipients = CorrespondenceRecipient.objects.filter(
        correspondence__request=request,
    )
    if set(recipients.values_list("pk", flat=True)) != expected_recipient_ids:
        raise CommandError(
            "The casework fixture recipient snapshots are inconsistent."
        )
    for recipient in recipients:
        index = recipient_indexes[recipient.pk]
        if (
            recipient.correspondence_id != _fixture_uuid(f"correspondence-{index}")
            or recipient.recipient_type != CorrespondenceRecipient.RecipientType.TO
            or recipient.email != "synthetic.requester@example.org"
            or recipient.display_name != "Synthetic Requester"
            or recipient.requester_id != request.requester_id
            or recipient.created_at
            != expected_by_id[recipient.correspondence_id].created_at
            + timedelta(seconds=1)
        ):
            raise CommandError(
                "The casework fixture recipient snapshots are inconsistent."
            )

    delivery_ids = set(
        CorrespondenceDelivery.objects.filter(
            correspondence__request=request,
        ).values_list("pk", flat=True)
    )
    if delivery_ids != {
        _fixture_uuid("sent-delivery"),
        _fixture_uuid("failed-delivery"),
    }:
        raise CommandError("The casework fixture delivery evidence is inconsistent.")
    sent_delivery = CorrespondenceDelivery.objects.select_related(
        "correspondence"
    ).get(pk=_fixture_uuid("sent-delivery"))
    failed_delivery = CorrespondenceDelivery.objects.select_related(
        "correspondence"
    ).get(pk=_fixture_uuid("failed-delivery"))
    expected_sent = expected_by_id[_fixture_uuid("correspondence-2")]
    expected_failed = expected_by_id[_fixture_uuid("correspondence-4")]
    _assert_field_values(
        sent_delivery,
        {
            "correspondence_id": expected_sent.pk,
            "status": CorrespondenceDelivery.Status.SENT,
            "provider": expected_sent.provider,
            "provider_account": expected_sent.provider_account,
            "idempotency_key": f"correspondence-delivery:{expected_sent.pk}",
            "claim_owner": "",
            "claim_expires_at": None,
            "attempt_count": 1,
            "successful_attempt_number": 1,
            "last_failure_category": "",
            "last_failure_description": "",
            "provider_message_id": expected_sent.provider_message_id,
            "provider_thread_id": expected_sent.provider_thread_id,
            "queued_at": expected_sent.queued_at,
            "completed_at": expected_sent.sent_at,
            "recovery_not_before": (
                expected_sent.queued_at
                + timedelta(seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"))
            ),
            "version": 3,
        },
        "The casework fixture sent-delivery evidence is inconsistent.",
    )
    _assert_field_values(
        failed_delivery,
        {
            "correspondence_id": expected_failed.pk,
            "status": CorrespondenceDelivery.Status.FAILED,
            "provider": expected_failed.provider,
            "provider_account": expected_failed.provider_account,
            "idempotency_key": f"correspondence-delivery:{expected_failed.pk}",
            "claim_owner": "",
            "claim_expires_at": None,
            "attempt_count": 1,
            "successful_attempt_number": None,
            "last_failure_category": _FAILURE_CATEGORY,
            "last_failure_description": _FAILURE_DESCRIPTION,
            "provider_message_id": "",
            "provider_thread_id": "",
            "queued_at": expected_failed.queued_at,
            "completed_at": expected_failed.failed_at,
            "recovery_not_before": (
                expected_failed.queued_at
                + timedelta(seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"))
            ),
            "version": 3,
        },
        "The casework fixture failed-delivery evidence is inconsistent.",
    )
    terminal_correspondence_ids = set(
        messages.filter(
            direction=Correspondence.Direction.OUTBOUND,
            status__in=(Correspondence.Status.SENT, Correspondence.Status.FAILED),
        ).values_list("pk", flat=True)
    )
    if terminal_correspondence_ids != {
        sent_delivery.correspondence_id,
        failed_delivery.correspondence_id,
    }:
        raise CommandError(
            "Every terminal casework fixture message requires exact delivery evidence."
        )

    attempts = CorrespondenceDeliveryAttempt.objects.filter(
        delivery__correspondence__request=request,
    )
    if set(attempts.values_list("pk", flat=True)) != {
        _fixture_uuid("sent-attempt"),
        _fixture_uuid("failed-attempt"),
    }:
        raise CommandError("The casework fixture delivery attempts are inconsistent.")
    sent_attempt = attempts.get(pk=_fixture_uuid("sent-attempt"))
    failed_attempt = attempts.get(pk=_fixture_uuid("failed-attempt"))
    _assert_field_values(
        sent_attempt,
        {
            "delivery_id": sent_delivery.pk,
            "attempt_number": 1,
            "action": CorrespondenceDeliveryAttempt.Action.SEND,
            "claim_owner": str(outbound_transport.pk),
            "claimed_at": expected_sent.queued_at,
            "result_at": expected_sent.sent_at,
            "result_category": CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            "provider_response_metadata": _SENT_PROVIDER_METADATA,
            "provider_message_id": expected_sent.provider_message_id,
            "provider_thread_id": expected_sent.provider_thread_id,
            "provider_timestamp": expected_sent.sent_at,
            "error_category": "",
            "result_fingerprint": _result_fingerprint(
                outcome=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                provider_message_id=expected_sent.provider_message_id,
                provider_thread_id=expected_sent.provider_thread_id,
                provider_timestamp=expected_sent.sent_at,
                failure_category="",
                provider_metadata=_SENT_PROVIDER_METADATA,
            ),
            "actor_id": outbound_transport.pk,
            "created_at": expected_sent.queued_at,
        },
        "The casework fixture sent-delivery attempt is inconsistent.",
    )
    _assert_field_values(
        failed_attempt,
        {
            "delivery_id": failed_delivery.pk,
            "attempt_number": 1,
            "action": CorrespondenceDeliveryAttempt.Action.SEND,
            "claim_owner": str(outbound_transport.pk),
            "claimed_at": expected_failed.queued_at,
            "result_at": expected_failed.failed_at,
            "result_category": CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            "provider_response_metadata": _FAILED_PROVIDER_METADATA,
            "provider_message_id": "",
            "provider_thread_id": "",
            "provider_timestamp": None,
            "error_category": _FAILURE_CATEGORY,
            "result_fingerprint": _result_fingerprint(
                outcome=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                provider_message_id="",
                provider_thread_id="",
                provider_timestamp=None,
                failure_category=_FAILURE_CATEGORY,
                provider_metadata=_FAILED_PROVIDER_METADATA,
            ),
            "actor_id": outbound_transport.pk,
            "created_at": expected_failed.queued_at,
        },
        "The casework fixture failed-delivery attempt is inconsistent.",
    )
    _assert_audit_evidence(
        request=request,
        officer=officer,
        outbound_transport=outbound_transport,
        inbound_transport=inbound_transport,
        messages=expected_messages,
        sent_delivery=sent_delivery,
        failed_delivery=failed_delivery,
        attempts=[sent_attempt, failed_attempt],
    )
    _assert_clarification_evidence(
        request=request,
        officer=officer,
        messages=list(actual_by_id.values()),
    )

    expected_attachment_ids = {
        _fixture_uuid(f"attachment-{index}") for index in range(1, 9)
    }
    attachments = CorrespondenceAttachment.objects.filter(
        correspondence__request=request,
    )
    if set(attachments.values_list("pk", flat=True)) != expected_attachment_ids:
        raise CommandError("The casework fixture attachment metadata is inconsistent.")
    expected_drafts = [
        message for message in expected_messages
        if message.status == Correspondence.Status.DRAFT
    ][:8]
    for index, expected_message in enumerate(expected_drafts, start=1):
        attachment = attachments.get(pk=_fixture_uuid(f"attachment-{index}"))
        if (
            attachment.correspondence_id != expected_message.pk
            or attachment.inbound_receipt_id is not None
            or attachment.original_filename != f"fixture-attachment-{index}.pdf"
            or attachment.content_type != "application/pdf"
            or attachment.size != 1_024 + index
            or attachment.sha256
            != hashlib.sha256(f"fixture-{index}".encode()).hexdigest()
            or attachment.storage_key
            != (
                "quarantine/correspondence/attachments/"
                f"{_fixture_uuid(f'attachment-storage-{index}').hex}"
            )
            or attachment.scan_status != CorrespondenceAttachment.ScanStatus.PENDING
            or not attachment.quarantined
            or attachment.quarantine_reason != ""
            or attachment.uploaded_by_id != officer.pk
            or attachment.scanned_at is not None
            or attachment.created_at
            != expected_message.created_at + timedelta(seconds=1)
        ):
            raise CommandError(
                "The casework fixture attachment metadata is inconsistent."
            )

    expected_receipt_ids = {
        _fixture_uuid("primary-receipt"),
        _fixture_uuid("duplicate-receipt"),
        _fixture_uuid("dsn-receipt"),
    }
    receipts = InboundCorrespondenceReceipt.objects.filter(
        pk__in=expected_receipt_ids,
    )
    if set(receipts.values_list("pk", flat=True)) != expected_receipt_ids:
        raise CommandError("The casework fixture inbound receipts are inconsistent.")
    primary_receipt = receipts.get(pk=_fixture_uuid("primary-receipt"))
    duplicate_receipt = receipts.get(pk=_fixture_uuid("duplicate-receipt"))
    dsn_receipt = receipts.get(pk=_fixture_uuid("dsn-receipt"))
    receipt_hash = hashlib.sha256(b"synthetic raw message").hexdigest()
    dsn_hash = hashlib.sha256(b"synthetic delivery status message").hexdigest()
    expected_primary = expected_by_id[_fixture_uuid("correspondence-0")]
    expected_dsn = expected_by_id[_fixture_uuid("correspondence-5")]
    primary_payload_hash = _payload_digest(
        raw_sha256=receipt_hash,
        provider=expected_primary.provider,
        provider_account=expected_primary.provider_account,
        provider_message_id=expected_primary.provider_message_id,
        provider_thread_id=expected_primary.provider_thread_id,
        envelope_sender=expected_primary.received_sender,
        envelope_recipients=expected_primary.received_recipients,
        received_at=expected_primary.received_at,
    )
    _assert_field_values(
        primary_receipt,
        {
            "provider": expected_primary.provider,
            "provider_account": expected_primary.provider_account,
            "provider_message_id": expected_primary.provider_message_id,
            "provider_thread_id": expected_primary.provider_thread_id,
            "rfc_message_id": "",
            "in_reply_to": "",
            "references": [],
            "received_at": expected_primary.received_at,
            "envelope_sender": expected_primary.received_sender,
            "envelope_recipients": expected_primary.received_recipients,
            "parsing_status": InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            "matching_status": InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            "match_method": InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
            "delivery_status_original_message_id": "",
            "candidate_request_id": None,
            "matched_request_id": request.pk,
            "matched_delivery_id": None,
            "matched_correspondence_id": expected_primary.pk,
            "matched_at": expected_primary.matched_at,
            "matched_by_id": None,
            "duplicate_of_id": None,
            "quarantined": False,
            "quarantine_reason": "",
            "parsing_error": "",
            "raw_message_size": 21,
            "raw_message_sha256": receipt_hash,
            "raw_storage_key": (
                "quarantine/correspondence/raw/"
                f"{_fixture_uuid('primary-receipt-storage').hex}"
            ),
            "payload_sha256": primary_payload_hash,
            "idempotency_key": "casework-e2e-primary-receipt",
            "submitted_by_id": inbound_transport.pk,
            "ip_address": None,
            "version": 3,
            "completed_at": expected_primary.received_at + timedelta(seconds=1),
            "created_at": expected_primary.received_at,
        },
        "The casework fixture primary inbound receipt is inconsistent.",
    )
    provider_thread_evidence = _provider_thread_evidence(primary_receipt)
    if (
        provider_thread_evidence.unique_request_id != request.pk
        or provider_thread_evidence.exact_outbound_id != expected_sent.pk
    ):
        raise CommandError(
            "The casework fixture provider-thread match evidence is inconsistent."
        )
    duplicate_received_at = expected_primary.received_at + timedelta(seconds=1)
    duplicate_payload_hash = _payload_digest(
        raw_sha256=receipt_hash,
        provider=expected_primary.provider,
        provider_account=expected_primary.provider_account,
        provider_message_id="fixture-receipt-duplicate",
        provider_thread_id=expected_primary.provider_thread_id,
        envelope_sender=expected_primary.received_sender,
        envelope_recipients=expected_primary.received_recipients,
        received_at=duplicate_received_at,
    )
    _assert_field_values(
        duplicate_receipt,
        {
            "provider": expected_primary.provider,
            "provider_account": expected_primary.provider_account,
            "provider_message_id": "fixture-receipt-duplicate",
            "provider_thread_id": expected_primary.provider_thread_id,
            "rfc_message_id": "",
            "in_reply_to": "",
            "references": [],
            "received_at": duplicate_received_at,
            "envelope_sender": expected_primary.received_sender,
            "envelope_recipients": expected_primary.received_recipients,
            "parsing_status": InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE,
            "matching_status": InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            "match_method": "",
            "delivery_status_original_message_id": "",
            "candidate_request_id": None,
            "matched_request_id": None,
            "matched_delivery_id": None,
            "matched_correspondence_id": None,
            "matched_at": None,
            "matched_by_id": None,
            "duplicate_of_id": primary_receipt.pk,
            "quarantined": True,
            "quarantine_reason": "Duplicate synthetic inbound receipt.",
            "parsing_error": "",
            "raw_message_size": 21,
            "raw_message_sha256": receipt_hash,
            "raw_storage_key": (
                "quarantine/correspondence/raw/"
                f"{_fixture_uuid('duplicate-receipt-storage').hex}"
            ),
            "payload_sha256": duplicate_payload_hash,
            "idempotency_key": "casework-e2e-duplicate-receipt",
            "submitted_by_id": inbound_transport.pk,
            "ip_address": None,
            "version": 2,
            "completed_at": duplicate_received_at + timedelta(seconds=1),
            "created_at": duplicate_received_at,
        },
        "The casework fixture duplicate inbound receipt is inconsistent.",
    )
    dsn_payload_hash = _payload_digest(
        raw_sha256=dsn_hash,
        provider=expected_dsn.provider,
        provider_account=expected_dsn.provider_account,
        provider_message_id=expected_dsn.provider_message_id,
        provider_thread_id=expected_dsn.provider_thread_id,
        envelope_sender=expected_dsn.received_sender,
        envelope_recipients=expected_dsn.received_recipients,
        received_at=expected_dsn.received_at,
    )
    _assert_field_values(
        dsn_receipt,
        {
            "provider": expected_dsn.provider,
            "provider_account": expected_dsn.provider_account,
            "provider_message_id": expected_dsn.provider_message_id,
            "provider_thread_id": expected_dsn.provider_thread_id,
            "rfc_message_id": "",
            "in_reply_to": "",
            "references": [],
            "received_at": expected_dsn.received_at,
            "envelope_sender": expected_dsn.received_sender,
            "envelope_recipients": expected_dsn.received_recipients,
            "parsing_status": InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            "matching_status": InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            "match_method": InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            "delivery_status_original_message_id": expected_sent.rfc_message_id,
            "candidate_request_id": None,
            "matched_request_id": request.pk,
            "matched_delivery_id": sent_delivery.pk,
            "matched_correspondence_id": expected_dsn.pk,
            "matched_at": expected_dsn.matched_at,
            "matched_by_id": None,
            "duplicate_of_id": None,
            "quarantined": False,
            "quarantine_reason": "",
            "parsing_error": "",
            "raw_message_size": 33,
            "raw_message_sha256": dsn_hash,
            "raw_storage_key": (
                "quarantine/correspondence/raw/"
                f"{_fixture_uuid('dsn-receipt-storage').hex}"
            ),
            "payload_sha256": dsn_payload_hash,
            "idempotency_key": "casework-e2e-dsn-receipt",
            "submitted_by_id": inbound_transport.pk,
            "ip_address": None,
            "version": 3,
            "completed_at": expected_dsn.received_at + timedelta(seconds=1),
            "created_at": expected_dsn.received_at,
        },
        "The casework fixture DSN receipt is inconsistent.",
    )

    notifications = CorrespondenceDeliveryNotification.objects.filter(
        pk=_fixture_uuid("dsn-notification"),
    )
    if set(notifications.values_list("pk", flat=True)) != {
        _fixture_uuid("dsn-notification")
    }:
        raise CommandError("The casework fixture DSN evidence is inconsistent.")
    notification = notifications.get()
    sent_index = next(
        index for index, message in enumerate(expected_messages)
        if message.pk == sent_delivery.correspondence_id
    )
    if (
        notification.inbound_receipt_id != dsn_receipt.pk
        or notification.delivery_id != sent_delivery.pk
        or notification.recipient_index != 1
        or notification.recipient_id
        != _fixture_uuid(f"recipient-{sent_index}")
        or notification.recipient_match_status
        != CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
        or notification.action != CorrespondenceDeliveryNotification.Action.FAILED
        or notification.enhanced_status != "5.1.1"
        or notification.created_at != expected_dsn.matched_at
    ):
        raise CommandError("The casework fixture DSN evidence is inconsistent.")

class Command(BaseCommand):
    help = (
        "Seed a deterministic synthetic 2,000-message case for test-only "
        "Playwright and contract verification."
    )

    def handle(self, *args, **options):
        if not getattr(settings, "PRR_TEST_FIXTURE_SEED_ENABLED", False):
            raise CommandError("test-only casework fixture seeding is disabled")

        with transaction.atomic():
            officer = _seed_users()
            _seed_observer()
            role_users = _seed_role_users()
            outbound_transport, inbound_transport = _system_transport_identities()
            request = _seed_case(officer)
            existing_count = Correspondence.objects.filter(request=request).count()
            if existing_count == 0:
                messages = [
                    _message(
                        index,
                        request=request,
                        officer=officer,
                        inbound_transport=inbound_transport,
                    )
                    for index in range(FIXTURE_MESSAGE_COUNT)
                ]
                message_created_at = [message.created_at for message in messages]
                Correspondence.objects.bulk_create(messages, batch_size=500)
                _restore_created_at(
                    messages,
                    message_created_at,
                    using=request._state.db,
                )
                _seed_related_evidence(
                    request,
                    officer,
                    outbound_transport,
                    inbound_transport,
                    messages,
                )
            elif existing_count != FIXTURE_MESSAGE_COUNT:
                raise CommandError(
                    f"{FIXTURE_REQUEST_NUMBER} contains {existing_count} messages; "
                    "refusing to rewrite immutable fixture evidence."
                )
            _seed_awaiting_statutory_response_case(officer)
            _seed_forwarded_requester_case(officer)
            supersede_request = _seed_supersede_case(officer)
            _seed_evaluation_run(officer, supersede_request)
            _seed_knowledge_pack()
            _seed_estimate_letter_case(officer)
            _seed_trust_ladder_case(officer)
            _seed_redaction_proposal_case(officer)
            _seed_review_flight_case(officer)
            _seed_rescope_case(officer)
            _seed_email_intake_redirect_cases(officer, inbound_transport)
            _reconcile_fixture_inbound_work(request)
            _seed_role_work(
                request,
                officer=officer,
                role_users=role_users,
            )
            _assert_fixture(
                request,
                officer,
                outbound_transport,
                inbound_transport,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {FIXTURE_REQUEST_NUMBER}; asserted exactly "
                f"{FIXTURE_MESSAGE_COUNT} correspondence rows."
            )
        )
