import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.append_only import AppendOnlyManager, AppendOnlyModelMixin


class WorkItem(models.Model):
    """Current next-action projection around authoritative source evidence."""

    class SourceType(models.TextChoices):
        REQUEST = "request", "Request"
        CORRESPONDENCE = "correspondence", "Correspondence"
        INBOUND_RECEIPT = "inbound_receipt", "Inbound receipt"
        COLLECTION_TASK = "collection_task", "Collection task"
        RECORD_ITEM = "record_item", "Record review evidence"

    class WorkKind(models.TextChoices):
        REQUEST_ACTION = "request_action", "Request action"
        CORRESPONDENCE_DRAFT = "correspondence_draft", "Correspondence draft"
        CORRESPONDENCE_APPROVAL = (
            "correspondence_approval",
            "Correspondence approval",
        )
        INBOUND_REPLY = "inbound_reply", "Inbound reply"
        EMAIL_INTAKE = "email_intake", "Email intake"
        INBOUND_UNMATCHED = "inbound_unmatched", "Unmatched inbound"
        INBOUND_CANDIDATE = "inbound_candidate", "Candidate inbound"
        INBOUND_AMBIGUOUS = "inbound_ambiguous", "Ambiguous inbound"
        INBOUND_QUARANTINED = "inbound_quarantined", "Quarantined inbound"
        DELIVERY_EXCEPTION = "delivery_exception", "Delivery exception"
        #: A fee or deposit the District asked for that has gone unanswered
        #: long enough to need a decision. Surfacing it closes nothing.
        FEE_UNPAID = "fee_unpaid", "Fee or deposit unpaid"
        COLLECTION = "collection", "Collection"
        RECORD_REVIEW = "record_review", "Record review"
        SECOND_REVIEW = "second_review", "Second review"

    class Disposition(models.TextChoices):
        NEEDS_ATTENTION = "needs_attention", "Needs attention"
        WAITING = "waiting", "Waiting"
        HANDLED = "handled", "Handled"
        NO_ACTION_REQUIRED = "no_action_required", "No action required"
        SOURCE_EXCLUDED = "source_excluded", "Source excluded"

    class NoActionCategory(models.TextChoices):
        COURTESY_COPY = "courtesy_copy", "Courtesy copy"
        DUPLICATE_NOTIFICATION = "duplicate_notification", "Duplicate notification"
        INFORMATIONAL_ONLY = "informational_only", "Informational only"
        RESOLVED_ELSEWHERE = "resolved_elsewhere", "Resolved elsewhere"
        SUPERSEDED = "superseded", "Superseded"
        #: Inbound mail that is no request at all: marketing, alerts, spam,
        #: or a bounce for mail this system never sent.
        NOT_A_RECORDS_REQUEST = "not_a_records_request", "Not a records request"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=30, choices=SourceType.choices)
    source_id = models.CharField(max_length=64)
    work_kind = models.CharField(max_length=40, choices=WorkKind.choices)
    request = models.ForeignKey(
        "records.PublicRecordsRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_items",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_work_items",
    )
    disposition = models.CharField(
        max_length=30,
        choices=Disposition.choices,
        default=Disposition.NEEDS_ATTENTION,
    )
    version = models.PositiveIntegerField(default=1)
    disposed_at = models.DateTimeField(null=True, blank=True)
    disposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="disposed_work_items",
    )
    no_action_category = models.CharField(
        max_length=40,
        choices=NoActionCategory.choices,
        blank=True,
    )
    no_action_reason = models.TextField(blank=True)
    no_action_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="no_action_work_items",
    )
    no_action_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id", "work_kind"],
                name="work_queue_unique_source_kind",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="work_queue_item_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        disposition="no_action_required",
                        no_action_category__in=[
                            "courtesy_copy",
                            "duplicate_notification",
                            "informational_only",
                            "resolved_elsewhere",
                            "superseded",
                            "not_a_records_request",
                        ],
                        no_action_by__isnull=False,
                        no_action_at__isnull=False,
                        disposed_at__isnull=False,
                    )
                    & ~Q(no_action_reason="")
                    & Q(no_action_by=F("disposed_by"))
                    | (
                        ~Q(disposition="no_action_required")
                        & Q(no_action_category="")
                        & Q(no_action_reason="")
                        & Q(no_action_by__isnull=True)
                        & Q(no_action_at__isnull=True)
                    )
                ),
                name="work_queue_no_action_evidence_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        disposition__in=["needs_attention", "waiting"],
                        disposed_at__isnull=True,
                        disposed_by__isnull=True,
                    )
                    | Q(
                        disposition="handled",
                        disposed_at__isnull=False,
                        disposed_by__isnull=False,
                    )
                    | Q(
                        disposition="no_action_required",
                        disposed_at__isnull=False,
                    )
                    | Q(
                        disposition="source_excluded",
                        disposed_at__isnull=False,
                        disposed_by__isnull=True,
                    )
                ),
                name="work_queue_disposition_evidence_shape",
            ),
        ]

    @property
    def is_terminal(self):
        return self.disposition in {
            self.Disposition.HANDLED,
            self.Disposition.NO_ACTION_REQUIRED,
            self.Disposition.SOURCE_EXCLUDED,
        }


class WorkItemEvent(AppendOnlyModelMixin, models.Model):
    """Append-only work history; never a request ``StatusEvent`` substitute."""

    class EventType(models.TextChoices):
        CREATED = "created", "Created"
        ASSIGNED = "assigned", "Assigned"
        REASSIGNED = "reassigned", "Reassigned"
        UNASSIGNED = "unassigned", "Unassigned"
        DISPOSITIONED = "dispositioned", "Dispositioned"
        REOPENED = "reopened", "Reopened"
        READ = "read", "Read"
        UNREAD = "unread", "Unread"
        RECONCILED = "reconciled", "Reconciled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        WorkItem,
        on_delete=models.PROTECT,
        related_name="events",
    )
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_item_events",
    )
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    expected_version = models.PositiveIntegerField(null=True, blank=True)
    resulting_version = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=200, blank=True)
    mutation_fingerprint = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["occurred_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["work_item"],
                condition=Q(event_type="created"),
                name="work_queue_one_creation_event",
            ),
            models.UniqueConstraint(
                fields=["actor", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="work_queue_actor_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=Q(resulting_version__gte=1),
                name="work_queue_event_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(idempotency_key="", mutation_fingerprint="")
                    | (
                        ~Q(idempotency_key="")
                        & Q(
                            idempotency_key__regex=(
                                r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
                            )
                        )
                        & Q(mutation_fingerprint__regex=r"\A[0-9a-f]{64}\Z")
                        & Q(actor__isnull=False)
                    )
                ),
                name="work_queue_event_idempotency_shape",
            ),
        ]


class WorkItemReceipt(models.Model):
    """One staff member's read state, independent of team disposition."""

    work_item = models.ForeignKey(
        WorkItem,
        on_delete=models.PROTECT,
        related_name="receipts",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="work_item_receipts",
    )
    read_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["work_item", "user"],
                name="work_queue_unique_user_receipt",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="work_queue_receipt_version_positive",
            ),
        ]
