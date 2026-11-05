# ABOUTME: Staff view of PublicRecordsRequest — list/retrieve/partial_update
# ABOUTME: plus lifecycle transitions that call the records service layer.
import datetime as dt
import hashlib
import json
import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    Exists,
    F,
    IntegerField,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Lower, NullIf
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.advisory import AdvisoryLatestSerializer, AdvisoryRunSerializer
from apps.api.case_scope import scope_case_queryset
from apps.api.handoffs import human_only_handoff
from apps.api.identity import (
    WORKFLOW_STAFF_ROLES,
    StaffRole,
    has_staff_role,
    staff_roles,
)
from apps.api.resources.collection_tasks import CollectionTaskSerializer
from apps.api.resources.work_items import (
    INBOX_MESSAGE_WORK_KINDS,
    NEXT_ACTION_LABELS,
    UNRESOLVED_DISPOSITIONS,
    annotate_work_item_sources,
    order_work_items_for_next_action,
    role_scoped_work_items,
)
from apps.api.vault_counts import (
    VaultCollectionRequestSerializer,
    VaultCountLatestSerializer,
    VaultCountRequestSerializer,
    VaultCountRunSerializer,
)
from apps.audit.models import audit
from apps.configuration import runtime
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    InboundCorrespondenceReceipt,
)
from apps.records import services
from apps.records.advisory_ai import (
    INTAKE_LABELS,
    request_intake_classification,
    request_request_intent_advisory,
)
from apps.records.attachments import (
    AttachmentValidationError,
    attachment_digest_descriptors,
    validate_attachments,
)
from apps.records.intake import submit_intake
from apps.records.jlarc import classify_request_for_jlarc
from apps.records.legacy_timeline import (
    COMPLIANCE_NOTE_MARKERS,
    compliance_flagged_note,
)
from apps.records.models import (
    CASE_NOTE_MAX_LENGTH,
    AdvisoryFeedback,
    AdvisoryRun,
    CaseNote,
    Channel,
    CollectionTask,
    EstimateRevision,
    FerpaIdentityVerification,
    FerpaIdentityVerificationMethod,
    FootagePreservation,
    InitialResponseAttestation,
    Installment,
    IntakeAttachment,
    JlarcRequestMetrics,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RecordsEstimate,
    Requester,
    RequestStatus,
    StaffIntakeSubmission,
    StatusEvent,
    VaultCountRun,
)
from apps.records.record_uploads import (
    OUTCOME_ADDED,
    RecordUploadClosedCase,
    RecordUploadLimitExceeded,
    upload_records,
)
from apps.records.record_uploads import (
    OUTCOMES as RECORD_UPLOAD_OUTCOMES,
)
from apps.records.response_due import (
    DueBasis,
    annotate_next_response_due,
    next_response_due,
)
from apps.records.upload_handlers import (
    IntakeAttachmentLimitUploadHandler,
    RecordUploadLimitUploadHandler,
)
from apps.records.vault_collections import queue_vault_collection
from apps.records.vault_counts import (
    VaultCountAlreadyRunning,
    request_vault_count,
    vault_count_enabled,
)
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt

logger = logging.getLogger(__name__)


class RequestsRequesterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Requester
        fields = [
            "name", "email", "phone", "mailing_address", "city", "state",
            "postal_code", "company",
        ]


class RequestsIntakeAttachmentSerializer(serializers.ModelSerializer):
    scan_status = serializers.CharField(
        source="effective_scan_status",
        read_only=True,
    )
    scanned_at = serializers.DateTimeField(
        source="effective_scanned_at",
        read_only=True,
    )

    class Meta:
        model = IntakeAttachment
        fields = [
            "id", "original_filename", "content_type", "size", "sha256",
            "scan_status", "created_at", "scanned_at",
        ]


class RequestsStatusEventSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.username", read_only=True, default=None)

    class Meta:
        model = StatusEvent
        fields = ["occurred_at", "from_status", "to_status", "note",
                  "actor", "visible_to_requester"]


class RequestsEstimateRevisionSerializer(serializers.ModelSerializer):
    revised_by = serializers.CharField(source="revised_by.username", read_only=True, default=None)

    class Meta:
        model = EstimateRevision
        fields = ["previous_date", "new_date", "reason", "revised_by",
                  "automated", "created_at"]


class RequestsCollectionTaskSerializer(serializers.ModelSerializer):
    system = serializers.CharField(source="system.name", read_only=True, default=None)

    class Meta:
        model = CollectionTask
        fields = ["id", "system", "state", "due_date", "kind"]


class RequestsFootagePreservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = FootagePreservation
        fields = ["id", "state"]


#: How much of the description stands in for a subject when no advisory short
#: title exists. Long enough to tell two cases apart, short enough to stay one
#: table cell.
DESCRIPTION_SNIPPET_LENGTH = 140


def description_snippet(description: str | None) -> str:
    """Collapse a request description to a single scannable line.

    Reads the legal record; never writes it. Intake accepts newlines and the
    email path accepts whole quoted threads, so the raw value would otherwise
    blow a table row open.
    """
    collapsed = " ".join((description or "").split())
    if len(collapsed) <= DESCRIPTION_SNIPPET_LENGTH:
        return collapsed
    return collapsed[:DESCRIPTION_SNIPPET_LENGTH].rstrip() + "…"


def advisory_short_title(obj) -> str:
    """The annotated advisory short title, or "" when there is none.

    Annotated by :meth:`RequestsViewSet.get_queryset`; blank on any queryset
    that did not annotate it, which is the honest answer rather than an
    per-row query. Blank text is treated as absent so a model that answered
    with whitespace cannot hide the description behind an empty column.
    """
    return (getattr(obj, "_advisory_short_title", None) or "").strip()


class CaseNoteSerializer(serializers.ModelSerializer):
    """One internal note, with the name of whoever wrote it."""

    author_username = serializers.CharField(
        source="author.username", read_only=True
    )

    class Meta:
        model = CaseNote
        fields = ["id", "body", "author_username", "created_at"]
        read_only_fields = fields


class CaseNoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField(
        max_length=CASE_NOTE_MAX_LENGTH,
        trim_whitespace=True,
        allow_blank=False,
    )


#: How much of a note the case list carries. The list shows one line to say
#: what the last thing that happened was; reading the note is what opening the
#: case is for, and shipping 10,000 characters per row to answer a glance
#: would be its own kind of leak.
CASE_NOTE_SNIPPET_LENGTH = 160


class PublicRecordsRequestListSerializer(serializers.ModelSerializer):
    requester_email = serializers.EmailField(source="requester.email", read_only=True)
    requester_name = serializers.CharField(source="requester.name", read_only=True)
    assigned_officer_username = serializers.CharField(
        source="assigned_officer.username", read_only=True, default=None)
    initial_response_delivered = serializers.BooleanField(read_only=True)
    matter_kind_display = serializers.CharField(
        source="get_matter_kind_display", read_only=True,
    )
    inbox_needs_attention = serializers.SerializerMethodField()
    latest_inbound_at = serializers.SerializerMethodField()
    subject = serializers.SerializerMethodField()
    subject_source = serializers.SerializerMethodField()
    next_due = serializers.SerializerMethodField()
    next_due_basis = serializers.SerializerMethodField()
    latest_note = serializers.SerializerMethodField()

    class Meta:
        model = PublicRecordsRequest
        fields = ["id", "number", "status", "matter_kind",
                  "matter_kind_display",
                  "received_at", "statutory_response_due",
                  "ferpa_access_due",
                  "estimated_completion", "closed_at", "requester_email",
                  "requester_name", "assigned_officer_username",
                  "tolled_business_days", "acknowledgment_sent_at",
                  "initial_response_delivered",
                  "next_due", "next_due_basis",
                  "latest_note",
                  "inbox_needs_attention", "latest_inbound_at",
                  "subject", "subject_source"]
        read_only_fields = ["id", "number", "status", "matter_kind",
                            "received_at",
                            "statutory_response_due", "ferpa_access_due",
                            "estimated_completion",
                            "closed_at", "tolled_business_days",
                            "acknowledgment_sent_at",
                            "initial_response_delivered"]

    @extend_schema_field(serializers.IntegerField())
    def get_inbox_needs_attention(self, obj):
        """Open requester-email rows on the case. Annotated by ``?queue=inbox``;
        zero elsewhere, which is also the honest answer for a closed case."""
        return int(getattr(obj, "_inbox_needs_attention", 0) or 0)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_latest_inbound_at(self, obj):
        value = getattr(obj, "_latest_inbound_at", None)
        return value.isoformat() if value else None

    @extend_schema_field(serializers.CharField())
    def get_subject(self, obj):
        """A one-line scanning label for the case. Never the legal record.

        ``request.description`` is what was asked for and is never edited
        (AGENTS.md rule 9). This is a column heading for a list, derived two
        ways and always declaring which: an advisory short title proposed by a
        completed request-intent run, or, failing that, the opening of the
        description itself with its whitespace collapsed. Neither writes
        anything back to the request, and the advisory form is a suggestion a
        human has not adopted -- which is exactly why ``subject_source`` ships
        beside it rather than being inferred by the reader.
        """
        return advisory_short_title(obj) or description_snippet(obj.description)

    @extend_schema_field(serializers.ChoiceField(choices=["ai", "description"]))
    def get_subject_source(self, obj):
        """Where ``subject`` came from, so the UI can mark advisory text."""
        return "ai" if advisory_short_title(obj) else "description"

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_next_due(self, obj):
        """The date this case owes an answer by next, or null when none.

        Not ``statutory_response_due``, which ships beside it unchanged: that
        date measures the RCW 42.56.520 first response and is spent once one
        was delivered. See ``apps/records/response_due.py`` for the rule per
        matter kind; ``next_due_basis`` says which one applied.
        """
        answer = next_response_due(obj)
        return answer.date.isoformat() if answer.date else None

    @extend_schema_field(
        serializers.ChoiceField(choices=list(DueBasis.CHOICES), allow_null=True)
    )
    def get_next_due_basis(self, obj):
        """Why ``next_due`` is that date, so the UI never has to guess.

        The distinction is legal, not cosmetic: ``statutory`` is a deadline
        under RCW 42.56.520, ``ferpa`` one under 34 CFR 99.10(b), ``estimate``
        the date the District promised this requester, and
        ``district_target`` an internal service level that binds nobody.
        """
        return next_response_due(obj).basis

    @extend_schema_field(CaseNoteSerializer(allow_null=True))
    def get_latest_note(self, obj):
        """The most recent internal note on the case, as a one-line snippet.

        Three annotated columns rather than a note lookup per row: a fifty-case
        page must cost one query, not fifty-one. Null where the case has no
        note, and on any queryset that did not annotate -- the honest answer,
        rather than a hidden query.

        Only a snippet. The list says what the last thing that happened was;
        reading the note is what opening the case is for.
        """
        body = getattr(obj, "_latest_note_body", None)
        if not body:
            return None
        collapsed = " ".join(body.split())
        if len(collapsed) > CASE_NOTE_SNIPPET_LENGTH:
            collapsed = collapsed[:CASE_NOTE_SNIPPET_LENGTH].rstrip() + "…"
        created_at = getattr(obj, "_latest_note_at", None)
        return {
            "body_snippet": collapsed,
            "author_username": getattr(obj, "_latest_note_author", None),
            "created_at": created_at.isoformat() if created_at else None,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        user = self.context["request"].user
        if not has_staff_role(user, StaffRole.RECORDS_OFFICER):
            data["requester_email"] = None
            data["requester_name"] = None
        return data


class PublicRecordsRequestDetailSerializer(PublicRecordsRequestListSerializer):
    requester = RequestsRequesterSerializer(read_only=True)
    events = serializers.SerializerMethodField()
    estimate_revisions = RequestsEstimateRevisionSerializer(many=True, read_only=True)
    collection_tasks = serializers.SerializerMethodField()
    footage_preservations = serializers.SerializerMethodField()
    intake_attachments = serializers.SerializerMethodField()
    email_intake_auto_created = serializers.SerializerMethodField()
    email_intake_receipt_id = serializers.SerializerMethodField()
    jlarc_metrics = serializers.SerializerMethodField()
    initial_response_plan = serializers.SerializerMethodField()
    assigned_officer_id = serializers.IntegerField(
        read_only=True,
        allow_null=True,
    )
    merged_into = serializers.SerializerMethodField()
    merged_sources = serializers.SerializerMethodField()
    external_initial_response = serializers.SerializerMethodField()
    ferpa_identity_verification = serializers.SerializerMethodField()

    class Meta(PublicRecordsRequestListSerializer.Meta):
        fields = [*PublicRecordsRequestListSerializer.Meta.fields, "description", "channel", "requester", "events", "estimate_revisions", "collection_tasks", "footage_preservations", "intake_attachments", "email_intake_auto_created", "email_intake_receipt_id", "jlarc_metrics", "initial_response_plan", "assigned_officer_id", "merged_into", "merged_sources", "external_initial_response", "ferpa_identity_verification"]
        read_only_fields = [*PublicRecordsRequestListSerializer.Meta.read_only_fields, "description", "channel"]

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_ferpa_identity_verification(self, obj):
        """Who verified the parent or eligible student, when, and how.

        Null on any other kind of matter, and null on a FERPA matter until a
        Records Officer has recorded it — which is the state that says nothing
        may be disclosed yet (34 CFR 99.10).
        """
        if obj.matter_kind != MatterKind.FERPA_PARENT:
            return None
        try:
            verification = obj.ferpa_identity_verification
        except FerpaIdentityVerification.DoesNotExist:
            return None
        payload = {
            "verified_at": verification.verified_at.isoformat(),
            "verified_by": verification.verified_by.username,
            "method": verification.method,
            "method_display": verification.get_method_display(),
            "note": verification.note,
        }
        # Every workflow role needs to know whether identity was verified,
        # because nothing may be disclosed until it was. The officer's free
        # text is different: it is what they relied on, so it can name a
        # custody order, a guardian, or a student. Only the role that may
        # record it reads it, the same way requester identity is masked below.
        if not has_staff_role(
            self.context["request"].user, StaffRole.RECORDS_OFFICER
        ):
            payload["note"] = None
        return payload

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_merged_into(self, obj):
        """The case of record this duplicate was merged into, by number."""
        return obj.merged_into.number if obj.merged_into_id else None

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_merged_sources(self, obj):
        """Duplicates merged into this case, oldest receipt first."""
        return list(
            obj.merged_sources.order_by("received_at", "number")
            .values_list("number", flat=True)
        )

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_external_initial_response(self, obj):
        """The officer's statement that the first response went out elsewhere."""
        try:
            attestation = obj.initial_response_attestation
        except InitialResponseAttestation.DoesNotExist:
            return None
        return {
            "sent_via": attestation.sent_via,
            "sent_via_label": attestation.get_sent_via_display(),
            "responded_on": (
                attestation.responded_on.isoformat()
                if attestation.responded_on
                else None
            ),
            "evidence": attestation.evidence,
            "attested_by": attestation.attested_by.username,
            "created_at": attestation.created_at.isoformat(),
        }

    @extend_schema_field(RequestsStatusEventSerializer(many=True))
    def get_events(self, obj):
        qs = obj.events.order_by("-occurred_at")
        return RequestsStatusEventSerializer(qs, many=True).data

    @extend_schema_field(RequestsFootagePreservationSerializer(many=True))
    def get_footage_preservations(self, obj):
        try:
            preservation = obj.footage_preservation
        except FootagePreservation.DoesNotExist:
            return []
        return [RequestsFootagePreservationSerializer(preservation).data]

    @extend_schema_field(RequestsCollectionTaskSerializer(many=True))
    def get_collection_tasks(self, obj):
        user = self.context["request"].user
        tasks = obj.collection_tasks.all()
        if has_staff_role(user, StaffRole.LIAISON) and not has_staff_role(
            user, StaffRole.RECORDS_OFFICER
        ):
            tasks = tasks.filter(assigned_to=user)
        return RequestsCollectionTaskSerializer(tasks, many=True).data

    @extend_schema_field(RequestsIntakeAttachmentSerializer(many=True))
    def get_intake_attachments(self, obj):
        user = self.context["request"].user
        if not has_staff_role(user, StaffRole.RECORDS_OFFICER):
            return []
        return RequestsIntakeAttachmentSerializer(
            obj.intake_attachments.all(), many=True
        ).data

    def _email_intake_receipts(self, obj):
        return obj.inbound_correspondence_receipts.filter(
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            ),
            match_method=(
                InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
            ),
        )

    @extend_schema_field(serializers.BooleanField())
    def get_email_intake_auto_created(self, obj):
        return self._email_intake_receipts(obj).exists()

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_email_intake_receipt_id(self, obj):
        """The retained receipt behind an automatically opened case.

        The case detail is the one place that already knows this relationship,
        so it says so here rather than making the screen fetch and scan the
        case's whole message list to rediscover it. It is an opaque identifier:
        acting on it still requires the Records Officer role the receipt
        endpoints enforce.
        """
        receipt_id = (
            self._email_intake_receipts(obj)
            .order_by("received_at", "id")
            .values_list("pk", flat=True)
            .first()
        )
        return str(receipt_id) if receipt_id else None

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_jlarc_metrics(self, obj):
        snapshot = (
            obj.jlarc_metric_snapshots.filter(superseded_by__isnull=True)
            .select_related("created_by")
            .order_by("-revision")
            .first()
        )
        return JlarcRequestMetricsSerializer(snapshot).data if snapshot else None

    @extend_schema_field(serializers.DictField())
    def get_initial_response_plan(self, obj):
        return initial_response_plan(obj)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        user = self.context["request"].user
        if not has_staff_role(user, StaffRole.RECORDS_OFFICER):
            data["requester"] = None
            data["assigned_officer_id"] = None
            for event in data["events"]:
                event["note"] = ""
                event["actor"] = None
            for revision in data["estimate_revisions"]:
                revision["reason"] = ""
                revision["revised_by"] = None
        return data


#: The four things that must happen for a five-business-day response to exist.
#: Nothing before the last one satisfies RCW 42.56.520 - a draft is not a
#: response, and neither is an approval or a queued message.
INITIAL_RESPONSE_STEPS = (
    ("prepare", "Prepare the acknowledgment draft"),
    ("approve", "Records Officer approves the exact wording"),
    ("queue", "Queue it for delivery"),
    ("deliver", "Mail provider confirms delivery"),
)

#: state -> (index of the step that is current, next action for the officer).
#: An index of len(INITIAL_RESPONSE_STEPS) means every step is complete. An
#: index of -1 means the opposite and is not the same thing as "complete":
#: no step was ever taken and none can be taken now, because the request was
#: closed. Only ``terminal_unverified`` uses it, and it must never render as
#: a finished ladder - that is the exact misreading this state exists to stop.
_INITIAL_RESPONSE_GUIDANCE = {
    "deadline_calculating": (
        0,
        (
            "Wait for the statutory due date to be calculated, then prepare the "
            "acknowledgment."
        ),
    ),
    "scheduled": (
        0,
        (
            "Prepare and send the acknowledgment. The safety net is a backstop, "
            "not the plan."
        ),
    ),
    "due_for_safety_net": (
        0,
        (
            "The response is due now. Prepare and send the acknowledgment without "
            "waiting for the safety net."
        ),
    ),
    "manual_required": (
        0,
        "Prepare an acknowledgment draft in Requester correspondence.",
    ),
    "automatic_blocked": (
        0,
        (
            "Cancel the unsent automatic acknowledgment, then prepare and approve "
            "a human one."
        ),
    ),
    "human_draft": (
        1,
        (
            "Approve the acknowledgment draft. A draft does not satisfy the "
            "deadline."
        ),
    ),
    "approved_not_queued": (
        2,
        (
            "Queue the approved acknowledgment for delivery. Approval alone does "
            "not satisfy the deadline."
        ),
    ),
    "queued": (
        3,
        (
            "Wait for the mail provider to confirm delivery; no action is needed "
            "unless it fails."
        ),
    ),
    "attention_required": (
        3,
        (
            "Delivery failed or was cancelled. Retry it or prepare a replacement "
            "acknowledgment."
        ),
    ),
    "delivered": (len(INITIAL_RESPONSE_STEPS), ""),
    "terminal_excluded": (len(INITIAL_RESPONSE_STEPS), ""),
    "terminal_unverified": (
        -1,
        (
            "Closed with no five-day response on record. Confirm whether one was "
            "sent and record it, or document why none was owed."
        ),
    ),
    # A matter governed by other law never owed an RCW 42.56.520 response, so
    # the ladder is not finished — it does not apply. -1 keeps every step
    # blank rather than claiming a statutory response was made.
    "not_applicable": (
        -1,
        (
            "This matter is not a public records request and owes no RCW "
            "42.56.520 five-day response."
        ),
    ),
    # A merged duplicate took no step and can take none; the ladder belongs
    # to the case of record, so it must not render as finished here either.
    "merged": (
        -1,
        (
            "Merged into another case. Prepare and send the five-day response on "
            "the case of record if it is still owed there."
        ),
    ),
}


def initial_response_guidance(state: str) -> dict:
    """Turn a plan state into an ordered, explicit set of officer steps."""
    current, next_action = _INITIAL_RESPONSE_GUIDANCE.get(
        state,
        _INITIAL_RESPONSE_GUIDANCE["deadline_calculating"],
    )
    return {
        "next_action": next_action,
        "steps": [
            {
                "key": key,
                "label": label,
                "state": (
                    "done"
                    if index < current
                    else "current"
                    if index == current
                    else "pending"
                ),
            }
            for index, (key, label) in enumerate(INITIAL_RESPONSE_STEPS)
        ],
    }


#: Sentinel for "no acknowledgment was pre-loaded, go query for it".
_UNSET = object()


def safety_net_automatic_enabled() -> bool:
    """Whether the day-5 safety net could actually deliver right now.

    Both switches are application-managed, so this is a settings-store read
    rather than a constant. A caller rendering a page of cases resolves it
    once and passes it in: a list row must not each pay for the same answer.
    """
    return bool(
        runtime.get("AUTO_ACK_SAFETY_NET")
        and runtime.get("PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED")
    )


def compute_initial_response_plan(
    obj,
    *,
    latest_acknowledgment=_UNSET,
    initial_response_delivered=_UNSET,
    automatic_enabled=_UNSET,
):
    """Return the raw statutory initial-response plan for one request.

    ``latest_acknowledgment`` lets a caller that has already batch-loaded the
    most recent outbound acknowledgment (a cross-case list, for example) pass
    it in instead of paying one query per request. Omitting it keeps the
    single-request behaviour and queries for it here. Either way this is the
    only place the plan states are decided.

    ``automatic_enabled`` does the same for the two application-managed
    switches that decide whether the safety net could deliver at all: a page
    of rows resolves them once instead of reading the settings store per row.

    ``initial_response_delivered`` does the same for the statutory-delivery
    answer. The per-request property costs two queries (the legacy import row
    and the acknowledged/clarification status events), so a cross-case list
    resolves the whole page at once with
    ``apps.records.models.awaiting_initial_response`` -- the same predicate the
    day-5 safety-net worker selects on -- and passes the answer in. A list row
    therefore cannot claim a response was delivered for a case the safety net
    still considers unanswered.
    """
    if automatic_enabled is _UNSET:
        automatic_enabled = safety_net_automatic_enabled()
    automatic_enabled = bool(automatic_enabled)
    plan = {
        "state": "deadline_calculating",
        "due_date": (
            obj.statutory_response_due.isoformat()
            if obj.statutory_response_due
            else None
        ),
        "automatic_check": (
            "Hourly at :30 Pacific on and after the due date."
            if automatic_enabled
            else (
                "Automatic requester correspondence is disabled; prepare "
                "and send a human-approved acknowledgment."
            )
        ),
        "correspondence_id": None,
        "correspondence_status": None,
        "delivery_status": None,
        "automated": None,
    }
    if obj.matter_kind != MatterKind.PUBLIC_RECORDS:
        # There is no RCW 42.56.520 initial response to plan for: this matter
        # is governed by other law and the safety net never sees it (ADR 0004).
        plan["state"] = "not_applicable"
        plan["automatic_check"] = (
            f"A {obj.get_matter_kind_display()} matter owes no RCW 42.56.520 "
            "five-day response."
        )
        return plan
    delivered = (
        obj.initial_response_delivered
        if initial_response_delivered is _UNSET
        else initial_response_delivered
    )
    if delivered:
        plan["state"] = "delivered"
        return plan
    if obj.status == RequestStatus.CLOSED_DUPLICATE:
        # The requester's exchange, and the five-day obligation with it, now
        # belong to the case of record this duplicate was merged into.
        plan["state"] = "merged"
        plan["automatic_check"] = (
            "Merged into another case; the five-day response is tracked "
            "there."
        )
        return plan
    if obj.status == RequestStatus.CLOSED_NON_REQUEST:
        # Nothing was ever owed: staff decided in writing this was not a
        # records request. Closing it without a five-day response is the
        # correct outcome, so this is the one terminal state that may read
        # as settled.
        plan["state"] = "terminal_excluded"
        plan["automatic_check"] = (
            "Terminal requests are excluded from the five-day safety-net worker."
        )
        return plan
    if obj.status in {
        RequestStatus.FULFILLED,
        RequestStatus.CLOSED_WITHDRAWN,
        RequestStatus.CLOSED_ABANDONED,
        RequestStatus.CLOSED_NO_RECORDS,
        RequestStatus.DENIED,
    }:
        # Reaching here means ``delivered`` was false: this request was closed
        # with no RCW 42.56.520 first response on record. That is a live
        # statutory exposure, not a settled case, and it must never be phrased
        # like one. The safety-net worker skips terminal requests
        # (apps/records/tasks.py) and the awaiting-statutory queue selects
        # RECEIVED only, so this state is the sole place the gap can surface.
        plan["state"] = "terminal_unverified"
        plan["automatic_check"] = (
            "Terminal requests are excluded from the five-day safety-net "
            "worker, so no automated response will ever cover this gap."
        )
        return plan

    latest = (
        (
            obj.correspondence.filter(
                direction=Correspondence.Direction.OUTBOUND,
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if latest_acknowledgment is _UNSET
        else latest_acknowledgment
    )
    if latest is not None:
        try:
            delivery_status = latest.delivery.status
        except CorrespondenceDelivery.DoesNotExist:
            delivery_status = None
        plan.update(
            {
                "correspondence_id": str(latest.pk),
                "correspondence_status": latest.status,
                "delivery_status": delivery_status,
                "automated": latest.automated,
            }
        )
        if (
            latest.automated
            and not automatic_enabled
            and latest.status
            in {
                Correspondence.Status.APPROVED,
                Correspondence.Status.QUEUED,
                Correspondence.Status.FAILED,
            }
        ):
            plan["state"] = "automatic_blocked"
            return plan
        if latest.status == Correspondence.Status.DRAFT:
            plan["state"] = "human_draft"
            return plan
        if latest.status == Correspondence.Status.APPROVED:
            plan["state"] = "approved_not_queued"
            return plan
        if (
            latest.status == Correspondence.Status.QUEUED
            and delivery_status
            in {
                CorrespondenceDelivery.Status.PENDING,
                CorrespondenceDelivery.Status.CLAIMED,
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
                CorrespondenceDelivery.Status.RETRYABLE,
            }
        ):
            plan["state"] = "queued"
            return plan
        if latest.status in {
            Correspondence.Status.FAILED,
            Correspondence.Status.CANCELLED,
        } or delivery_status == CorrespondenceDelivery.Status.FAILED:
            plan["state"] = "attention_required"
            return plan

    if obj.statutory_response_due is None:
        return plan
    if not automatic_enabled:
        plan["state"] = "manual_required"
        return plan
    if obj.statutory_response_due > timezone.localdate():
        plan["state"] = "scheduled"
    else:
        plan["state"] = "due_for_safety_net"
    return plan


def initial_response_plan(obj, *, latest_acknowledgment=_UNSET) -> dict:
    """The statutory plan plus the officer guidance that explains it."""
    plan = compute_initial_response_plan(
        obj,
        latest_acknowledgment=latest_acknowledgment,
    )
    plan.update(initial_response_guidance(plan["state"]))
    return plan


class RequestOwnerAssignmentSerializer(serializers.Serializer):
    assigned_officer_id = serializers.PrimaryKeyRelatedField(
        source="assigned_officer",
        queryset=get_user_model().objects.all(),
        required=True,
        allow_null=True,
    )

    def validate_assigned_officer_id(self, value):
        if value is None:
            return value
        if not value.is_active or not has_staff_role(
            value,
            StaffRole.RECORDS_OFFICER,
        ):
            raise serializers.ValidationError(
                "The case owner must be an active Records Officer."
            )
        return value


class TransitionSerializer(serializers.Serializer):
    to_status = serializers.ChoiceField(choices=RequestStatus.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class NonRequestClosureSerializer(serializers.Serializer):
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


#: Every lifecycle refusal the service layer raises, with the documented API
#: code and HTTP status each one carries. Keeping it as data means a new
#: closure endpoint reports the same refusal the same way instead of growing
#: its own five-branch except ladder.
_LIFECYCLE_ERROR_CODES = {
    services.CorrespondenceDeliveryRequired: (
        "correspondence_delivery_required",
        status.HTTP_409_CONFLICT,
    ),
    services.ClarificationResolutionRequired: (
        "clarification_resolution_required",
        status.HTTP_409_CONFLICT,
    ),
    services.TerminalActionRequired: (
        "terminal_action_required",
        status.HTTP_400_BAD_REQUEST,
    ),
    services.ClosedRequestTransition: (
        "closed_request",
        status.HTTP_409_CONFLICT,
    ),
    services.EmailIntakeClosureRequired: (
        "not_email_intake",
        status.HTTP_400_BAD_REQUEST,
    ),
}

_LIFECYCLE_REFUSALS = services.LIFECYCLE_REFUSALS


def _lifecycle_error_response(exc):
    # Walk the MRO rather than keying on the exact type: `except` already
    # catches subclasses, so an exact lookup would turn the first more-specific
    # refusal anyone adds into a KeyError and a 500 instead of its 400 or 409.
    for refusal in type(exc).__mro__:
        entry = _LIFECYCLE_ERROR_CODES.get(refusal)
        if entry is not None:
            code, http_status = entry
            return Response(
                {"code": code, "detail": str(exc)},
                status=http_status,
            )
    raise exc


class CaseClosureSerializer(serializers.Serializer):
    """Close a case as withdrawn or abandoned, with the reason in writing."""

    to_status = serializers.ChoiceField(
        choices=sorted(services.STAFF_CLOSABLE_STATUSES),
    )
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


class IntakeFeedbackSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=AdvisoryFeedback.Outcome.choices)
    final_label = serializers.ChoiceField(choices=sorted(INTAKE_LABELS))
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class MergeRequestSerializer(serializers.Serializer):
    """A merge names the case of record and says why, in writing."""

    target_number = serializers.CharField(max_length=20, trim_whitespace=True)
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


class ExternalInitialResponseSerializer(serializers.Serializer):
    """An officer's statement that the first response went out elsewhere."""

    sent_via = serializers.ChoiceField(
        choices=InitialResponseAttestation.SentVia.choices,
    )
    responded_on = serializers.DateField(required=False, allow_null=True)
    evidence = serializers.CharField(
        min_length=services.MIN_ATTESTATION_EVIDENCE,
        max_length=2000,
        trim_whitespace=True,
    )


class FerpaIdentityVerificationSerializer(serializers.Serializer):
    """An officer's record that the FERPA requester is who they claim to be."""

    method = serializers.ChoiceField(
        choices=FerpaIdentityVerificationMethod.choices,
    )
    note = serializers.CharField(
        min_length=services.MIN_FERPA_IDENTITY_NOTE,
        max_length=2000,
        trim_whitespace=True,
    )


class InboxHandledSerializer(serializers.Serializer):
    """How many requester-email rows the case had waiting; they are now Handled."""

    handled = serializers.IntegerField(min_value=0, read_only=True)


class ReopenRequestSerializer(serializers.Serializer):
    """A reopen names where the case is going and says why, in writing."""

    to_status = serializers.ChoiceField(
        choices=sorted(services.REOPENABLE_TARGET_STATUSES),
    )
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


class CaseWorkspaceUserSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    username = serializers.CharField()
    display_name = serializers.CharField()


class CaseWorkspaceRequesterSerializer(serializers.Serializer):
    name = serializers.CharField()
    email = serializers.EmailField()


class RequesterIdentityCorrectionSerializer(serializers.Serializer):
    """Correct who the record says asked, in writing and on the record."""

    name = serializers.CharField(max_length=255, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


class CorrectedCorrespondenceSerializer(serializers.Serializer):
    """What the correction did to one unsent outbound message."""

    id = serializers.CharField()
    kind = serializers.CharField()
    status = serializers.CharField()
    subject = serializers.CharField(allow_blank=True)
    detail = serializers.CharField(allow_blank=True, required=False)


class RequesterIdentityCorrectionResultSerializer(serializers.Serializer):
    request = PublicRecordsRequestDetailSerializer()
    previous_requester = CaseWorkspaceRequesterSerializer()
    requester = CaseWorkspaceRequesterSerializer()
    refreshed_correspondence = CorrectedCorrespondenceSerializer(many=True)
    correspondence_needing_review = CorrectedCorrespondenceSerializer(many=True)
    cancelled_correspondence = CorrectedCorrespondenceSerializer(many=True)
    uncancelled_correspondence = CorrectedCorrespondenceSerializer(many=True)


def _corrected_correspondence_payload(item, detail=""):
    payload = {
        "id": str(item.id),
        "kind": item.kind,
        "status": item.status,
        "subject": item.draft_subject or item.approved_subject or "",
    }
    if detail:
        payload["detail"] = detail
    return payload


class CaseWorkspaceStatutoryStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    state = serializers.ChoiceField(choices=["done", "current", "pending"])


class CaseWorkspaceResponseStateSerializer(serializers.Serializer):
    state = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)
    next_action = serializers.CharField(allow_blank=True)
    # The Messages tab renders the same tracker as Overview, so the officer
    # sees which step is outstanding on the screen that performs it. A field
    # missing here is dropped silently, not rejected.
    steps = CaseWorkspaceStatutoryStepSerializer(many=True)
    correspondence_id = serializers.CharField(allow_null=True)


class CaseWorkspaceCountsSerializer(serializers.Serializer):
    new = serializers.IntegerField(min_value=0)
    needs_attention = serializers.IntegerField(min_value=0)


CASE_WORKSPACE_TABS = (
    "overview",
    "messages",
    "collection",
    "records",
    "releases",
    "activity",
)


class CaseWorkspaceNextActionSerializer(serializers.Serializer):
    label = serializers.CharField()
    detail = serializers.CharField()
    tab = serializers.ChoiceField(choices=CASE_WORKSPACE_TABS)
    work_item_id = serializers.UUIDField()


class CaseWorkspaceSummarySerializer(serializers.Serializer):
    number = serializers.CharField()
    legal_status = serializers.CharField()
    requester = CaseWorkspaceRequesterSerializer(allow_null=True)
    owner = CaseWorkspaceUserSerializer(allow_null=True)
    statutory_response = CaseWorkspaceResponseStateSerializer()
    estimated_completion = serializers.DateField(allow_null=True)
    counts = CaseWorkspaceCountsSerializer()
    next_action = CaseWorkspaceNextActionSerializer(allow_null=True)


class CaseActivityEntrySerializer(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.ChoiceField(choices=["lifecycle", "work", "delivery"])
    occurred_at = serializers.DateTimeField()
    label = serializers.CharField()
    actor_username = serializers.CharField(allow_null=True)
    detail = serializers.CharField(allow_null=True)
    work_item_id = serializers.UUIDField(allow_null=True)
    delivery_status = serializers.CharField(allow_null=True)
    flagged = serializers.BooleanField()


class CaseActivitySerializer(serializers.Serializer):
    results = CaseActivityEntrySerializer(many=True)


class CaseClosureBlockerSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField(min_value=1)
    tab = serializers.ChoiceField(choices=CASE_WORKSPACE_TABS)


class CaseClosureReadinessSerializer(serializers.Serializer):
    ready = serializers.BooleanField()
    counts = serializers.DictField(child=serializers.IntegerField(min_value=0))
    blockers = CaseClosureBlockerSerializer(many=True)


class JlarcRequestMetricsSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username",
        read_only=True,
    )

    class Meta:
        model = JlarcRequestMetrics
        fields = [
            "id",
            "revision",
            "requester_type",
            "fulfillment_format",
            "scanned_physical",
            "records_produced_description",
            "records_withheld_description",
            "reason",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = fields


class JlarcRequestMetricsInputSerializer(serializers.Serializer):
    requester_type = serializers.ChoiceField(
        choices=JlarcRequestMetrics.RequesterType.choices,
    )
    fulfillment_format = serializers.ChoiceField(
        choices=JlarcRequestMetrics.FulfillmentFormat.choices,
    )
    scanned_physical = serializers.BooleanField()
    records_produced_description = serializers.CharField(max_length=5000)
    records_withheld_description = serializers.CharField(max_length=5000)
    reason = serializers.CharField(min_length=10, max_length=1000)


class StaffIntakeSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()
    channel = serializers.ChoiceField(
        choices=(Channel.MAIL, Channel.IN_PERSON, Channel.PHONE),
    )
    # Which law governs the matter, decided by the staff member opening it and
    # fixed from then on (ADR 0004). Omitted means a public records request,
    # so every existing caller keeps its meaning.
    matter_kind = serializers.ChoiceField(
        choices=MatterKind.choices,
        required=False,
        default=MatterKind.PUBLIC_RECORDS,
    )
    received_at = serializers.DateTimeField()
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    street_address = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
    )
    city = serializers.CharField(max_length=255, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_code = serializers.CharField(
        max_length=32,
        required=False,
        allow_blank=True,
    )
    company = serializers.CharField(max_length=255, required=False, allow_blank=True)
    description = serializers.CharField(max_length=100_000)
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
    )

    def validate_received_at(self, value):
        if value > timezone.now() + dt.timedelta(minutes=5):
            raise serializers.ValidationError(
                "The legal receipt time cannot be in the future."
            )
        return value

    def validate(self, attrs):
        if not any(
            str(attrs.get(field, "")).strip()
            for field in ("email", "phone", "street_address")
        ):
            raise serializers.ValidationError(
                "Provide at least one response method: email, phone, or mailing address."
            )
        return attrs


class StaffIntakeResponseSerializer(serializers.Serializer):
    number = serializers.CharField()




#: The date a case is answerable by *next*, annotated by
#: :func:`apps.records.response_due.annotate_next_response_due`.
#:
#: Not the RCW 42.56.520 date. That date answers whether the first response was
#: made in time and is spent once it has been; ranking an acknowledged case by
#: it puts a statutory deadline on a screen for a case that no longer owes one.
#: See ``apps/records/response_due.py`` for which basis each matter kind gets.
CASE_DUE_DATE = F("_next_due")


#: Column key -> the expressions that sort it ascending, before direction is
#: applied. A whitelist rather than passthrough so a query parameter can never
#: order by an unindexed or private column.
_CASE_ORDERING_FIELDS = {
    "number": (Lower("number"),),
    # Name first, then address: two requesters can share neither, but a
    # requester recorded by email alone has no name to sort by.
    "requester": (Lower("requester__name"), Lower("requester__email")),
    "subject": (Lower("_subject_sort"),),
    "status": (Lower("status"),),
    "received": (F("received_at"),),
    # The column shows whichever date the case owes next -- statutory, the
    # estimate promised to the requester, the district target, or a FERPA
    # access date -- so it sorts by the date it shows.
    "due": (CASE_DUE_DATE,),
    "estimated": (F("estimated_completion"),),
    "assignee": (Lower("assigned_officer__username"),),
}

#: Sort keys that read requester identity. Ordering by a value the response
#: blanks is still a reading of it, one comparison at a time, so these are
#: Records Officer only -- the same boundary the list serializer draws.
_IDENTITY_ORDERINGS = frozenset({"requester", "-requester"})


def _build_case_orderings():
    """Expand the column spec into ascending and descending orderings.

    Every column is ordered nulls-last in both directions deliberately:
    SQLite and Postgres disagree about where NULL sorts by default, and a
    deadline column that puts the undated cases on top under test and at the
    bottom in production is a compliance surface, not a cosmetic one. The
    primary key is appended as a tiebreak so paging through a column with
    repeated values cannot show or skip a row.
    """
    orderings = {}
    for key, fields in _CASE_ORDERING_FIELDS.items():
        orderings[key] = (
            *(field.asc(nulls_last=True) for field in fields), "pk",
        )
        orderings[f"-{key}"] = (
            *(field.desc(nulls_last=True) for field in fields), "-pk",
        )
    return orderings


CASE_ORDERINGS = _build_case_orderings()


class RecordUploadResultSerializer(serializers.Serializer):
    filename = serializers.CharField()
    outcome = serializers.ChoiceField(choices=RECORD_UPLOAD_OUTCOMES)
    detail = serializers.CharField(allow_blank=True)
    record_item_id = serializers.UUIDField(allow_null=True)
    sha256 = serializers.CharField(allow_blank=True)
    size = serializers.IntegerField()


class RecordUploadResponseSerializer(serializers.Serializer):
    added_count = serializers.IntegerField()
    results = RecordUploadResultSerializer(many=True)


def _scoping_data(session) -> dict:
    from apps.records.models import ScopeIteration

    iterations = list(
        ScopeIteration.objects.filter(session=session).select_related("count_run").order_by("number")
    )
    final = next(
        (i for i in reversed(iterations) if i.count_run.state == "complete"), None,
    )

    def row(iteration):
        run = iteration.count_run
        return {
            "number": iteration.number,
            "count_run_id": str(run.pk),
            "input_sha256": run.input_sha256,
            "reason": iteration.reason,
            "query": run.terms,
            "search_scope": run.search_scope,
            "custodians": iteration.custodians,
            "state": run.state,
            "total": run.total_count,
            "top_accounts": run.account_counts[:5],
            "date_range_start": run.date_range_start,
            "date_range_end": run.date_range_end,
        }

    return {
        "id": session.pk,
        "trigger": session.trigger,
        "actor": getattr(session.actor, "email", None),
        "reason": session.reason,
        "state": session.state,
        "stop_reason": session.stop_reason,
        "max_iterations": session.max_iterations,
        "created_at": session.created_at,
        "iterations": [row(i) for i in iterations],
        "final": row(final) if final else None,
    }


class RequestsViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """List, retrieve, and partially update retained compliance records.

    Intake has a separate least-privilege integration boundary. Custom actions
    drive status changes through the service layer, never the model directly.
    Estimate revisions use the durable correspondence resource.
    """
    lookup_field = "number"
    http_method_names = ["get", "patch", "post", "head", "options"]
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "partial_update": {StaffRole.RECORDS_OFFICER},
        "intake": {StaffRole.INTAKE_CLERK, StaffRole.RECORDS_OFFICER},
        "transition": {StaffRole.RECORDS_OFFICER},
        # Recording that a requester withdrew, or that a documented
        # clarification period expired, is part of working a case rather than
        # a legal determination about the records. Everyone who works cases
        # here may do it; the substantive reason and the audit entry are what
        # make it accountable. Every other closure stays with the officer.
        "close_case": WORKFLOW_STAFF_ROLES,
        "close_non_request": {StaffRole.RECORDS_OFFICER},
        # Correcting who the record says asked changes who every statutory
        # notice is addressed to, so it stays with the Records Officer.
        "correct_requester": {StaffRole.RECORDS_OFFICER},
        # Reopening reverses a written closure decision, so it sits with
        # the other Records Officer-only lifecycle actions.
        "reopen": {StaffRole.RECORDS_OFFICER},
        # A merge closes one case and moves a requester's correspondence;
        # marking a case caught up disposes its Inbox rows. Both are the
        # officer's call.
        "merge": {StaffRole.RECORDS_OFFICER},
        "inbox_handled": {StaffRole.RECORDS_OFFICER},
        # A written statement about the statutory first response.
        "external_initial_response": {StaffRole.RECORDS_OFFICER},
        # Deciding a parent is the right parent (34 CFR 99.10).
        "ferpa_identity_verification": {StaffRole.RECORDS_OFFICER},
        "jlarc_classification": {StaffRole.RECORDS_OFFICER},
        "advisory_intent": {StaffRole.RECORDS_OFFICER},
        "advisory_intake": {StaffRole.RECORDS_OFFICER},
        "advisory_intake_feedback": {StaffRole.RECORDS_OFFICER},
        # Sending it is approve_and_send, which is the officer's too.
        "estimate_letter": {StaffRole.RECORDS_OFFICER},
        "vault_counts": {StaffRole.RECORDS_OFFICER},
        "scoping": {StaffRole.RECORDS_OFFICER},
        "rescope": {StaffRole.RECORDS_OFFICER},
        "vault_collection": {StaffRole.RECORDS_OFFICER},
        # Adding candidate records to a case is collection, which is the
        # officer's: liaisons hand theirs over through a collection task.
        "record_uploads": {StaffRole.RECORDS_OFFICER},
        "workspace_summary": WORKFLOW_STAFF_ROLES,
        # Everyone who works a case keeps and reads its running record. The
        # note never reaches the requester, so the boundary that matters is
        # the one that decides who may open the case at all, and
        # ``get_object`` already applies it.
        "notes": WORKFLOW_STAFF_ROLES,
        "activity": WORKFLOW_STAFF_ROLES,
        "closure_readiness": {
            StaffRole.RECORDS_OFFICER,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        },
    }

    def initialize_request(self, request, *args, **kwargs):
        content_type = request.META.get("CONTENT_TYPE", "")
        if (
            request.method == "POST"
            and content_type.partition(";")[0].strip().lower()
            == "multipart/form-data"
        ):
            path = request.path.rstrip("/")
            # The limit handler must run before Django spools a single byte,
            # so it is chosen from the path here rather than in the action.
            if path.endswith("/requests/intake"):
                request.upload_handlers.insert(
                    0,
                    IntakeAttachmentLimitUploadHandler(request),
                )
            elif path.endswith("/record-uploads"):
                request.upload_handlers.insert(
                    0,
                    RecordUploadLimitUploadHandler(request),
                )
        return super().initialize_request(request, *args, **kwargs)

    def _requested_ordering(self):
        """The whitelisted ordering this caller asked for, or ``None``.

        ``None`` means "use the view's own default", which is what an unknown
        key and a refused identity sort both fall back to: a list that is
        ordered by something defensible is better than an error, and worse
        than silence would be ordering by a column this caller may not read.
        """
        key = (self.request.query_params.get("ordering") or "").strip()
        if not key:
            return None
        if key in _IDENTITY_ORDERINGS and not has_staff_role(
            self.request.user, StaffRole.RECORDS_OFFICER
        ):
            return None
        return CASE_ORDERINGS.get(key)

    def _annotate_subject(self, qs):
        """Expose the latest completed advisory title for the case.

        Two correlated subqueries for the whole page rather than a run lookup
        per row. Advisory only: this is a scanning label the model proposed
        and no human adopted, it is never written back to the request, and
        ``AdvisoryRun.applied`` stays false by database constraint.

        A dedicated ``case_title`` run wins over the ``short_title`` that a
        request-intent run happens to carry. The title run exists for this
        column and is prepared for every case; the intent run's title is a
        by-product of an analysis an officer asked for, so it is a fallback
        rather than the source.

        Records Officer only, the same boundary as the advisory actions that
        own this output. Everyone else gets the description column, in both
        the cell and the sort, so neither becomes a second and wider way to
        read an advisory run.
        """
        if has_staff_role(self.request.user, StaffRole.RECORDS_OFFICER):

            def latest_title(kind, key):
                runs = AdvisoryRun.objects.filter(
                    request=OuterRef("pk"),
                    kind=kind,
                    state=AdvisoryRun.State.COMPLETE,
                # Two runs can share a timestamp; the primary key keeps SQLite
                # and Postgres agreeing about which one is the latest.
                ).order_by("-created_at", "-pk")
                return Subquery(
                    runs.annotate(
                        _value=KeyTextTransform(key, "proposal"),
                    ).values("_value")[:1],
                    output_field=CharField(),
                )

            advisory_title = Coalesce(
                NullIf(latest_title(AdvisoryRun.Kind.CASE_TITLE, "title"), Value("")),
                NullIf(
                    latest_title(
                        AdvisoryRun.Kind.REQUEST_INTENT, "short_title"
                    ),
                    Value(""),
                ),
                Value(""),
                output_field=CharField(),
            )
        else:
            advisory_title = Value("", output_field=CharField())
        qs = qs.annotate(_advisory_short_title=advisory_title)
        # Sorting by the column the officer actually reads: the advisory title
        # where there is one, the description underneath it where there is
        # not. Ordering by the raw description rather than the rendered
        # snippet is the same order, because the snippet only ever truncates.
        return qs.annotate(
            _subject_sort=Coalesce(
                NullIf(F("_advisory_short_title"), Value("")),
                F("description"),
                output_field=CharField(),
            ),
        )

    def _annotate_latest_note(self, qs):
        """Carry the newest internal note's text, author, and time per row.

        Three correlated subqueries over the whole page rather than a note
        query per row: the alternative is fifty-one queries to render a
        fifty-case list, which is the shape of slowness that makes a work
        queue stop being used.
        """
        newest = CaseNote.objects.filter(request=OuterRef("pk")).order_by(
            # Two notes can share a timestamp; the primary key keeps SQLite
            # and PostgreSQL agreeing about which one is newest.
            "-created_at", "-pk",
        )
        return qs.annotate(
            _latest_note_body=Subquery(
                newest.values("body")[:1], output_field=TextField()
            ),
            _latest_note_author=Subquery(
                newest.values("author__username")[:1],
                output_field=CharField(),
            ),
            _latest_note_at=Subquery(
                newest.values("created_at")[:1],
                output_field=DateTimeField(),
            ),
        )

    def get_queryset(self):
        qs = PublicRecordsRequest.objects.select_related(
            # The last two carry the first-response evidence that both
            # ``initial_response_delivered`` and the next-due basis read. They
            # are one-to-ones, so joining them costs one query rather than one
            # per row on a fifty-case page.
            "requester",
            "assigned_officer",
            "legacy_import",
            "initial_response_attestation",
        )
        qs = annotate_next_response_due(qs)
        qs = self._annotate_subject(qs)
        qs = self._annotate_latest_note(qs)
        if self.action not in {
            "workspace_summary",
            "activity",
            "closure_readiness",
        }:
            qs = qs.prefetch_related(
                "events__actor", "estimate_revisions__revised_by",
                "collection_tasks__system", "footage_preservation",
                Prefetch(
                    "intake_attachments",
                    queryset=IntakeAttachment.objects.select_related(
                        "source_correspondence_attachment"
                    ),
                ),
            )
        qs = scope_case_queryset(qs, self.request.user)
        status_param = self.request.query_params.get("status")
        if status_param and self.request.query_params.get("queue") == "inbox":
            # The open-cases queue is already one status band. A status on
            # top of it could only narrow it silently, and "closed" would
            # empty it, so refuse the combination the way work items refuse
            # a disposition with a named queue.
            raise serializers.ValidationError(
                {"status": ["Do not combine status with queue=inbox."]}
            )
        # "open" and "closed" are the two groups staff actually ask for; no
        # legal status carries either name, so they cannot shadow one.
        if status_param == "open":
            qs = qs.exclude(status__in=services.CLOSED_STATUSES)
        elif status_param == "closed":
            qs = qs.filter(status__in=services.CLOSED_STATUSES)
        elif status_param:
            qs = qs.filter(status=status_param)
        search = self.request.query_params.get("search")
        if search:
            search_query = (
                Q(number__icontains=search)
                | Q(description__icontains=search)
                # Staff know migrated cases by the number the old system gave
                # them, not the one this system assigned: what NextRequest
                # called 25-42 is PRR-2026-0043 here. Searching the number a
                # person actually remembers has to find the case. The source
                # number is an identifier the district issued, not requester
                # PII, so every role that may see the case may search it.
                | Q(legacy_import__source_number__icontains=search)
            )
            if has_staff_role(
                self.request.user, StaffRole.RECORDS_OFFICER
            ):
                # Requester identity stays officer-only, matching the list
                # serializer, which blanks name and email for everyone else.
                # Name belongs here for the same reason email does: it is how
                # an officer refers to a requester who has called back.
                search_query |= Q(requester__email__icontains=search)
                search_query |= Q(requester__name__icontains=search)
            qs = qs.filter(search_query)
        ordering = self._requested_ordering()
        if self.request.query_params.get("queue") == "inbox":
            # The per-case Inbox: every open case once, with the number of
            # requester emails nobody has handled yet. Web-form cases have no
            # email rows at all, which is exactly why the Inbox has to start
            # from cases rather than from messages.
            open_inbox_rows = Q(
                work_items__work_kind__in=INBOX_MESSAGE_WORK_KINDS,
                work_items__disposition=WorkItem.Disposition.NEEDS_ATTENTION,
            )
            qs = qs.exclude(status__in=services.CLOSED_STATUSES).annotate(
                _inbox_needs_attention=Count(
                    "work_items", filter=open_inbox_rows, distinct=True,
                ),
                _latest_inbound_at=Max(
                    "work_items__created_at", filter=open_inbox_rows,
                ),
            )
            # An explicit column sort replaces the flagged-first default
            # outright rather than sorting within it. A "sorted by received
            # date" table whose rows are still grouped by a flag underneath is
            # a table that lies about its own header.
            if ordering:
                return qs.order_by(*ordering)
            return qs.order_by(
                Case(
                    When(_inbox_needs_attention__gt=0, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                ),
                CASE_DUE_DATE.asc(nulls_last=True),
                "received_at",
                "pk",
            )
        if ordering:
            return qs.order_by(*ordering)
        return qs.order_by(CASE_DUE_DATE.asc(nulls_last=True), "pk")

    def get_serializer_class(self):
        if self.action == "intake":
            return StaffIntakeSerializer
        if self.action == "list":
            return PublicRecordsRequestListSerializer
        return PublicRecordsRequestDetailSerializer

    @extend_schema(
        request=RequestOwnerAssignmentSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    def partial_update(self, request, *args, **kwargs):
        """Assign case ownership through locked, append-only evidence."""
        prr = self.get_object()
        assignment = RequestOwnerAssignmentSerializer(data=request.data)
        assignment.is_valid(raise_exception=True)
        try:
            updated = services.assign_request_owner(
                prr,
                owner=assignment.validated_data["assigned_officer"],
                actor=request.user,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError({
                "assigned_officer_id": exc.messages,
            }) from exc
        return Response(
            PublicRecordsRequestDetailSerializer(
                updated,
                context=self.get_serializer_context(),
            ).data
        )

    @staticmethod
    def _next_action_tab(work_kind, user):
        if work_kind == WorkItem.WorkKind.REQUEST_ACTION:
            return "overview"
        if work_kind in {
            WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
            WorkItem.WorkKind.CORRESPONDENCE_APPROVAL,
            WorkItem.WorkKind.INBOUND_REPLY,
            WorkItem.WorkKind.EMAIL_INTAKE,
            WorkItem.WorkKind.INBOUND_UNMATCHED,
            WorkItem.WorkKind.INBOUND_CANDIDATE,
            WorkItem.WorkKind.INBOUND_AMBIGUOUS,
            WorkItem.WorkKind.INBOUND_QUARANTINED,
        }:
            return "messages"
        if work_kind == WorkItem.WorkKind.DELIVERY_EXCEPTION:
            if has_staff_role(user, StaffRole.RECORDS_OFFICER):
                return "messages"
            return "overview"
        if work_kind == WorkItem.WorkKind.COLLECTION:
            return "collection"
        if work_kind in {
            WorkItem.WorkKind.RECORD_REVIEW,
            WorkItem.WorkKind.SECOND_REVIEW,
        }:
            return "records"
        return "activity"

    def _scoped_open_work(self, prr):
        return role_scoped_work_items(
            annotate_work_item_sources(
                WorkItem.objects.filter(
                    request=prr,
                    disposition__in=UNRESOLVED_DISPOSITIONS,
                )
            ).select_related("assignee", "request"),
            self.request.user,
        )

    @extend_schema(responses={200: CaseWorkspaceSummarySerializer})
    @action(detail=True, methods=["get"], url_path="workspace-summary")
    def workspace_summary(self, request, number=None):
        """Return one bounded, role-safe case header without changing state."""
        prr = self.get_object()
        scoped_work = self._scoped_open_work(prr)
        next_work = order_work_items_for_next_action(scoped_work).first()
        new_count = 0
        if request.user.is_staff:
            current_user_read = WorkItemReceipt.objects.filter(
                work_item=OuterRef("pk"),
                user=request.user,
                read_at__isnull=False,
            )
            new_count = scoped_work.annotate(
                _current_user_read=Exists(current_user_read),
            ).filter(_current_user_read=False).count()

        requester = None
        if has_staff_role(request.user, StaffRole.RECORDS_OFFICER):
            requester = {
                "name": prr.requester.name,
                "email": prr.requester.email,
            }
        owner = None
        if request.user.is_staff and prr.assigned_officer is not None:
            owner = {
                "id": prr.assigned_officer_id,
                "username": prr.assigned_officer.username,
                "display_name": (
                    prr.assigned_officer.get_full_name()
                    or prr.assigned_officer.username
                ),
            }
        response_plan = initial_response_plan(prr)
        payload = {
            "number": prr.number,
            "legal_status": prr.status,
            "requester": requester,
            "owner": owner,
            "statutory_response": {
                "state": response_plan["state"],
                "due_date": response_plan["due_date"],
                "next_action": response_plan["next_action"],
                # The Messages tab renders the same four-step tracker the
                # Overview tab does, so the officer sees which step is
                # outstanding on the screen where the step is performed.
                "steps": response_plan["steps"],
                "correspondence_id": response_plan["correspondence_id"],
            },
            "estimated_completion": prr.estimated_completion,
            "counts": {
                "new": new_count,
                "needs_attention": scoped_work.filter(
                    disposition=WorkItem.Disposition.NEEDS_ATTENTION,
                ).count(),
            },
            "next_action": (
                {
                    "label": (
                        "Records Officer handoff required"
                        if (
                            next_work.work_kind
                            == WorkItem.WorkKind.DELIVERY_EXCEPTION
                            and not has_staff_role(
                                request.user,
                                StaffRole.RECORDS_OFFICER,
                            )
                        )
                        else NEXT_ACTION_LABELS[next_work.work_kind]
                    ),
                    "detail": (
                        "Contact the case owner to resolve the delivery exception."
                        if (
                            next_work.work_kind
                            == WorkItem.WorkKind.DELIVERY_EXCEPTION
                            and not has_staff_role(
                                request.user,
                                StaffRole.RECORDS_OFFICER,
                            )
                        )
                        else "The earliest role-scoped open work item."
                    ),
                    "tab": self._next_action_tab(
                        next_work.work_kind,
                        request.user,
                    ),
                    "work_item_id": next_work.pk,
                }
                if next_work is not None
                else None
            ),
        }
        return Response(CaseWorkspaceSummarySerializer(payload).data)

    @extend_schema(
        request=CaseNoteCreateSerializer,
        responses={200: CaseNoteSerializer(many=True), 201: CaseNoteSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="notes")
    def notes(self, request, number=None):
        """Read or append this case's internal staff notes.

        Internal only. A note never reaches the requester: no letter, no
        production manifest, and no export reads ``CaseNote``, which
        ``apps/records/tests/test_case_notes.py`` asserts against the real
        drafting and production paths.

        Scoped by ``get_object`` -- so the same rule that decides whether this
        caller may open the case decides whether they may read or write its
        notes -- and available to every workflow role, because everybody who
        works a case needs the running record of it. Appending is the only
        write: notes are append-only in the ORM and in the database.
        """
        prr = self.get_object()
        if request.method == "GET":
            # Newest first: the list answers "what happened last", and the
            # whole history is one scroll below it.
            queryset = (
                prr.case_notes.select_related("author")
                .order_by("-created_at", "-pk")
            )
            page = self.paginate_queryset(queryset)
            serializer = CaseNoteSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        payload = CaseNoteCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            note = services.add_case_note(
                prr, actor=request.user, body=payload.validated_data["body"]
            )
        except DjangoValidationError as exc:
            # The serializer and the service bound a note the same way today,
            # so nothing normally reaches this. That is two numbers agreeing,
            # not a guarantee: Django's ValidationError is not one DRF knows,
            # so without this the next rule added to the service would surface
            # as a 500 rather than as a refusal.
            return Response(
                {"code": "invalid", "detail": "; ".join(exc.messages)},
                status=400,
            )
        return Response(CaseNoteSerializer(note).data, status=201)

    @extend_schema(responses={200: CaseActivitySerializer})
    @action(detail=True, methods=["get"])
    def activity(self, request, number=None):
        """Compose allowlisted lifecycle, work, and delivery milestones."""
        prr = self.get_object()
        roles = staff_roles(request.user)
        officer = StaffRole.RECORDS_OFFICER in roles
        status_labels = dict(RequestStatus.choices)
        entries = []

        # The newest hundred, plus every compliance-marked note regardless of
        # age. Capping in the query alone silently drops those notes: the
        # importer backdates them to the request's receipt, so on a case with
        # any history they sort past the cap and no amount of reordering
        # afterwards can bring back a row that was never fetched.
        recent_events = list(
            prr.events.select_related("actor").order_by("-occurred_at", "-pk")[:100]
        )
        seen_event_pks = {event.pk for event in recent_events}
        marker_match = Q()
        for marker in COMPLIANCE_NOTE_MARKERS:
            marker_match |= Q(note__contains=marker)
        flagged_events = list(
            prr.events.select_related("actor")
            .filter(marker_match)
            .exclude(pk__in=seen_event_pks)
            .order_by("-occurred_at", "-pk")[:100]
        )
        for event in recent_events + flagged_events:
            if not event.from_status and not event.to_status:
                # A note-only event records something that happened around the
                # case, not a change of legal state. Rendering it as
                # "New request → " would assert a transition that never
                # occurred, on an append-only record.
                label = "Operational note"
            else:
                from_label = status_labels.get(event.from_status, "New request")
                to_label = status_labels.get(event.to_status, event.to_status)
                label = f"{from_label} → {to_label}"
            entries.append({
                "id": f"lifecycle:{event.pk}",
                "type": "lifecycle",
                "occurred_at": event.occurred_at,
                "label": label,
                "actor_username": (
                    (event.actor.username if event.actor else "system")
                    if officer
                    else None
                ),
                "detail": event.note if officer else None,
                "work_item_id": None,
                "delivery_status": None,
                "flagged": compliance_flagged_note(event.note),
            })

        scoped_items = role_scoped_work_items(
            WorkItem.objects.filter(request=prr),
            request.user,
        )
        work_events = WorkItemEvent.objects.filter(
            work_item__in=scoped_items,
        ).select_related("actor", "work_item").order_by(
            "-occurred_at",
            "-pk",
        )[:100]
        for event in work_events:
            entries.append({  # noqa: PERF401 - two sources, one ordered timeline
                "id": f"work:{event.pk}",
                "type": "work",
                "occurred_at": event.occurred_at,
                "label": (
                    f"{NEXT_ACTION_LABELS[event.work_item.work_kind]}: "
                    f"{event.get_event_type_display()}"
                ),
                "actor_username": (
                    (event.actor.username if event.actor else "system")
                    if officer
                    else None
                ),
                "detail": event.metadata.get("reason") if officer else None,
                "work_item_id": event.work_item_id,
                "delivery_status": None,
                "flagged": False,
            })

        if roles.intersection({
            StaffRole.RECORDS_OFFICER,
            StaffRole.LIAISON,
            StaffRole.ADMIN,
        }):
            deliveries = CorrespondenceDelivery.objects.filter(
                correspondence__request=prr,
            ).select_related(
                "correspondence",
                "correspondence__queued_by",
            ).order_by("-queued_at", "-pk")[:100]
            for delivery in deliveries:
                correspondence = delivery.correspondence
                entries.append({
                    "id": f"delivery:{delivery.pk}:queued",
                    "type": "delivery",
                    "occurred_at": delivery.queued_at,
                    "label": f"{correspondence.get_kind_display()} delivery queued",
                    "actor_username": (
                        correspondence.queued_by.username
                        if officer and correspondence.queued_by
                        else None
                    ),
                    "detail": None,
                    "work_item_id": None,
                    "delivery_status": "queued",
                    "flagged": False,
                })
            attempts = CorrespondenceDeliveryAttempt.objects.filter(
                delivery__correspondence__request=prr,
                result_at__isnull=False,
            ).select_related(
                "actor",
                "delivery__correspondence",
            ).order_by("-result_at", "-pk")[:100]
            for attempt in attempts:
                correspondence = attempt.delivery.correspondence
                entries.append({
                    "id": f"delivery-attempt:{attempt.pk}",
                    "type": "delivery",
                    "occurred_at": attempt.result_at,
                    "label": (
                        f"{correspondence.get_kind_display()} delivery attempt "
                        f"{attempt.get_result_category_display().lower()}"
                    ),
                    "actor_username": (
                        attempt.actor.username
                        if officer and attempt.actor
                        else None
                    ),
                    "detail": None,
                    "work_item_id": None,
                    "delivery_status": attempt.result_category,
                    "flagged": False,
                })
        entries.sort(key=lambda row: row["occurred_at"], reverse=True)
        # A plain newest-first cut buries the migration compliance notes: the
        # importer backdates them to the request's receipt date, so any case
        # with routine activity since pushes them past the cap. Seat those
        # entries first, then fill the rest by recency, then restore
        # chronological order so the feed still reads as a timeline.
        flagged = [row for row in entries if row["flagged"]]
        seated = {row["id"] for row in flagged}
        recent = [row for row in entries if row["id"] not in seated]
        results = flagged + recent[: max(0, 100 - len(flagged))]
        results.sort(key=lambda row: row["occurred_at"], reverse=True)
        return Response(CaseActivitySerializer({"results": results}).data)

    @extend_schema(responses={200: CaseClosureReadinessSerializer})
    @action(detail=True, methods=["get"], url_path="closure-readiness")
    def closure_readiness(self, request, number=None):
        """Report blockers only; lifecycle and correspondence services decide."""
        prr = self.get_object()
        released_installment_coverage = Installment.objects.filter(
            request=prr,
            released_at__isnull=False,
            items=OuterRef("pk"),
        )
        confirmed_release_notice = CorrespondenceDeliveryAttempt.objects.filter(
            delivery__correspondence__request=prr,
            delivery__correspondence__kind=(
                Correspondence.Kind.INSTALLMENT_NOTICE
            ),
            delivery__correspondence__status=Correspondence.Status.SENT,
            result_at__isnull=False,
            result_category__in=[
                CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
            ],
        ).annotate(
            _installment_id_text=KeyTextTransform(
                "installment_id",
                "delivery__correspondence__approved_metadata",
            ),
        ).filter(
            _installment_id_text=Cast(
                OuterRef("pk"),
                output_field=CharField(),
            ),
        )
        counts = {
            "open_tasks": prr.collection_tasks.exclude(
                state=CollectionTask.State.COMPLETE,
            ).count(),
            "unresolved_clarifications": prr.clarification_cycles.filter(
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            ).count(),
            "unreviewed_records": prr.record_items.filter(
                review_state=RecordItem.ReviewState.UNREVIEWED,
            ).count(),
            "pending_second_review": prr.record_items.filter(
                second_reviewed_at__isnull=True,
            ).filter(
                Q(
                    review_state=RecordItem.ReviewState.WITHHELD,
                    withholding_exemption__requires_second_review=True,
                )
                | Q(
                    review_state=RecordItem.ReviewState.WITHHELD,
                    withholding_exemptions__requires_second_review=True,
                )
                | Q(
                    redactions__voided_at__isnull=True,
                    redactions__exemption__requires_second_review=True,
                )
            ).distinct().count(),
            "responsive_records_unreleased": prr.record_items.filter(
                review_state=RecordItem.ReviewState.RESPONSIVE,
            ).annotate(
                _has_released_installment=Exists(released_installment_coverage),
            ).filter(_has_released_installment=False).count(),
            "fee_installment_blockers": prr.installments.filter(
                released_at__isnull=True,
            ).count(),
            "open_deposit_gates": prr.fee_deposit_gates.filter(
                satisfied_at__isnull=True,
                lifted_at__isnull=True,
            ).count(),
            "undelivered_release_notices": prr.installments.filter(
                released_at__isnull=False,
            ).annotate(
                _has_confirmed_delivery=Exists(confirmed_release_notice),
            ).filter(_has_confirmed_delivery=False).count(),
            "pending_failed_deliveries": CorrespondenceDelivery.objects.filter(
                correspondence__request=prr,
                status__in=[
                    CorrespondenceDelivery.Status.PENDING,
                    CorrespondenceDelivery.Status.CLAIMED,
                    CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
                    CorrespondenceDelivery.Status.RETRYABLE,
                    CorrespondenceDelivery.Status.FAILED,
                ],
            ).count(),
        }
        blocker_definitions = {
            "open_tasks": ("Complete open collection tasks", "collection"),
            "unresolved_clarifications": (
                "Resolve or close open clarification cycles",
                "messages",
            ),
            "unreviewed_records": ("Review every candidate record", "records"),
            "pending_second_review": (
                "Complete required second review",
                "records",
            ),
            "responsive_records_unreleased": (
                "Include every responsive record in a released installment",
                "releases",
            ),
            "fee_installment_blockers": (
                "Resolve unreleased installment and fee work",
                "releases",
            ),
            "open_deposit_gates": (
                "Record or lift every requested deposit",
                "releases",
            ),
            "undelivered_release_notices": (
                "Confirm delivery of every installment notice",
                "messages",
            ),
            "pending_failed_deliveries": (
                "Resolve or cancel pending and failed delivery",
                "messages",
            ),
        }
        if not has_staff_role(request.user, StaffRole.RECORDS_OFFICER):
            authority = (
                "Records Officer"
                if request.user.is_staff
                else "Authorized staff"
            )
            blocker_definitions["unresolved_clarifications"] = (
                f"{authority} must resolve open clarification cycles",
                "overview",
            )
            blocker_definitions["pending_failed_deliveries"] = (
                f"{authority} must resolve pending or failed delivery",
                "overview",
            )
            blocker_definitions["undelivered_release_notices"] = (
                f"{authority} must confirm installment notice delivery",
                "overview",
            )
        roles = staff_roles(request.user)
        if (
            StaffRole.REVIEWER in roles
            and StaffRole.RECORDS_OFFICER not in roles
            and StaffRole.ADMIN not in roles
        ):
            blocker_definitions["open_tasks"] = (
                "Records Officer must complete open collection tasks",
                "overview",
            )
        blockers = [
            {
                "code": code,
                "label": blocker_definitions[code][0],
                "count": count,
                "tab": blocker_definitions[code][1],
            }
            for code, count in counts.items()
            if count
        ]
        return Response(CaseClosureReadinessSerializer({
            "ready": not blockers,
            "counts": counts,
            "blockers": blockers,
        }).data)

    @extend_schema(
        description=(
            "Log a phone, mail, or in-person request through the canonical "
            "deadline, preservation, routing, quarantine, and audit pipeline."
        ),
        request=StaffIntakeSerializer,
        responses={201: StaffIntakeResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def intake(self, request):
        parsed_data = request.data
        upload_limit_error = getattr(
            request._request,
            "_prr_intake_upload_limit_error",
            "",
        )
        if upload_limit_error:
            return Response(
                {
                    "code": "multipart_upload_limit_exceeded",
                    "attachments": [upload_limit_error],
                },
                status=413,
            )

        serializer = StaffIntakeSerializer(data=parsed_data)
        serializer.is_valid(raise_exception=True)
        try:
            attachments = validate_attachments(
                request.FILES.getlist("attachments"),
            )
        except AttachmentValidationError as exc:
            return Response(
                {"attachments": [str(exc)]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated = dict(serializer.validated_data)
        idempotency_key = validated.pop("idempotency_key")
        validated.pop("attachments", None)
        received_at = validated.pop("received_at")
        matter_kind = validated.pop("matter_kind")
        canonical_payload = {
            "matter_kind": matter_kind,
            **validated,
            "received_at": received_at.astimezone(dt.UTC).isoformat(),
            "attachments": attachment_digest_descriptors(attachments),
        }
        payload_sha256 = hashlib.sha256(
            json.dumps(
                canonical_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
        ).hexdigest()

        with transaction.atomic():
            receipt = (
                StaffIntakeSubmission.objects.filter(
                    submitted_by=request.user,
                    idempotency_key=idempotency_key,
                )
                .select_related("request")
                .first()
            )
            if receipt:
                return self._staff_intake_receipt_response(
                    receipt,
                    payload_sha256,
                )
            try:
                with transaction.atomic():
                    receipt = StaffIntakeSubmission.objects.create(
                        submitted_by=request.user,
                        idempotency_key=idempotency_key,
                        payload_sha256=payload_sha256,
                    )
            except IntegrityError:
                receipt = StaffIntakeSubmission.objects.select_related(
                    "request"
                ).get(
                    submitted_by=request.user,
                    idempotency_key=idempotency_key,
                )
                return self._staff_intake_receipt_response(
                    receipt,
                    payload_sha256,
                )

        def link_request(prr):
            receipt.request = prr
            receipt.save(update_fields=["request"])

        try:
            prr = submit_intake(
                validated,
                channel=validated["channel"],
                matter_kind=matter_kind,
                actor=request.user,
                integration_source="staff_intake",
                idempotency_key=str(idempotency_key),
                on_created=link_request,
                attachments=attachments,
                received_at=received_at,
            )
        except Exception:
            logger.exception("Staff intake failed for receipt %s", receipt.pk)
            receipt.state = StaffIntakeSubmission.State.FAILED
            receipt.last_error = "intake_processing_failed"
            receipt.save(update_fields=["state", "last_error"])
            response = {
                "detail": "The submission was accepted but processing did not complete."
            }
            if receipt.request_id:
                response["number"] = receipt.request.number
            return Response(
                response,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        receipt.state = StaffIntakeSubmission.State.COMPLETED
        receipt.completed_at = timezone.now()
        receipt.last_error = ""
        receipt.save(update_fields=["state", "completed_at", "last_error"])
        return Response(
            {"number": prr.number},
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _staff_intake_receipt_response(receipt, payload_sha256):
        if receipt.payload_sha256 != payload_sha256:
            return Response(
                {"detail": "This intake key was already used with different data."},
                status=status.HTTP_409_CONFLICT,
            )
        if (
            receipt.state == StaffIntakeSubmission.State.COMPLETED
            and receipt.request
        ):
            return Response(
                {"number": receipt.request.number},
                status=status.HTTP_200_OK,
            )
        if receipt.state == StaffIntakeSubmission.State.FAILED:
            response = {"detail": "The original intake did not finish processing."}
            if receipt.request:
                response["number"] = receipt.request.number
            return Response(
                response,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"detail": "The original intake is still processing."},
            status=status.HTTP_409_CONFLICT,
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, number=None):
        prr = self.get_object()
        ser = TransitionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        to_status = ser.validated_data["to_status"]
        if to_status == RequestStatus.DENIED:
            handoff = human_only_handoff(
                request,
                capability_id="requests.transition",
                action="Deny a public records request.",
            )
            if handoff:
                return handoff
        if prr.status in services.CLOSED_STATUSES:
            return Response(
                {"detail": f"This request is already closed ({prr.status}) "
                           "and cannot be transitioned further."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            prr = services.transition_manually(
                prr,
                to_status,
                actor=request.user,
                note=ser.validated_data["note"],
            )
        except _LIFECYCLE_REFUSALS as exc:
            return _lifecycle_error_response(exc)
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=CaseClosureSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="close")
    def close_case(self, request, number=None):
        """Close an open case as withdrawn or abandoned, with a reason."""
        prr = self.get_object()
        serializer = CaseClosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if prr.status in services.CLOSED_STATUSES:
            return Response(
                {
                    "code": "closed_request",
                    "detail": f"This request is already closed ({prr.status}).",
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            prr = services.close_case(
                prr,
                serializer.validated_data["to_status"],
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except _LIFECYCLE_REFUSALS as exc:
            return _lifecycle_error_response(exc)
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=NonRequestClosureSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="close-non-request",
    )
    def close_non_request(self, request, number=None):
        prr = self.get_object()
        serializer = NonRequestClosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            prr = services.close_email_non_request(
                prr,
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except _LIFECYCLE_REFUSALS as exc:
            return _lifecycle_error_response(exc)
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=RequesterIdentityCorrectionSerializer,
        responses={200: RequesterIdentityCorrectionResultSerializer},
    )
    @action(detail=True, methods=["post"], url_path="correct-requester")
    def correct_requester(self, request, number=None):
        """Correct the requester identity a forwarded submission recorded.

        Never an agent action: this is a written officer determination about
        who the statutory response is owed to, so it is refused for every
        non-staff identity by the same permission that keeps service tokens
        read-only, and again by the service layer.
        """
        prr = self.get_object()
        serializer = RequesterIdentityCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            correction = services.correct_requester_identity(
                prr,
                actor=request.user,
                name=serializer.validated_data["name"],
                email=serializer.validated_data["email"],
                reason=serializer.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            return Response(
                {"code": "invalid_requester_correction", "detail": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        refresh = correction.refresh
        return Response(
            {
                "request": self.get_serializer(correction.request).data,
                "previous_requester": {
                    "name": correction.previous_requester.name,
                    "email": correction.previous_requester.email,
                },
                "requester": {
                    "name": correction.requester.name,
                    "email": correction.requester.email,
                },
                "refreshed_correspondence": [
                    _corrected_correspondence_payload(item)
                    for item in refresh.refreshed
                ],
                "correspondence_needing_review": [
                    _corrected_correspondence_payload(item, detail)
                    for item, detail in refresh.needs_review
                ],
                "cancelled_correspondence": [
                    _corrected_correspondence_payload(item)
                    for item in refresh.cancelled
                ],
                "uncancelled_correspondence": [
                    _corrected_correspondence_payload(item, detail)
                    for item, detail in refresh.uncancelled
                ],
            }
        )

    @extend_schema(
        request=ReopenRequestSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, number=None):
        """Reopen a closed request, with a named officer and a written reason."""
        prr = self.get_object()
        serializer = ReopenRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            prr = services.reopen_request(
                prr,
                serializer.validated_data["to_status"],
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except services.TerminalActionRequired as exc:
            return Response(
                {"code": "terminal_action_required", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except services.ClosedRequestTransition as exc:
            return Response(
                {"code": "not_closed", "detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            return Response(
                {"code": "invalid_target", "detail": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=ExternalInitialResponseSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="external-initial-response")
    def external_initial_response(self, request, number=None):
        """Record, on the officer's statement, a first response sent outside this system.

        Never an agent action: it asserts that a statutory obligation was
        met through a channel this system cannot verify, under a named
        Records Officer.
        """
        handoff = human_only_handoff(
            request,
            capability_id="requests.record_external_initial_response",
            action=(
                "Record a statutory first response sent outside this system."
            ),
        )
        if handoff:
            return handoff
        prr = self.get_object()
        serializer = ExternalInitialResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.record_external_initial_response(
                prr,
                actor=request.user,
                sent_via=serializer.validated_data["sent_via"],
                evidence=serializer.validated_data["evidence"],
                responded_on=serializer.validated_data.get("responded_on"),
            )
        except _LIFECYCLE_REFUSALS as exc:
            return _lifecycle_error_response(exc)
        except DjangoValidationError as exc:
            return Response(
                {"code": "invalid_attestation", "detail": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        prr = self.get_queryset().get(pk=prr.pk)
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=FerpaIdentityVerificationSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="ferpa-identity-verification",
    )
    def ferpa_identity_verification(self, request, number=None):
        """Record that a named officer verified a FERPA requester's identity.

        Never an agent action: 34 CFR 99.10 gives the right of access to a
        parent or eligible student and to nobody else, and in a custody
        dispute deciding who that is has consequences no automation can
        carry. It records a judgment and discloses nothing.
        """
        handoff = human_only_handoff(
            request,
            capability_id="requests.verify_ferpa_identity",
            action="Verify a FERPA requester's identity under 34 CFR 99.10.",
        )
        if handoff:
            return handoff
        prr = self.get_object()
        serializer = FerpaIdentityVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.record_ferpa_identity_verification(
                prr,
                actor=request.user,
                method=serializer.validated_data["method"],
                note=serializer.validated_data["note"],
            )
        except DjangoValidationError as exc:
            return Response(
                {
                    "code": "invalid_ferpa_identity_verification",
                    "detail": exc.messages,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        prr = self.get_queryset().get(pk=prr.pk)
        return Response(self.get_serializer(prr).data)

    @extend_schema(
        request=MergeRequestSerializer,
        responses={200: PublicRecordsRequestDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="merge")
    def merge(self, request, number=None):
        """Merge this duplicate case into the case of record; returns the target."""
        source = self.get_object()
        serializer = MergeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = (
            self.get_queryset()
            .filter(number=serializer.validated_data["target_number"])
            .first()
        )
        if target is None:
            return Response(
                {
                    "code": "target_not_found",
                    "detail": "No case with that number is available to you.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            target = services.merge_requests(
                source,
                target,
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except _LIFECYCLE_REFUSALS as exc:
            return _lifecycle_error_response(exc)
        except DjangoValidationError as exc:
            return Response(
                {"code": "invalid_merge", "detail": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(self.get_serializer(target).data)

    @extend_schema(request=None, responses={200: InboxHandledSerializer})
    @action(detail=True, methods=["post"], url_path="inbox-handled")
    def inbox_handled(self, request, number=None):
        """Mark every open requester-email row on this case Handled by the acting officer."""
        prr = self.get_object()
        from apps.work_queue.services import handle_case_inbox_items

        try:
            handled = handle_case_inbox_items(
                prr,
                actor=request.user,
                reason="officer_caught_up",
            )
        except PermissionDenied as exc:
            return Response(
                {"code": "records_officer_required", "detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response({"handled": len(handled)})

    @extend_schema(
        request=JlarcRequestMetricsInputSerializer,
        responses={201: JlarcRequestMetricsSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="jlarc-classification",
    )
    def jlarc_classification(self, request, number=None):
        prr = self.get_object()
        serializer = JlarcRequestMetricsInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            snapshot = classify_request_for_jlarc(
                prr.pk,
                actor=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or exc.messages
            raise serializers.ValidationError(detail) from exc
        return Response(
            JlarcRequestMetricsSerializer(snapshot).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=None,
        responses={200: AdvisoryLatestSerializer, 202: AdvisoryRunSerializer},
    )
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="advisory-intent",
    )
    def advisory_intent(self, request, number=None):
        prr = self.get_object()
        if request.method == "GET":
            latest = (
                AdvisoryRun.objects.filter(
                    request=prr,
                    kind=AdvisoryRun.Kind.REQUEST_INTENT,
                    record_item__isnull=True,
                )
                .select_related("requested_by")
                .first()
            )
            return Response(
                {
                    "enabled": runtime.get("PRR_AI_ADVISORY_ENABLED"),
                    "latest": (
                        AdvisoryRunSerializer(latest).data
                        if latest is not None
                        else None
                    ),
                }
            )
        try:
            run = request_request_intent_advisory(prr.pk, actor=request.user)
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or exc.messages
            raise serializers.ValidationError(detail) from exc
        return Response(
            AdvisoryRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED,
        )

    def _latest_intake_classification(self, prr):
        return (
            AdvisoryRun.objects.filter(
                request=prr,
                kind=AdvisoryRun.Kind.INTAKE_CLASSIFICATION,
                record_item__isnull=True,
            )
            .select_related("requested_by")
            .first()
        )

    @extend_schema(
        request=None,
        responses={200: AdvisoryLatestSerializer, 202: AdvisoryRunSerializer},
    )
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="advisory-intake",
    )
    def advisory_intake(self, request, number=None):
        """The suggested intake label for this case, for a person to confirm.

        Advisory only: nothing here merges, closes, or sends. The officer
        applies a label through the ordinary merge, close, or add-to-case
        action and then records what they decided (``feedback``).
        """
        prr = self.get_object()
        if request.method == "GET":
            latest = self._latest_intake_classification(prr)
            feedback = (
                AdvisoryFeedback.objects.filter(advisory_run=latest).first()
                if latest is not None
                else None
            )
            return Response({
                "enabled": runtime.get("PRR_AI_ADVISORY_ENABLED"),
                "latest": (
                    AdvisoryRunSerializer(latest).data if latest is not None else None
                ),
                "feedback": (
                    {
                        "outcome": feedback.outcome,
                        "final_label": feedback.final_label,
                        "created_at": feedback.created_at,
                    }
                    if feedback is not None
                    else None
                ),
            })
        try:
            run = request_intake_classification(prr.pk, actor=request.user)
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or exc.messages
            raise serializers.ValidationError(detail) from exc
        return Response(
            AdvisoryRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        responses={
            200: inline_serializer(
                "EstimateLetter",
                fields={
                    "correspondence": inline_serializer(
                        "EstimateLetterDraft",
                        allow_null=True,
                        fields={
                            "id": serializers.UUIDField(),
                            "version": serializers.IntegerField(),
                            "status": serializers.CharField(),
                            "system_prepared": serializers.BooleanField(),
                            "subject": serializers.CharField(),
                            "body": serializers.CharField(),
                        },
                    ),
                    "estimate": serializers.JSONField(allow_null=True),
                },
            )
        }
    )
    @action(detail=True, methods=["get"], url_path="estimate-letter")
    def estimate_letter(self, request, number=None):
        """The interpretation-and-estimate letter awaiting approval (#42).

        Read only. The case header shows it with an Approve and send button,
        which calls the ordinary correspondence approve-and-send primitive.
        """
        prr = self.get_object()
        draft = (
            Correspondence.objects.filter(
                request=prr,
                kind=Correspondence.Kind.INTERPRETATION_ESTIMATE,
                status=Correspondence.Status.DRAFT,
            )
            .order_by("-created_at")
            .first()
        )
        estimate = None
        if draft is not None:
            row = RecordsEstimate.objects.filter(
                pk=draft.draft_metadata.get("estimate_id"), request=prr,
            ).first()
            if row is not None:
                estimate = {"id": str(row.pk), "as_of": row.as_of, **row.result}
        return Response({
            "correspondence": None if draft is None else {
                "id": draft.pk,
                "version": draft.version,
                "status": draft.status,
                "system_prepared": draft.system_prepared,
                "subject": draft.draft_subject,
                "body": draft.draft_body,
            },
            "estimate": estimate,
        })

    @extend_schema(request=IntakeFeedbackSerializer, responses={201: None})
    @action(
        detail=True,
        methods=["post"],
        url_path="advisory-intake/feedback",
    )
    def advisory_intake_feedback(self, request, number=None):
        """Record whether the officer agreed with the latest classification."""
        prr = self.get_object()
        payload = IntakeFeedbackSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        latest = self._latest_intake_classification(prr)
        if latest is None or latest.state != AdvisoryRun.State.COMPLETE:
            raise serializers.ValidationError(
                {"detail": ["There is no completed classification to answer."]}
            )
        with transaction.atomic():
            if AdvisoryFeedback.objects.filter(advisory_run=latest).exists():
                return Response(
                    {"code": "feedback_recorded",
                     "detail": "This classification already has an answer."},
                    status=status.HTTP_409_CONFLICT,
                )
            feedback = AdvisoryFeedback.objects.create(
                advisory_run=latest,
                actor=request.user,
                **payload.validated_data,
            )
            audit(
                "advisory.feedback_recorded",
                target=str(latest.pk),
                actor=request.user,
                request_number=prr.number,
                kind=latest.kind,
                outcome=feedback.outcome,
                final_label=feedback.final_label,
                proposed_label=(latest.proposal or {}).get("label", ""),
            )
        return Response({"outcome": feedback.outcome}, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=inline_serializer("ScopingRerun", fields={"reason": serializers.CharField()}),
        responses={
            200: inline_serializer(
                "ScopingLatest",
                fields={"session": serializers.JSONField(allow_null=True)},
            ),
            202: inline_serializer(
                "ScopingStarted",
                fields={"session": serializers.JSONField()},
            ),
        },
    )
    @action(detail=True, methods=["get", "post"], url_path="scoping")
    def scoping(self, request, number=None):
        """Iterative scoping (#36): the latest session's count trail, or an
        officer's re-run with a reason. Counts only."""
        from apps.records.models import ScopingSession
        from apps.records.scoping import rerun_scoping

        prr = self.get_object()
        if request.method == "POST":
            try:
                session = rerun_scoping(
                    prr, actor=request.user, reason=request.data.get("reason", ""),
                )
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.messages) from exc
            except VaultCountAlreadyRunning as exc:
                raise serializers.ValidationError([str(exc)]) from exc
            return Response({"session": _scoping_data(session)}, status=status.HTTP_202_ACCEPTED)
        from apps.records.scoping import current_collection

        session = ScopingSession.objects.filter(request=prr).order_by("-created_at").first()
        return Response({
            "session": _scoping_data(session) if session else None,
            "current_collection": current_collection(prr),
        })

    @extend_schema(
        request=inline_serializer(
            "Rescope",
            fields={
                "count_run_id": serializers.UUIDField(),
                "expected_input_sha256": serializers.CharField(),
                "reason": serializers.CharField(),
            },
        ),
        responses={202: CollectionTaskSerializer},
    )
    @action(detail=True, methods=["post"], url_path="rescope")
    def rescope(self, request, number=None):
        """Collect a new scope and supersede the previous collection (#37)."""
        from apps.records.scoping import rescope as rescope_request

        prr = self.get_object()
        try:
            task = rescope_request(
                prr,
                count_run_id=request.data.get("count_run_id"),
                expected_input_sha256=request.data.get("expected_input_sha256", ""),
                actor=request.user,
                reason=request.data.get("reason", ""),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(
            CollectionTaskSerializer(task, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=VaultCountRequestSerializer,
        responses={200: VaultCountLatestSerializer, 202: VaultCountRunSerializer},
    )
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="vault-counts",
    )
    def vault_counts(self, request, number=None):
        prr = self.get_object()
        if request.method == "GET":
            latest = (
                VaultCountRun.objects.filter(request=prr)
                .select_related("requested_by", "collection_task")
                .first()
            )
            return Response(
                {
                    "enabled": vault_count_enabled(),
                    "corpus": "MAIL",
                    "latest": (
                        VaultCountRunSerializer(latest).data
                        if latest is not None
                        else None
                    ),
                }
            )
        if not vault_count_enabled():
            return Response(
                {"detail": "Google Vault counting is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = VaultCountRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = request_vault_count(
                prr.pk,
                actor=request.user,
                **serializer.validated_data,
            )
        except VaultCountAlreadyRunning as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or exc.messages
            raise serializers.ValidationError(detail) from exc
        return Response(
            VaultCountRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=VaultCollectionRequestSerializer,
        responses={202: CollectionTaskSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="vault-collection",
    )
    def vault_collection(self, request, number=None):
        prr = self.get_object()
        serializer = VaultCollectionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count_run_id = serializer.validated_data["count_run_id"]
        if not VaultCountRun.objects.filter(
            pk=count_run_id,
            request=prr,
        ).exists():
            raise serializers.ValidationError(
                {"count_run_id": "Choose a completed count for this request."}
            )
        try:
            result = queue_vault_collection(
                count_run_id,
                actor=request.user,
                expected_input_sha256=serializer.validated_data[
                    "expected_input_sha256"
                ],
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or exc.messages
            raise serializers.ValidationError(detail) from exc
        return Response(
            CollectionTaskSerializer(
                result.task,
                context={"request": request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string", "format": "binary"},
                    },
                },
                "required": ["files"],
            },
        },
        responses={200: RecordUploadResponseSerializer},
    )
    @action(detail=True, methods=["post"], url_path="record-uploads")
    def record_uploads(self, request, number=None):
        """Add files to this case as unreviewed candidate records.

        Multipart, one or more ``files`` parts. Each file is quarantined,
        malware-scanned, and retained immutably before its ``RecordItem`` is
        written; each has its own outcome in the response, so an infected or
        unscannable file is refused without discarding the clean ones.
        """
        # Parsing is lazy: read the files first so the streaming limit
        # handler has run, and any refusal it recorded is visible below.
        files = request.FILES.getlist("files")
        upload_limit_error = getattr(
            request._request,
            "_prr_record_upload_limit_error",
            "",
        )
        if upload_limit_error:
            return Response(
                {
                    "code": "multipart_upload_limit_exceeded",
                    "files": [upload_limit_error],
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        prr = self.get_object()
        try:
            results = upload_records(
                prr,
                files,
                actor=request.user,
            )
        except RecordUploadClosedCase as exc:
            return Response(
                {"code": "closed_request", "detail": exc.messages[0]},
                status=status.HTTP_409_CONFLICT,
            )
        except RecordUploadLimitExceeded as exc:
            return Response(
                {
                    "code": "multipart_upload_limit_exceeded",
                    "files": exc.messages,
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        except DjangoValidationError as exc:
            return Response(
                {"files": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {
                "added_count": sum(
                    1 for result in results if result.outcome == OUTCOME_ADDED
                ),
                "results": [result.as_dict() for result in results],
            }
        )


def register(router):
    router.register("requests", RequestsViewSet, basename="staff-request")
