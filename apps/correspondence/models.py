"""Requester-correspondence records and transport evidence.

The models in this app deliberately separate immutable recipient-visible
content from delivery jobs and from the legal request lifecycle. Services in
this app will be the only place allowed to coordinate those records with
``apps.records.services.transition``.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, router, transaction
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.configuration import runtime


def _raw_message_storage_key() -> str:
    return f"quarantine/correspondence/raw/{uuid.uuid4().hex}"


def _attachment_storage_key() -> str:
    return f"quarantine/correspondence/attachments/{uuid.uuid4().hex}"


def _reservation_storage_key(kind: str) -> str:
    if kind == "raw_message":
        return _raw_message_storage_key()
    if kind == "attachment":
        return _attachment_storage_key()
    raise ValidationError("Inbound quarantine reservation kind is invalid.")


_MESSAGE_ID_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_ATTACHMENT_CONTENT_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}\Z",
    re.ASCII,
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RFC_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z")


class InboundQuarantineReservationAlreadyAdopted(ValidationError):
    """A concurrent exact replay adopted the shared reservation first."""


class InboundQuarantineReservationConflict(ValidationError):
    """A transport identity was concurrently reserved for other evidence."""

    def __init__(self, conflict_kind):
        self.conflict_kind = conflict_kind
        super().__init__(
            "Inbound quarantine transport identity conflicts with reserved "
            "evidence."
        )


class InboundQuarantineCleanupInProgress(ValidationError):
    """Stale cleanup owns this reservation; ingestion must retry later."""


class GmailSyncState(models.Model):
    """Durable, environment-scoped Gmail history checkpoint."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mailbox = models.EmailField(max_length=100)
    environment = models.CharField(max_length=64)
    history_id = models.CharField(max_length=255, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["mailbox", "environment"]
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox", "environment"],
                name="correspondence_gmail_sync_mailbox_environment_unique",
            ),
            models.CheckConstraint(
                condition=Q(history_id="") | Q(history_id__regex=r"\A[0-9]+\Z"),
                name="correspondence_gmail_sync_history_id_valid",
            ),
        ]


def _validated_message_id_domain() -> str:
    domain = runtime.get("PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN")
    if not _MESSAGE_ID_DOMAIN.fullmatch(domain):
        raise ValidationError(
            "PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN must be a valid DNS name."
        )
    return domain.lower()


def sanitize_filename(value: str) -> str:
    """Return safe display metadata; filenames never become object keys."""
    name = unicodedata.normalize("NFKC", value or "")
    name = os.path.basename(name.replace("\\", "/"))
    name = "".join(
        character
        for character in name
        if not unicodedata.category(character).startswith("C")
    )
    name = name[:255]
    return "attachment" if name in {"", ".", ".."} else name


TERMINAL_OUTBOUND_CORRESPONDENCE_STATUSES = frozenset(
    {"sent", "cancelled"}
)


class CorrespondenceQuerySet(models.QuerySet):
    _IMMUTABLE_UPDATE_FIELDS = {
        "request",
        "request_id",
        "direction",
        "kind",
        "draft_subject",
        "draft_body",
        "draft_metadata",
        "approved_subject",
        "approved_body",
        "approved_metadata",
        "received_subject",
        "received_body",
        "received_sender",
        "received_recipients",
        "received_headers",
        "template_identifier",
        "template_version",
        "automated",
        "replaces",
        "replaces_id",
        "rfc_message_id",
        "reply_token",
        "provider",
        "provider_account",
    }
    _INBOUND_ASSOCIATION_FIELDS = {
        "request",
        "request_id",
        "kind",
        "status",
        "matched_at",
        "matched_by",
        "matched_by_id",
        "version",
    }
    _INBOUND_TRANSPORT_FIELDS = {
        "provider_message_id",
        "provider_thread_id",
        "received_at",
        "created_at",
    }
    _RESULTING_STATUS_EVENT_FIELDS = {
        "resulting_status_event",
        "resulting_status_event_id",
    }

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objs = list(objs)
        conflict_update_fields = {
            getattr(field, "name", field)
            for field in (update_fields or [])
        }
        if (
            any(
                obj.resulting_status_event_id is not None
                for obj in objs
            )
            or self._RESULTING_STATUS_EVENT_FIELDS.intersection(
                conflict_update_fields
            )
        ):
            raise ValidationError(
                "A correspondence resulting status event may change only "
                "through the locked one-shot lifecycle binding."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def update(self, **kwargs):
        if self._RESULTING_STATUS_EVENT_FIELDS.intersection(kwargs):
            raise ValidationError(
                "A correspondence resulting status event may change only "
                "through the locked one-shot lifecycle binding."
            )
        if (
            self._INBOUND_TRANSPORT_FIELDS.intersection(kwargs)
            and self.filter(direction="inbound").exists()
        ):
            raise ValidationError(
                "Inbound correspondence transport evidence is immutable."
            )
        if (
            self._INBOUND_ASSOCIATION_FIELDS.intersection(kwargs)
            and self.filter(direction="inbound").exists()
        ):
            raise ValidationError(
                "Inbound correspondence associations may change only through "
                "the locked matching workflow."
            )
        next_status = kwargs.get("status")
        if (
            next_status is not None
            and self.filter(
                status__in=TERMINAL_OUTBOUND_CORRESPONDENCE_STATUSES
            )
            .exclude(status=next_status)
            .exists()
        ):
            raise ValidationError(
                "Sent and cancelled correspondence are terminal; create an "
                "audited replacement instead."
            )
        if (
            kwargs.get("status") == "draft"
            and self.exclude(status="draft").exists()
        ):
            raise ValidationError(
                "Approved correspondence cannot return to draft; create an "
                "audited replacement instead."
            )
        if self._IMMUTABLE_UPDATE_FIELDS.intersection(kwargs) and self.exclude(
            status="draft"
        ).exists():
            raise ValidationError(
                "Approved correspondence content and identity are immutable; "
                "create an audited replacement instead."
            )
        return super().update(**kwargs)

    @staticmethod
    def _validate_resulting_status_event(correspondence, status_event):
        """Validate the exact immutable legal effect represented by a message."""
        from apps.records.models import RequestStatus, StatusEvent

        expected_statuses = {
            Correspondence.Kind.ACKNOWLEDGMENT: {
                RequestStatus.ACKNOWLEDGED,
                "",
            },
            Correspondence.Kind.CLARIFICATION_REQUEST: {
                RequestStatus.CLARIFICATION,
            },
            # Only the clarification form of a scope conversation ever binds a
            # status event; the informational form binds none at all, which is
            # why "" is not admitted here.
            Correspondence.Kind.SCOPE_CLARIFICATION: {
                RequestStatus.CLARIFICATION,
            },
            Correspondence.Kind.ESTIMATE_REVISION: {""},
            Correspondence.Kind.INTERPRETATION_ESTIMATE: {""},
            Correspondence.Kind.DENIAL: {RequestStatus.DENIED},
            Correspondence.Kind.NO_RECORDS_CLOSURE: {
                RequestStatus.CLOSED_NO_RECORDS
            },
            Correspondence.Kind.COMPLETION_CLOSURE: {
                RequestStatus.FULFILLED
            },
            Correspondence.Kind.NONPAYMENT_CLOSURE: {
                RequestStatus.CLOSED_ABANDONED
            },
        }
        expected_effects = {
            Correspondence.Kind.ACKNOWLEDGMENT: (
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
            Correspondence.Kind.CLARIFICATION_REQUEST: (
                StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY
            ),
            Correspondence.Kind.SCOPE_CLARIFICATION: (
                StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY
            ),
            Correspondence.Kind.ESTIMATE_REVISION: (
                StatusEvent.CorrespondenceEffect.ESTIMATE_REVISION_DELIVERY
            ),
            Correspondence.Kind.INTERPRETATION_ESTIMATE: (
                StatusEvent.CorrespondenceEffect.ESTIMATE_REVISION_DELIVERY
            ),
            Correspondence.Kind.DENIAL: (
                StatusEvent.CorrespondenceEffect.DENIAL_DELIVERY
            ),
            Correspondence.Kind.NO_RECORDS_CLOSURE: "",
            Correspondence.Kind.COMPLETION_CLOSURE: "",
            Correspondence.Kind.NONPAYMENT_CLOSURE: "",
        }
        expected = expected_statuses.get(correspondence.kind)
        expected_effect = expected_effects.get(correspondence.kind)
        blank_effect_valid = (
            status_event.to_status != ""
            or (
                status_event.from_status == ""
                and correspondence.kind
                in {
                    Correspondence.Kind.ACKNOWLEDGMENT,
                    Correspondence.Kind.ESTIMATE_REVISION,
                    Correspondence.Kind.INTERPRETATION_ESTIMATE,
                }
            )
        )
        requester_visible_effect = (
            status_event.visible_to_requester
            and len((status_event.note or "").strip()) >= 10
        )
        if (
            not requester_visible_effect
            and correspondence.kind
            == Correspondence.Kind.ACKNOWLEDGMENT
            and status_event.from_status == RequestStatus.RECEIVED
            and status_event.to_status == RequestStatus.ACKNOWLEDGED
            and not status_event.visible_to_requester
            and status_event.note == ""
        ):
            paired_notes = type(status_event).objects.filter(
                request_id=status_event.request_id,
                actor_id=status_event.actor_id,
                occurred_at=status_event.occurred_at,
                from_status="",
                to_status="",
                visible_to_requester=True,
            ).exclude(pk=status_event.pk).values_list("note", flat=True)
            requester_visible_effect = any(
                len((note or "").strip()) >= 10
                for note in paired_notes
            )
        if (
            correspondence.direction != Correspondence.Direction.OUTBOUND
            or correspondence.status != Correspondence.Status.SENT
            or correspondence.request_id is None
            or correspondence.sent_at is None
            or expected is None
            or expected_effect is None
            or status_event.correspondence_effect != expected_effect
            or status_event.request_id != correspondence.request_id
            or status_event.to_status not in expected
            or not blank_effect_valid
            or status_event.occurred_at != correspondence.sent_at
            or not requester_visible_effect
            or status_event.actor_id != correspondence.approved_by_id
        ):
            raise ValidationError(
                "The resulting status event does not match this sent "
                "correspondence's legal effect."
            )

    def _bind_locked_resulting_status_event(self, *, status_event):
        """Bind one status event while holding the correspondence row lock."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "A resulting status event binding requires a transaction."
            )
        from apps.records.models import StatusEvent

        correspondence = self.select_for_update().get()
        event_id = getattr(status_event, "pk", status_event)
        event = (
            StatusEvent.objects.using(self.db)
            .select_for_update()
            .get(pk=event_id)
        )
        if correspondence.resulting_status_event_id is not None:
            if correspondence.resulting_status_event_id == event.pk:
                self._validate_resulting_status_event(correspondence, event)
                return correspondence
            raise ValidationError(
                "This correspondence is already bound to a resulting status "
                "event."
            )
        self._validate_resulting_status_event(correspondence, event)
        if Correspondence.objects.using(self.db).filter(
            resulting_status_event_id=event.pk,
        ).exclude(pk=correspondence.pk).exists():
            raise ValidationError(
                "This status event already represents another "
                "correspondence result."
            )
        raw_queryset = models.QuerySet(
            model=self.model,
            using=self.db,
        ).filter(
            pk=correspondence.pk,
            resulting_status_event__isnull=True,
        )
        try:
            updated = raw_queryset.update(
                resulting_status_event_id=event.pk
            )
        except IntegrityError as exc:
            raise ValidationError(
                "This status event already represents another "
                "correspondence result."
            ) from exc
        if updated != 1:
            raise ValidationError(
                "The resulting status event was concurrently bound."
            )
        correspondence.resulting_status_event_id = event.pk
        correspondence._state.fields_cache.pop("resulting_status_event", None)
        return correspondence

    def _validate_locked_resulting_status_event(self):
        """Fail closed when an existing lifecycle-effect binding is corrupt."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "A resulting status event validation requires a transaction."
            )
        correspondence = self.select_for_update().get()
        if correspondence.resulting_status_event_id is None:
            raise ValidationError(
                "This correspondence has no resulting status event."
            )
        from apps.records.models import StatusEvent

        status_event = (
            StatusEvent.objects.using(self.db)
            .select_for_update()
            .get(pk=correspondence.resulting_status_event_id)
        )
        self._validate_resulting_status_event(
            correspondence,
            status_event,
        )
        return correspondence

    def delete(self):
        target_ids = sorted(set(self.values_list("pk", flat=True)))
        with transaction.atomic(using=self.db):
            locked = list(
                Correspondence.objects.using(self.db)
                .select_for_update()
                .filter(pk__in=target_ids)
                .order_by("pk")
                .values_list("status", flat=True)
            )
            if any(status != "draft" for status in locked):
                raise ValidationError(
                    "Non-draft correspondence is an immutable legal record."
                )
            raw_queryset = models.QuerySet(
                model=self.model,
                using=self.db,
            ).filter(pk__in=target_ids)
            return models.QuerySet.delete(raw_queryset)

    def _set_locked_inbound_association(
        self,
        *,
        request_id,
        kind,
        matched_at,
        matched_by_id,
    ):
        """Update only the association fields on one locked inbound message.

        Callers must already be inside the transaction that owns the matching
        decision.  The explicit argument list prevents this escape hatch from
        becoming a way to rewrite requester-visible content or transport
        evidence on an immutable inbound legal record.
        """
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Inbound correspondence association requires a transaction."
            )
        correspondence = self.select_for_update().only(
            "direction",
            "status",
        ).get()
        if correspondence.direction != Correspondence.Direction.INBOUND:
            raise ValidationError(
                "Only inbound correspondence may be associated by the matcher."
            )
        if correspondence.status not in {
            Correspondence.Status.UNMATCHED,
            Correspondence.Status.RECEIVED,
        }:
            raise ValidationError(
                "This inbound correspondence is not eligible for matching."
            )
        if request_id is None:
            raise ValidationError("Matched inbound correspondence requires a request.")
        if kind not in {
            Correspondence.Kind.GENERAL,
            Correspondence.Kind.CLARIFICATION_RESPONSE,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        }:
            raise ValidationError("The inbound matcher cannot assign this kind.")
        return super().update(
            request_id=request_id,
            kind=kind,
            status=Correspondence.Status.RECEIVED,
            matched_at=matched_at,
            matched_by_id=matched_by_id,
            version=models.F("version") + 1,
        )

    def _set_locked_clarification_response(self, *, request_id):
        """Refine one matched inbound message after explicit officer review."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Clarification response association requires a transaction."
            )
        correspondence = self.select_for_update().only(
            "direction",
            "status",
            "request_id",
            "kind",
        ).get()
        if (
            correspondence.direction != Correspondence.Direction.INBOUND
            or correspondence.status != Correspondence.Status.RECEIVED
            or correspondence.request_id != request_id
            or correspondence.kind != Correspondence.Kind.GENERAL
        ):
            raise ValidationError(
                "Only matched general inbound correspondence may be associated "
                "with a clarification cycle."
            )
        return super().update(
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            version=models.F("version") + 1,
        )


class Correspondence(models.Model):
    """One incoming or outgoing communication and its immutable snapshot."""

    class Direction(models.TextChoices):
        OUTBOUND = "outbound", "Outbound"
        INBOUND = "inbound", "Inbound"

    class Kind(models.TextChoices):
        ACKNOWLEDGMENT = "acknowledgment", "Acknowledgment"
        CLARIFICATION_REQUEST = "clarification_request", "Clarification request"
        CLARIFICATION_RESPONSE = "clarification_response", "Clarification response"
        DELIVERY_STATUS_NOTIFICATION = (
            "delivery_status_notification",
            "Delivery-status notification",
        )
        ESTIMATE_REVISION = "estimate_revision", "Estimate revision"
        #: The interpretation-and-estimate letter (#42): what the District
        #: understands the request to ask for, about how many records, the
        #: copying charge, and when. The only kind a system-prepared draft
        #: may have; a Records Officer still approves and sends it.
        INTERPRETATION_ESTIMATE = (
            "interpretation_estimate",
            "Interpretation and estimate",
        )
        #: RCW 42.56.120 copying-charge estimate, optionally carrying the
        #: deposit the District asks for before it does the gated work.
        FEE_ESTIMATE = "fee_estimate", "Fee estimate"
        INSTALLMENT_NOTICE = "installment_notice", "Installment notice"
        RESPONSIVE_RECORDS_NOTICE = (
            "responsive_records_notice",
            "Responsive-records notice",
        )
        SCOPE_CLARIFICATION = (
            "scope_clarification",
            "Scope conversation",
        )
        SUBMISSION_RECEIPT = "submission_receipt", "Submission receipt"
        CLOSURE = "closure", "Closure"
        NO_RECORDS_CLOSURE = (
            "no_records_closure",
            "No-records / out-of-retention closure",
        )
        COMPLETION_CLOSURE = (
            "completion_closure",
            "Completion closure",
        )
        NONPAYMENT_CLOSURE = (
            "nonpayment_closure",
            "Non-payment closure",
        )
        #: RCW 42.56.540 notice to a named person whose records the District
        #: intends to disclose. Never addressed to the requester.
        THIRD_PARTY_NOTICE = (
            "third_party_notice",
            "Third-party notice",
        )
        DENIAL = "denial", "Denial"
        GENERAL = "general", "General correspondence"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        RECEIVED = "received", "Received"
        UNMATCHED = "unmatched", "Unmatched"
        QUARANTINED = "quarantined", "Quarantined"

    OUTBOUND_STATUSES = {
        Status.DRAFT,
        Status.APPROVED,
        Status.QUEUED,
        Status.SENT,
        Status.FAILED,
        Status.CANCELLED,
    }
    INBOUND_STATUSES = {
        Status.RECEIVED,
        Status.UNMATCHED,
        Status.QUARANTINED,
    }
    SNAPSHOT_STATUSES = {
        Status.APPROVED,
        Status.QUEUED,
        Status.SENT,
        Status.FAILED,
        Status.CANCELLED,
    }
    IMMUTABLE_FIELDS = (
        "request_id",
        "direction",
        "kind",
        "draft_subject",
        "draft_body",
        "draft_metadata",
        "approved_subject",
        "approved_body",
        "approved_metadata",
        "received_subject",
        "received_body",
        "received_sender",
        "received_recipients",
        "received_headers",
        "template_identifier",
        "template_version",
        "automated",
        "replaces_id",
        "rfc_message_id",
        "reply_token",
        "provider",
        "provider_account",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        "records.PublicRecordsRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="correspondence",
    )
    direction = models.CharField(max_length=20, choices=Direction.choices)
    kind = models.CharField(max_length=40, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    draft_subject = models.CharField(max_length=998, blank=True)
    draft_body = models.TextField(blank=True)
    draft_metadata = models.JSONField(default=dict, blank=True)
    approved_subject = models.CharField(max_length=998, blank=True)
    approved_body = models.TextField(blank=True)
    approved_metadata = models.JSONField(default=dict, blank=True)
    received_subject = models.CharField(max_length=998, blank=True)
    received_body = models.TextField(blank=True)
    received_sender = models.CharField(max_length=998, blank=True)
    received_recipients = models.JSONField(default=list, blank=True)
    received_headers = models.JSONField(default=dict, blank=True)
    template_identifier = models.CharField(max_length=200, blank=True)
    template_version = models.CharField(max_length=100, blank=True)
    automated = models.BooleanField(default=False)
    #: Drafted by the system with no author (``created_by`` is empty), for a
    #: Records Officer to approve. Never sent by the system. See #42 and
    #: ``services.create_interpretation_estimate_draft``.
    system_prepared = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_correspondence",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_correspondence",
    )
    queued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="queued_correspondence",
    )
    matched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matched_correspondence",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    rfc_message_id = models.CharField(
        max_length=998,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    reply_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    provider = models.CharField(max_length=50, default="gmail")
    provider_account = models.CharField(max_length=100, default="default")
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_thread_id = models.CharField(max_length=255, blank=True)
    failure_category = models.CharField(max_length=100, blank=True)
    failure_description = models.CharField(max_length=1000, blank=True)
    version = models.PositiveIntegerField(default=1)
    resulting_status_event = models.OneToOneField(
        "records.StatusEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="resulting_correspondence",
    )
    replaces = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacements",
    )

    objects = CorrespondenceQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        base_manager_name = "objects"
        indexes = [
            models.Index(fields=["request", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["provider_thread_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(direction="outbound", status__in=[
                        "draft", "approved", "queued", "sent", "failed", "cancelled"
                    ])
                    | Q(direction="inbound", status__in=[
                        "received", "unmatched", "quarantined"
                    ])
                ),
                name="correspondence_direction_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(kind="delivery_status_notification")
                    | Q(direction="inbound")
                ),
                name="correspondence_dsn_kind_inbound_only",
            ),
            models.CheckConstraint(
                condition=(
                    Q(request__isnull=False)
                    | Q(direction="inbound", status__in=["unmatched", "quarantined"])
                ),
                name="correspondence_request_unless_unmatched",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["approved", "queued", "sent", "failed", "cancelled"])
                    | (
                        ~Q(approved_subject="")
                        & ~Q(approved_body="")
                        & (Q(approved_by__isnull=False) | Q(automated=True))
                        & Q(approved_at__isnull=False)
                    )
                ),
                name="correspondence_approved_snapshot_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(automated=False)
                    | Q(
                        direction="outbound",
                        kind__in=["acknowledgment", "submission_receipt"],
                    )
                ),
                name="correspondence_automation_kind_allowed",
            ),
            # A system-prepared draft is written by the system, never sent by
            # it: it is never ``automated`` and only two kinds may be prepared
            # this way. Approval still needs a named Records Officer
            # (correspondence_approved_snapshot_complete).
            models.CheckConstraint(
                condition=(
                    Q(system_prepared=False)
                    | Q(
                        automated=False,
                        direction="outbound",
                        kind__in=["interpretation_estimate", "scope_clarification"],
                    )
                ),
                name="correspondence_system_prepared_kind_allowed",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["queued", "sent", "failed"])
                    | Q(queued_at__isnull=False)
                ),
                name="correspondence_queued_timestamp_present",
            ),
            models.CheckConstraint(
                condition=~Q(status="sent") | Q(sent_at__isnull=False),
                name="correspondence_sent_timestamp_present",
            ),
            models.CheckConstraint(
                condition=~Q(status="failed") | Q(failed_at__isnull=False),
                name="correspondence_failed_timestamp_present",
            ),
            models.CheckConstraint(
                condition=~Q(status="cancelled") | Q(cancelled_at__isnull=False),
                name="correspondence_cancelled_timestamp_present",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="correspondence_version_positive",
            ),
            models.CheckConstraint(
                condition=~Q(replaces=models.F("id")),
                name="correspondence_cannot_replace_itself",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_account", "provider_message_id"],
                condition=~Q(provider_message_id=""),
                name="correspondence_provider_message_unique",
            ),
            # The courtesy receipt is sent once per request. Both the automated
            # intake path and the officer-triggered path check for an existing
            # receipt first; this constraint is what makes the invariant hold
            # when two of them race.
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(kind="submission_receipt") & ~Q(status="cancelled"),
                name="correspondence_one_submission_receipt_per_request",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(direction="inbound")
                    | Q(received_at__isnull=False)
                ),
                name="correspondence_inbound_received_at_present",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and self.resulting_status_event_id is not None:
            raise ValidationError(
                "A correspondence resulting status event may change only "
                "through the locked one-shot lifecycle binding."
            )
        if self.direction == self.Direction.OUTBOUND and self._state.adding:
            domain = _validated_message_id_domain()
            self.rfc_message_id = f"<{self.id}.correspondence@{domain}>"
        if self.direction == self.Direction.INBOUND and self.received_at is None:
            self.received_at = timezone.now()

        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if (
                previous.resulting_status_event_id
                != self.resulting_status_event_id
            ):
                raise ValidationError(
                    "A correspondence resulting status event may change only "
                    "through the locked one-shot lifecycle binding."
                )
            if previous.direction == self.Direction.INBOUND:
                transport_fields = (
                    "provider_message_id",
                    "provider_thread_id",
                    "received_at",
                    "created_at",
                )
                if any(
                    getattr(previous, field) != getattr(self, field)
                    for field in transport_fields
                ):
                    raise ValidationError(
                        "Inbound correspondence transport evidence is immutable."
                    )
                association_fields = (
                    "request_id",
                    "kind",
                    "status",
                    "matched_at",
                    "matched_by_id",
                    "version",
                )
                if any(
                    getattr(previous, field) != getattr(self, field)
                    for field in association_fields
                ):
                    raise ValidationError(
                        "Inbound correspondence associations may change only "
                        "through the locked matching workflow."
                    )
            if (
                previous.status
                in TERMINAL_OUTBOUND_CORRESPONDENCE_STATUSES
                and self.status != previous.status
            ):
                raise ValidationError(
                    "Sent and cancelled correspondence are terminal; create "
                    "an audited replacement instead."
                )
            if (
                previous.status != self.Status.DRAFT
                and self.status == self.Status.DRAFT
            ):
                raise ValidationError(
                    "Approved correspondence cannot return to draft; create "
                    "an audited replacement instead."
                )
            if previous.status != self.Status.DRAFT:
                changed = [
                    field
                    for field in self.IMMUTABLE_FIELDS
                    if getattr(previous, field) != getattr(self, field)
                ]
                if changed:
                    raise ValidationError(
                        "Approved correspondence content and identity are immutable; "
                        "create an audited replacement instead."
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db
        return type(self).objects.using(using).filter(pk=self.pk).delete()


class CorrespondenceSearchChunk(models.Model):
    """Database-maintained bounded full-text projection of immutable content."""

    correspondence = models.ForeignKey(
        Correspondence,
        on_delete=models.CASCADE,
        related_name="search_chunks",
    )
    source = models.CharField(max_length=20)
    chunk_index = models.PositiveIntegerField()
    search_vector = SearchVectorField(editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["correspondence", "source", "chunk_index"],
                name="corr_search_chunk_unique",
            )
        ]
        indexes = [
            GinIndex(fields=["search_vector"], name="corr_search_chunk_gin"),
        ]


class AutomaticMessageTemplateQuerySet(models.QuerySet):
    """Keeps the activation immutability guarantee off the bulk-write bypass.

    ``Model.save()`` guards field-level immutability, but ``QuerySet.update()``
    and ``bulk_update()`` never call it. Correspondence snapshots are
    reproduced from a pinned template row, so a bulk rewrite of an activated
    version would silently break that reproduction.
    """

    def _immutable_fields(self, names):
        return set(AutomaticMessageTemplate.IMMUTABLE_AFTER_ACTIVATION).intersection(
            # ``created_by_id`` is the attribute name; accept the field name too.
            set(names) | {f"{name}_id" for name in names}
        )

    def update(self, **kwargs):
        if self._immutable_fields(kwargs.keys()) and (
            self.exclude(state=AutomaticMessageTemplate.State.DRAFT).exists()
        ):
            raise ValidationError(
                "Activated automatic-message template content is immutable; "
                "create a new draft version instead."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        if self._immutable_fields(fields):
            activated = (
                AutomaticMessageTemplate.objects.filter(
                    pk__in=[obj.pk for obj in objs if obj.pk is not None],
                )
                .exclude(state=AutomaticMessageTemplate.State.DRAFT)
                .exists()
            )
            if activated:
                raise ValidationError(
                    "Activated automatic-message template content is immutable; "
                    "create a new draft version instead."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise ValidationError(
            "Automatic-message template versions are retained as legal evidence."
        )


#: Kinds that may carry several named templates at once. General
#: correspondence is one slot for every situation that is not a statutory
#: letter -- a records-not-ready note, a fee reminder, a courtesy follow-up --
#: so the district kept its other wordings outside the application, where
#: nothing versioned or audited them. A statutory kind stays a single slot on
#: purpose: two active acknowledgments is an ambiguity about what the
#: District's five-day response says.
NAMED_TEMPLATE_KINDS = frozenset({"general"})


class AutomaticMessageTemplate(models.Model):
    """Versioned plain-text source for every editable message template.

    Drafts are editable. Once activated, their content becomes immutable so
    correspondence snapshots can always be reproduced from the exact version
    that was used. Structured kinds render their legal snapshot from the
    activated version; free-form kinds use it only as a staff drafting
    starting point and remain human-authored.
    """

    class Kind(models.TextChoices):
        ACKNOWLEDGMENT = "acknowledgment", "Five-day acknowledgment"
        SUBMISSION_RECEIPT = "submission_receipt", "Submission receipt"
        CLARIFICATION_REQUEST = "clarification_request", "Clarification request"
        ESTIMATE_REVISION = "estimate_revision", "Estimate revision"
        INTERPRETATION_ESTIMATE = (
            "interpretation_estimate",
            "Interpretation and estimate",
        )
        FEE_ESTIMATE = "fee_estimate", "Fee estimate"
        INSTALLMENT_NOTICE = "installment_notice", "Installment notice"
        NO_RECORDS_CLOSURE = (
            "no_records_closure",
            "No-records / out-of-retention closure",
        )
        COMPLETION_CLOSURE = "completion_closure", "Completion closure"
        NONPAYMENT_CLOSURE = "nonpayment_closure", "Non-payment closure"
        THIRD_PARTY_NOTICE = "third_party_notice", "Third-party notice"
        RESPONSIVE_RECORDS_NOTICE = (
            "responsive_records_notice",
            "Responsive-records notice",
        )
        SCOPE_CLARIFICATION = (
            "scope_clarification",
            "Scope conversation",
        )
        CLOSURE = "closure", "Closure"
        DENIAL = "denial", "Denial"
        GENERAL = "general", "General correspondence"

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    NAMED_KINDS = NAMED_TEMPLATE_KINDS

    IMMUTABLE_AFTER_ACTIVATION = (
        "kind",
        "name",
        "template_version",
        "subject_template",
        "body_template",
        "change_reason",
        "created_by_id",
        # An override is part of the published record of *why* this wording
        # was allowed. Editing it after activation would rewrite the answer to
        # a question an access review or a court might ask.
        "guardrail_overrides",
        "guardrail_override_reason",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=40, choices=Kind.choices)
    #: Empty for a single-slot kind. For a kind in ``NAMED_KINDS`` this is how
    #: an officer tells two starting points apart in the compose dialog.
    name = models.CharField(max_length=120, blank=True, default="")
    template_version = models.PositiveIntegerField()
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.DRAFT,
    )
    subject_template = models.CharField(max_length=998)
    body_template = models.TextField()
    change_reason = models.CharField(max_length=1000)
    #: Guardrail codes a Records Officer or Application Admin knowingly
    #: published past, e.g. ``missing_token:estimated_completion``. Empty for
    #: the ordinary template that trips nothing.
    guardrail_overrides = models.JSONField(default=list, blank=True)
    #: Why those guardrails were overridden. Required when the list is not
    #: empty; the pair is what makes an override defensible a year later.
    guardrail_override_reason = models.CharField(
        max_length=1000,
        blank=True,
        default="",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_automatic_message_templates",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="activated_automatic_message_templates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    objects = AutomaticMessageTemplateQuerySet.as_manager()

    class Meta:
        ordering = ["kind", "name", "-template_version", "-created_at"]
        constraints = [
            # Names compare without case: "Fee reminder" and "FEE REMINDER"
            # are one template, never two active ones side by side.
            models.UniqueConstraint(
                F("kind"),
                Lower("name"),
                F("template_version"),
                name="automatic_template_kind_version_unique",
            ),
            models.UniqueConstraint(
                F("kind"),
                Lower("name"),
                condition=Q(state="active"),
                name="automatic_template_one_active_per_kind",
            ),
            models.CheckConstraint(
                # Only a named kind may carry a name. Everything else is one
                # slot, and a name on it would silently create a second
                # active version of a statutory letter.
                condition=(
                    Q(name="") | Q(kind__in=sorted(NAMED_TEMPLATE_KINDS))
                ),
                name="automatic_template_name_only_for_named_kinds",
            ),
            models.CheckConstraint(
                condition=Q(template_version__gte=1),
                name="automatic_template_version_positive",
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1),
                name="automatic_template_revision_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        state="draft",
                        activated_at__isnull=True,
                        activated_by__isnull=True,
                    )
                    | Q(
                        state__in=["active", "retired"],
                        activated_at__isnull=False,
                        activated_by__isnull=False,
                    )
                ),
                name="automatic_template_activation_evidence",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.state != self.State.DRAFT:
                changed = [
                    field
                    for field in self.IMMUTABLE_AFTER_ACTIVATION
                    if getattr(previous, field) != getattr(self, field)
                ]
                if changed:
                    raise ValidationError(
                        "Activated automatic-message template content is immutable; "
                        "create a new draft version instead."
                    )
                if self.state not in {self.State.ACTIVE, self.State.RETIRED}:
                    raise ValidationError(
                        "An activated automatic-message template cannot return to draft."
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Automatic-message template versions are retained as legal evidence."
        )


class CorrespondenceDenialCitationQuerySet(models.QuerySet):
    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objs = list(objs)
        correspondence_ids = {
            basis.correspondence_id
            for basis in objs
            if basis.correspondence_id is not None
        }
        with transaction.atomic(using=self.db):
            from apps.records.models import ExemptionCitation

            parents = list(
                Correspondence.objects.using(self.db)
                .select_for_update()
                .filter(pk__in=correspondence_ids)
                .order_by("pk")
                .values_list("pk", "kind", "status")
            )
            if (
                len(parents) != len(correspondence_ids)
                or any(
                    kind != Correspondence.Kind.DENIAL
                    or status != Correspondence.Status.DRAFT
                    for _, kind, status in parents
                )
            ):
                raise ValidationError(
                    "Denial citation snapshots may be added only to denial drafts."
                )
            exemptions = {
                exemption.pk: exemption
                for exemption in ExemptionCitation.objects.using(self.db).filter(
                    pk__in={
                        basis.exemption_id
                        for basis in objs
                        if basis.exemption_id is not None
                    }
                )
            }
            if len(exemptions) != len(
                {
                    basis.exemption_id
                    for basis in objs
                    if basis.exemption_id is not None
                }
            ):
                raise ValidationError(
                    "Every denial basis must reference an exemption citation."
                )
            for basis in objs:
                exemption = exemptions.get(basis.exemption_id)
                if exemption is None:
                    continue
                basis.citation_snapshot = exemption.citation
                basis.short_name_snapshot = exemption.short_name
                basis.description_snapshot = exemption.description
                basis.requires_second_review_snapshot = (
                    exemption.requires_second_review
                )
            return super().bulk_create(
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

    def update(self, **kwargs):
        raise ValidationError(
            "Denial citation snapshots must be replaced through the locked "
            "draft-edit service."
        )

    def delete(self):
        with transaction.atomic(using=self.db):
            targets = list(
                self.order_by("pk").values_list("pk", "correspondence_id")
            )
            if not targets:
                return 0, {}
            correspondence_ids = {
                correspondence_id for _, correspondence_id in targets
            }
            parents = list(
                Correspondence.objects.using(self.db)
                .select_for_update()
                .filter(pk__in=correspondence_ids)
                .order_by("pk")
                .values_list("pk", "kind", "status")
            )
            if (
                len(parents) != len(correspondence_ids)
                or any(
                    kind != Correspondence.Kind.DENIAL
                    or status != Correspondence.Status.DRAFT
                    for _, kind, status in parents
                )
            ):
                raise ValidationError(
                    "Denial citation snapshots are immutable after approval."
                )
            raw_queryset = models.QuerySet(
                model=self.model,
                using=self.db,
            ).filter(pk__in=[pk for pk, _ in targets])
            return models.QuerySet.delete(raw_queryset)


class CorrespondenceDenialCitation(models.Model):
    """Structured exemption authority supporting a full-denial notice."""

    correspondence = models.ForeignKey(
        Correspondence,
        on_delete=models.CASCADE,
        related_name="denial_citations",
    )
    exemption = models.ForeignKey(
        "records.ExemptionCitation",
        on_delete=models.PROTECT,
        related_name="denial_correspondence_bases",
    )
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="selected_denial_citations",
    )
    citation_snapshot = models.CharField(max_length=100)
    short_name_snapshot = models.CharField(max_length=200)
    description_snapshot = models.TextField(blank=True)
    requires_second_review_snapshot = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CorrespondenceDenialCitationQuerySet.as_manager()

    class Meta:
        ordering = ["exemption__citation", "id"]
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["correspondence", "exemption"],
                name="correspondence_denial_citation_unique",
            ),
            models.CheckConstraint(
                condition=~Q(citation_snapshot=""),
                name="correspondence_denial_citation_text_present",
            ),
            models.CheckConstraint(
                condition=~Q(short_name_snapshot=""),
                name="correspondence_denial_short_name_present",
            ),
        ]

    def _locked_parent(self):
        return Correspondence.objects.select_for_update().only(
            "kind",
            "status",
        ).get(pk=self.correspondence_id)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Denial citation snapshots must be replaced through the locked "
                "draft-edit service."
            )
        with transaction.atomic():
            parent = self._locked_parent()
            if parent.status != Correspondence.Status.DRAFT:
                raise ValidationError(
                    "Denial citation snapshots are immutable after approval."
                )
            if parent.kind != Correspondence.Kind.DENIAL:
                raise ValidationError(
                    "Denial citation snapshots may be added only to denial drafts."
                )
            if self.exemption_id is not None:
                exemption = self.exemption
                self.citation_snapshot = exemption.citation
                self.short_name_snapshot = exemption.short_name
                self.description_snapshot = exemption.description
                self.requires_second_review_snapshot = (
                    exemption.requires_second_review
                )
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            parent = self._locked_parent()
            if parent.status != Correspondence.Status.DRAFT:
                raise ValidationError(
                    "Denial citation snapshots are immutable after approval."
                )
            return super().delete(*args, **kwargs)


class CorrespondenceRecipientQuerySet(models.QuerySet):
    _CORRESPONDENCE_FIELDS = {"correspondence", "correspondence_id"}
    _IMMUTABLE_MESSAGE = "Recipient snapshots are immutable after approval."
    _REASSIGNMENT_MESSAGE = "Recipient snapshots cannot be reassigned."

    def _lock_editable_correspondences(self, correspondence_ids):
        correspondence_ids = {
            correspondence_id
            for correspondence_id in correspondence_ids
            if correspondence_id is not None
        }
        if not correspondence_ids:
            return
        locked = list(
            Correspondence.objects.using(self.db)
            .select_for_update()
            .filter(pk__in=correspondence_ids)
            .order_by("pk")
            .values_list("pk", "status")
        )
        if (
            len(locked) != len(correspondence_ids)
            or any(
                status != Correspondence.Status.DRAFT
                for _, status in locked
            )
        ):
            raise ValidationError(self._IMMUTABLE_MESSAGE)

    def _frozen_targets(self):
        return list(self.values_list("pk", "correspondence_id"))

    def _assert_correspondence_unchanged(self, fields):
        if self._CORRESPONDENCE_FIELDS.intersection(fields):
            raise ValidationError(self._REASSIGNMENT_MESSAGE)

    def _locked_frozen_queryset(self):
        targets = self._frozen_targets()
        self._lock_editable_correspondences(
            correspondence_id for _, correspondence_id in targets
        )
        return self.model._base_manager.using(self.db).filter(
            pk__in=[pk for pk, _ in targets]
        )

    def update(self, **kwargs):
        self._assert_correspondence_unchanged(kwargs)
        with transaction.atomic(using=self.db):
            frozen = self._locked_frozen_queryset()
            return models.QuerySet.update(frozen, **kwargs)

    def delete(self):
        with transaction.atomic(using=self.db):
            frozen = self._locked_frozen_queryset()
            return models.QuerySet.delete(frozen)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        self._assert_correspondence_unchanged(fields)
        with transaction.atomic(using=self.db):
            targets = list(
                self.model._base_manager.using(self.db)
                .filter(pk__in=[obj.pk for obj in objs])
                .values_list("pk", "correspondence_id")
            )
            self._lock_editable_correspondences(
                correspondence_id for _, correspondence_id in targets
            )
            return super().bulk_update(
                objs,
                fields,
                batch_size=batch_size,
            )

    @staticmethod
    def _validate_recipient(recipient):
        if any(
            unicodedata.category(character).startswith("C")
            for character in recipient.display_name
        ):
            raise ValidationError(
                "Recipient display names cannot contain control characters."
            )

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objs = list(objs)
        self._assert_correspondence_unchanged(update_fields or ())
        for recipient in objs:
            self._validate_recipient(recipient)
        correspondence_ids = {
            recipient.correspondence_id
            for recipient in objs
            if recipient.correspondence_id is not None
        }
        with transaction.atomic(using=self.db):
            self._lock_editable_correspondences(correspondence_ids)
            return super().bulk_create(
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )


class CorrespondenceRecipient(models.Model):
    """Exact recipient snapshot used for one outbound correspondence."""

    class RecipientType(models.TextChoices):
        TO = "to", "To"
        CC = "cc", "Cc"
        BCC = "bcc", "Bcc"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correspondence = models.ForeignKey(
        Correspondence,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    recipient_type = models.CharField(max_length=10, choices=RecipientType.choices)
    email = models.EmailField()
    display_name = models.CharField(max_length=255, blank=True)
    requester = models.ForeignKey(
        "records.Requester",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="correspondence_recipient_snapshots",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CorrespondenceRecipientQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["correspondence", "recipient_type", "email"],
                name="correspondence_recipient_snapshot_unique",
            )
        ]

    def _validate_display_name(self):
        CorrespondenceRecipientQuerySet._validate_recipient(self)

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(
            type(self),
            instance=self,
        )
        with transaction.atomic(using=using):
            recipients = type(self).objects.using(using)
            if self._state.adding:
                correspondence_id = self.correspondence_id
            else:
                correspondence_id = (
                    recipients.filter(pk=self.pk)
                    .values_list("correspondence_id", flat=True)
                    .get()
                )
                if self.correspondence_id != correspondence_id:
                    raise ValidationError(
                        CorrespondenceRecipientQuerySet._REASSIGNMENT_MESSAGE
                    )
            recipients._lock_editable_correspondences({correspondence_id})
            self._validate_display_name()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(
            type(self),
            instance=self,
        )
        with transaction.atomic(using=using):
            recipients = type(self).objects.using(using)
            correspondence_id = (
                recipients.filter(pk=self.pk)
                .values_list("correspondence_id", flat=True)
                .get()
            )
            recipients._lock_editable_correspondences({correspondence_id})
            return super().delete(*args, **kwargs)


class DeliveryQuerySet(models.QuerySet):
    IMMUTABLE_FIELDS = {
        "correspondence",
        "correspondence_id",
        "provider",
        "provider_account",
        "idempotency_key",
        "queued_at",
    }

    def update(self, **kwargs):
        if self.IMMUTABLE_FIELDS.intersection(kwargs):
            raise ValidationError("Delivery transport identity is immutable.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Delivery jobs are immutable and cannot be deleted.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError(
                "Delivery rows cannot be rewritten through a conflict upsert."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class CorrespondenceDelivery(models.Model):
    """Lease-controlled transport job; retries reuse this job and Message-ID."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CLAIMED = "claimed", "Claimed"
        AWAITING_RECONCILIATION = "awaiting_reconciliation", "Awaiting reconciliation"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        RETRYABLE = "retryable", "Retryable"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correspondence = models.OneToOneField(
        Correspondence,
        on_delete=models.PROTECT,
        related_name="delivery",
    )
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=50, default="gmail")
    provider_account = models.CharField(max_length=100, default="default")
    idempotency_key = models.CharField(max_length=255, unique=True)
    claim_owner = models.CharField(max_length=255, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    successful_attempt_number = models.PositiveIntegerField(null=True, blank=True)
    last_failure_category = models.CharField(max_length=100, blank=True)
    last_failure_description = models.CharField(max_length=1000, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_thread_id = models.CharField(max_length=255, blank=True)
    queued_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    recovery_not_before = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)

    objects = DeliveryQuerySet.as_manager()

    class Meta:
        ordering = ["queued_at", "id"]
        base_manager_name = "objects"
        permissions = [
            (
                "deliver_outbound_correspondence",
                "Can claim and report outbound correspondence deliveries",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "queued_at"]),
            models.Index(fields=["claim_expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "pending",
                        "claimed",
                        "awaiting_reconciliation",
                        "sent",
                        "failed",
                        "retryable",
                        "cancelled",
                    ]
                ),
                name="correspondence_delivery_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="correspondence_delivery_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="claimed")
                    | (~Q(claim_owner="") & Q(claim_expires_at__isnull=False))
                ),
                name="correspondence_delivery_claim_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(claim_owner="", claim_expires_at__isnull=True)
                    | (~Q(claim_owner="") & Q(claim_expires_at__isnull=False))
                ),
                name="correspondence_delivery_claim_fields_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=["claimed", "awaiting_reconciliation"])
                    | Q(claim_owner="", claim_expires_at__isnull=True)
                ),
                name="correspondence_delivery_claim_state_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="sent")
                    | (
                        ~Q(provider_message_id="")
                        & Q(completed_at__isnull=False)
                        & Q(successful_attempt_number__gte=1)
                    )
                ),
                name="correspondence_delivery_sent_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="sent") | Q(successful_attempt_number__isnull=True)
                ),
                name="correspondence_delivery_success_only_when_sent",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_account", "provider_message_id"],
                condition=~Q(provider_message_id=""),
                name="correspondence_delivery_provider_message_unique",
            ),
        ]

    def _validate_correspondence_direction(self):
        if self.correspondence_id and self.correspondence.direction != Correspondence.Direction.OUTBOUND:
            raise ValidationError("Only outbound correspondence may have a delivery job.")

    def clean(self):
        super().clean()
        self._validate_correspondence_direction()

    def save(self, *args, **kwargs):
        self._validate_correspondence_direction()
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "correspondence_id",
                "provider",
                "provider_account",
                "idempotency_key",
                "queued_at",
            )
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in immutable_fields
            ):
                raise ValidationError("Delivery transport identity is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Delivery jobs are immutable and cannot be deleted.")


class DeliveryAttemptQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Delivery attempts are append-only and cannot be deleted.")

    def update(self, **kwargs):
        raise ValidationError("Delivery attempts must be finalized through an instance save.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError(
                "Delivery attempts cannot be rewritten through a conflict upsert."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class CorrespondenceDeliveryAttempt(models.Model):
    """One claim/send/result attempt, frozen permanently once finalized."""

    class Action(models.TextChoices):
        SEND = "send", "Send"
        RECONCILE = "reconcile", "Reconcile"

    class ResultCategory(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Unknown send outcome"
        RECONCILED = "reconciled", "Reconciled"
        NOT_FOUND = "not_found", "Not found during reconciliation"
        CANCELLED = "cancelled", "Cancelled"
        LEASE_EXPIRED = "lease_expired", "Lease expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(
        CorrespondenceDelivery,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    action = models.CharField(
        max_length=20,
        choices=Action.choices,
        default=Action.SEND,
    )
    claim_owner = models.CharField(max_length=255)
    claimed_at = models.DateTimeField()
    result_at = models.DateTimeField(null=True, blank=True)
    result_category = models.CharField(
        max_length=30,
        choices=ResultCategory.choices,
        blank=True,
    )
    provider_response_metadata = models.JSONField(default=dict, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_thread_id = models.CharField(max_length=255, blank=True)
    provider_timestamp = models.DateTimeField(null=True, blank=True)
    error_category = models.CharField(max_length=100, blank=True)
    result_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="correspondence_delivery_attempts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = DeliveryAttemptQuerySet.as_manager()

    class Meta:
        ordering = ["attempt_number"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=Q(action__in=["send", "reconcile"]),
                name="correspondence_delivery_attempt_action_valid",
            ),
            models.UniqueConstraint(
                fields=["delivery", "attempt_number"],
                name="correspondence_delivery_attempt_number_unique",
            ),
            models.UniqueConstraint(
                fields=["delivery"],
                condition=Q(result_at__isnull=True),
                name="correspondence_one_open_delivery_attempt",
            ),
            models.UniqueConstraint(
                fields=["delivery"],
                condition=Q(result_category__in=["sent", "reconciled"]),
                name="correspondence_one_successful_delivery_attempt",
            ),
            models.CheckConstraint(
                condition=Q(attempt_number__gte=1),
                name="correspondence_delivery_attempt_positive",
            ),
            models.CheckConstraint(
                condition=~Q(claim_owner=""),
                name="correspondence_delivery_attempt_owner_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(result_at__isnull=True)
                    | Q(result_at__gte=models.F("claimed_at"))
                ),
                name="correspondence_delivery_attempt_result_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(result_at__isnull=True, result_category="")
                    | Q(
                        result_at__isnull=False,
                        result_category__in=[
                            "sent",
                            "failed",
                            "unknown",
                            "reconciled",
                            "not_found",
                            "cancelled",
                            "lease_expired",
                        ],
                    )
                ),
                name="correspondence_delivery_attempt_result_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(result_at__isnull=True, result_fingerprint="")
                    | Q(
                        result_at__isnull=False,
                        result_fingerprint__regex=r"\A[0-9a-f]{64}\Z",
                    )
                ),
                name="correspondence_delivery_attempt_fingerprint_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(result_category__in=["reconciled", "not_found"])
                    | Q(action="reconcile")
                ),
                name="correspondence_delivery_attempt_reconcile_result",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(result_category__in=["sent", "failed"])
                    | Q(action="send")
                ),
                name="correspondence_delivery_attempt_send_result",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(result_category__in=["sent", "reconciled"])
                        & ~Q(provider_message_id="")
                        & ~Q(provider_thread_id="")
                        & Q(provider_timestamp__isnull=False)
                    )
                    | (
                        ~Q(result_category__in=["sent", "reconciled"])
                        & Q(provider_message_id="")
                        & Q(provider_thread_id="")
                        & Q(provider_timestamp__isnull=True)
                    )
                ),
                name="correspondence_delivery_attempt_success_evidence",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            identity_fields = (
                "delivery_id",
                "attempt_number",
                "action",
                "claim_owner",
                "claimed_at",
                "actor_id",
                "created_at",
            )
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in identity_fields
            ):
                raise ValidationError("Delivery attempt identity is immutable.")
            if previous.result_at is not None:
                immutable_fields = (
                    "result_at",
                    "result_category",
                    "provider_response_metadata",
                    "provider_message_id",
                    "provider_thread_id",
                    "provider_timestamp",
                    "error_category",
                    "result_fingerprint",
                )
                if any(
                    getattr(previous, field) != getattr(self, field)
                    for field in immutable_fields
                ):
                    raise ValidationError("Completed delivery attempts are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Delivery attempts are append-only and cannot be deleted.")


class InboundCorrespondenceReceiptQuerySet(models.QuerySet):
    IMMUTABLE_EVIDENCE_FIELDS = {
        "provider",
        "provider_account",
        "provider_message_id",
        "provider_thread_id",
        "rfc_message_id",
        "in_reply_to",
        "references",
        "received_at",
        "envelope_sender",
        "envelope_recipients",
        "raw_message_size",
        "raw_message_sha256",
        "raw_storage_key",
        "payload_sha256",
        "idempotency_key",
        "ip_address",
        "submitted_by",
        "submitted_by_id",
        "matched_correspondence",
        "matched_correspondence_id",
        "created_at",
    }
    MATCHING_ASSOCIATION_FIELDS = {
        "matching_status",
        "match_method",
        "delivery_status_original_message_id",
        "matched_delivery",
        "matched_delivery_id",
        "candidate_request",
        "candidate_request_id",
        "matched_request",
        "matched_request_id",
        "matched_at",
        "matched_by",
        "matched_by_id",
        "duplicate_of",
        "duplicate_of_id",
        "version",
    }
    PARSING_FINALIZATION_FIELDS = {
        "parsing_status",
        "quarantined",
        "quarantine_reason",
        "parsing_error",
        "completed_at",
    }

    def update(self, **kwargs):
        if self.IMMUTABLE_EVIDENCE_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Inbound receipt transport identity and raw evidence are immutable."
            )
        if self.PARSING_FINALIZATION_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Inbound receipt parsing evidence may change only through the "
                "locked one-shot finalization workflow."
            )
        if self.MATCHING_ASSOCIATION_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Inbound receipt associations may change only through the "
                "locked matching workflow."
            )
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError(
            "Inbound correspondence receipts are immutable and cannot be deleted."
        )

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError(
                "Inbound receipt evidence cannot be rewritten through a "
                "conflict upsert."
            )
        for receipt in objs:
            receipt.raw_storage_key = _raw_message_storage_key()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def create_from_quarantine_reservation(self, *, reservation, **kwargs):
        """Create raw evidence with the pre-upload key inside its adoption tx."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Inbound receipt reservation adoption requires a transaction."
            )
        reservation = (
            InboundQuarantineWriteReservation.objects.select_for_update()
            .get(pk=reservation.pk)
        )
        if reservation.adopted_at is not None:
            raise InboundQuarantineReservationAlreadyAdopted(
                "Raw inbound quarantine reservation was already adopted."
            )
        if (
            reservation.kind
            != InboundQuarantineWriteReservation.Kind.RAW_MESSAGE
            or reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
            or kwargs.get("raw_message_size") != reservation.size
            or kwargs.get("raw_message_sha256") != reservation.sha256
        ):
            raise ValidationError(
                "Raw inbound evidence does not match its quarantine reservation."
            )
        receipt = self.model(**kwargs)
        receipt.raw_storage_key = reservation.storage_key
        models.Model.save(
            receipt,
            force_insert=True,
            using=self.db,
        )
        InboundQuarantineWriteReservation.objects.adopt_receipt_locked(
            reservation_id=reservation.pk,
            receipt_id=receipt.pk,
            adopted_at=timezone.now(),
        )
        return receipt

    def _set_locked_matching_state(
        self,
        *,
        matching_status,
        match_method="",
        candidate_request_id=None,
        matched_request_id=None,
        matched_at=None,
        matched_by_id=None,
    ):
        """Set one receipt's complete matching state inside a lock-owning tx."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError("Inbound receipt matching requires a transaction.")
        self.select_for_update().only("pk").get()

        if matching_status == "matched":
            if (
                matched_request_id is None
                or matched_at is None
                or not match_method
            ):
                raise ValidationError("Matched receipt evidence is incomplete.")
            candidate_request_id = None
        elif matching_status == "candidate":
            # A candidate is a request number the requester wrote, in the
            # subject or the body; nothing else may propose a case.
            if candidate_request_id is None or match_method not in {
                "subject",
                "body",
            }:
                raise ValidationError("Candidate receipt evidence is incomplete.")
            matched_request_id = None
            matched_at = None
            matched_by_id = None
        elif matching_status in {"unmatched", "ambiguous"}:
            match_method = ""
            candidate_request_id = None
            matched_request_id = None
            matched_at = None
            matched_by_id = None
        else:
            raise ValidationError(
                "The matcher cannot assign this receipt matching state."
            )

        return super().update(
            matching_status=matching_status,
            match_method=match_method,
            delivery_status_original_message_id="",
            matched_delivery_id=None,
            candidate_request_id=candidate_request_id,
            matched_request_id=matched_request_id,
            matched_at=matched_at,
            matched_by_id=matched_by_id,
            duplicate_of_id=None,
            version=models.F("version") + 1,
        )

    def _set_locked_delivery_status_state(
        self,
        *,
        original_message_id,
        matching_status,
        matched_request_id=None,
        matched_delivery_id=None,
        matched_at=None,
    ):
        """Record one normalized DSN target and its exact delivery match."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Delivery-status matching requires a transaction."
            )
        self.select_for_update().only("pk").get()
        original_message_id = str(original_message_id or "")
        if (
            len(original_message_id) > 998
            or _RFC_MESSAGE_ID.fullmatch(original_message_id) is None
        ):
            raise ValidationError(
                "A strict reported original Message-ID is required."
            )
        if matching_status == "matched":
            if (
                matched_request_id is None
                or matched_delivery_id is None
                or matched_at is None
            ):
                raise ValidationError(
                    "Matched delivery-status evidence is incomplete."
                )
            match_method = "delivery_status"
        elif matching_status in {"unmatched", "ambiguous"}:
            match_method = ""
            matched_request_id = None
            matched_delivery_id = None
            matched_at = None
        else:
            raise ValidationError(
                "Delivery-status evidence may be matched, unmatched, or ambiguous."
            )
        return super().update(
            matching_status=matching_status,
            match_method=match_method,
            delivery_status_original_message_id=original_message_id,
            matched_delivery_id=matched_delivery_id,
            candidate_request_id=None,
            matched_request_id=matched_request_id,
            matched_at=matched_at,
            matched_by_id=None,
            duplicate_of_id=None,
            version=models.F("version") + 1,
        )

    def _record_locked_clarification_association(
        self,
        *,
        matched_request_id,
        matched_correspondence_id,
    ):
        """Version an explicit cycle association without rewriting match proof."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Clarification response association requires a transaction."
            )
        receipt = self.select_for_update().only(
            "matching_status",
            "matched_request_id",
            "matched_correspondence_id",
        ).get()
        if (
            receipt.matching_status != "matched"
            or receipt.matched_request_id != matched_request_id
            or receipt.matched_correspondence_id != matched_correspondence_id
        ):
            raise ValidationError(
                "Only a matched inbound receipt may be associated with a "
                "clarification cycle."
            )
        return super().update(version=models.F("version") + 1)

    def finalize_locked_parsing(
        self,
        *,
        parsing_status,
        completed_at,
        matched_correspondence_id=None,
        rfc_message_id="",
        in_reply_to="",
        references=(),
        quarantine_reason="",
        parsing_error="",
        duplicate_of_id=None,
    ):
        """Finalize one pending receipt exactly once under a row lock."""
        if completed_at is None:
            raise ValidationError("Inbound parsing completion time is required.")
        with transaction.atomic(using=self.db):
            receipt = self.select_for_update().get()
            if (
                receipt.parsing_status != "pending"
                or receipt.completed_at is not None
                or receipt.matching_status != "unmatched"
                or receipt.matched_correspondence_id is not None
            ):
                raise ValidationError(
                    "This inbound receipt parsing attempt is already finalized."
                )

            common_updates = {
                "parsing_status": parsing_status,
                "completed_at": completed_at,
                "candidate_request_id": None,
                "matched_request_id": None,
                "matched_delivery_id": None,
                "delivery_status_original_message_id": "",
                "matched_at": None,
                "matched_by_id": None,
                "match_method": "",
                "version": models.F("version") + 1,
            }
            if parsing_status == "parsed":
                if matched_correspondence_id is None:
                    raise ValidationError(
                        "Parsed inbound receipts require an immutable "
                        "correspondence snapshot."
                    )
                correspondence = (
                    Correspondence.objects.select_for_update()
                    .only("direction", "status")
                    .get(pk=matched_correspondence_id)
                )
                if (
                    correspondence.direction
                    != Correspondence.Direction.INBOUND
                    or correspondence.status
                    != Correspondence.Status.UNMATCHED
                ):
                    raise ValidationError(
                        "Parsed receipt content must be unmatched inbound "
                        "correspondence."
                    )
                if duplicate_of_id is not None:
                    raise ValidationError(
                        "Parsed receipts cannot claim duplicate evidence."
                    )
                if quarantine_reason or parsing_error:
                    raise ValidationError(
                        "Released parsed receipts cannot retain parsing or "
                        "quarantine errors."
                    )
                common_updates.update(
                    matching_status="unmatched",
                    matched_correspondence_id=matched_correspondence_id,
                    rfc_message_id=rfc_message_id or "",
                    in_reply_to=in_reply_to or "",
                    references=list(references or ()),
                    quarantined=False,
                    quarantine_reason="",
                    parsing_error="",
                    duplicate_of_id=None,
                )
            elif parsing_status in {"failed", "quarantined"}:
                if matched_correspondence_id is not None or duplicate_of_id is not None:
                    raise ValidationError(
                        "Failed parsing cannot attach correspondence or "
                        "duplicate evidence."
                    )
                if not (quarantine_reason or parsing_error):
                    raise ValidationError(
                        "Failed inbound parsing requires a safe failure reason."
                    )
                common_updates.update(
                    matching_status="quarantined",
                    matched_correspondence_id=None,
                    rfc_message_id="",
                    in_reply_to="",
                    references=[],
                    quarantined=True,
                    quarantine_reason=quarantine_reason,
                    parsing_error=parsing_error,
                    duplicate_of_id=None,
                )
            elif parsing_status == "duplicate":
                if matched_correspondence_id is not None or duplicate_of_id is None:
                    raise ValidationError(
                        "Duplicate parsing requires only an earlier receipt."
                    )
                if duplicate_of_id == receipt.pk:
                    raise ValidationError(
                        "An inbound receipt cannot duplicate itself."
                    )
                duplicate = self.model.objects.select_for_update().only(
                    "pk",
                    "submitted_by_id",
                ).get(pk=duplicate_of_id)
                if duplicate.submitted_by_id != receipt.submitted_by_id:
                    raise ValidationError(
                        "Duplicate receipt evidence must share the submitting "
                        "integration identity."
                    )
                common_updates.update(
                    matching_status="duplicate",
                    matched_correspondence_id=None,
                    rfc_message_id="",
                    in_reply_to="",
                    references=[],
                    quarantined=True,
                    quarantine_reason=(
                        quarantine_reason or "Duplicate inbound receipt."
                    ),
                    parsing_error="",
                    duplicate_of_id=duplicate.pk,
                )
            else:
                raise ValidationError(
                    "The inbound parser cannot finalize this parsing status."
                )

            super().update(**common_updates)
            return self.model.objects.get(pk=receipt.pk)

    def release_reparsed_locked(
        self,
        *,
        completed_at,
        matched_correspondence_id,
        rfc_message_id="",
        in_reply_to="",
        references=(),
    ):
        """Release one whole-message quarantine the corrected parser now reads.

        The only transition out of a parsing quarantine. Raw evidence and
        transport identity are untouched; the receipt gains the correspondence
        snapshot it would have had if the parser had accepted it on arrival.
        """
        if completed_at is None:
            raise ValidationError("Inbound parsing completion time is required.")
        with transaction.atomic(using=self.db):
            receipt = self.select_for_update().get()
            if (
                receipt.parsing_status not in {"failed", "quarantined"}
                or receipt.matching_status != "quarantined"
                or not receipt.quarantined
                or receipt.matched_correspondence_id is not None
                or receipt.duplicate_of_id is not None
            ):
                raise ValidationError(
                    "Only a whole-message parsing quarantine can be re-parsed."
                )
            correspondence = (
                Correspondence.objects.select_for_update()
                .only("direction", "status")
                .get(pk=matched_correspondence_id)
            )
            if (
                correspondence.direction != Correspondence.Direction.INBOUND
                or correspondence.status != Correspondence.Status.UNMATCHED
            ):
                raise ValidationError(
                    "Parsed receipt content must be unmatched inbound "
                    "correspondence."
                )
            super().update(
                parsing_status="parsed",
                completed_at=completed_at,
                matching_status="unmatched",
                match_method="",
                matched_correspondence_id=matched_correspondence_id,
                rfc_message_id=rfc_message_id or "",
                in_reply_to=in_reply_to or "",
                references=list(references or ()),
                quarantined=False,
                quarantine_reason="",
                parsing_error="",
                version=models.F("version") + 1,
            )
            return self.model.objects.get(pk=receipt.pk)


class InboundCorrespondenceReceipt(models.Model):
    """Idempotent raw inbound transport receipt, retained before matching."""

    class ParsingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PARSED = "parsed", "Parsed"
        FAILED = "failed", "Failed"
        QUARANTINED = "quarantined", "Quarantined"
        DUPLICATE = "duplicate", "Duplicate"

    class MatchingStatus(models.TextChoices):
        UNMATCHED = "unmatched", "Unmatched"
        CANDIDATE = "candidate", "Candidate"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        MATCHED = "matched", "Matched"
        QUARANTINED = "quarantined", "Quarantined"
        DUPLICATE = "duplicate", "Duplicate"

    class MatchMethod(models.TextChoices):
        REPLY_TOKEN = "reply_token", "Reply token"
        IN_REPLY_TO = "in_reply_to", "In-Reply-To"
        PROVIDER_THREAD = "provider_thread", "Provider thread"
        REFERENCES = "references", "References"
        DELIVERY_STATUS = "delivery_status", "Delivery status"
        SUBJECT = "subject", "Request number in subject"
        BODY = "body", "Request number in body"
        EMAIL_INTAKE = "email_intake", "Email intake"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50, default="gmail")
    provider_account = models.CharField(max_length=100, default="default")
    provider_message_id = models.CharField(max_length=255)
    provider_thread_id = models.CharField(max_length=255, blank=True)
    rfc_message_id = models.CharField(max_length=998, blank=True)
    in_reply_to = models.CharField(max_length=998, blank=True)
    references = models.JSONField(default=list, blank=True)
    received_at = models.DateTimeField()
    envelope_sender = models.EmailField(blank=True)
    envelope_recipients = models.JSONField(default=list, blank=True)
    parsing_status = models.CharField(
        max_length=20,
        choices=ParsingStatus.choices,
        default=ParsingStatus.PENDING,
    )
    matching_status = models.CharField(
        max_length=20,
        choices=MatchingStatus.choices,
        default=MatchingStatus.UNMATCHED,
    )
    match_method = models.CharField(max_length=30, choices=MatchMethod.choices, blank=True)
    delivery_status_original_message_id = models.CharField(
        max_length=998,
        blank=True,
    )
    candidate_request = models.ForeignKey(
        "records.PublicRecordsRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="candidate_inbound_correspondence_receipts",
    )
    matched_request = models.ForeignKey(
        "records.PublicRecordsRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inbound_correspondence_receipts",
    )
    matched_delivery = models.ForeignKey(
        CorrespondenceDelivery,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inbound_delivery_status_receipts",
    )
    matched_correspondence = models.ForeignKey(
        Correspondence,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inbound_receipts",
    )
    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matched_inbound_correspondence_receipts",
    )
    duplicate_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="duplicate_receipts",
    )
    quarantined = models.BooleanField(default=True)
    quarantine_reason = models.CharField(max_length=500, blank=True)
    parsing_error = models.CharField(max_length=1000, blank=True)
    raw_message_size = models.PositiveBigIntegerField()
    raw_message_sha256 = models.CharField(max_length=64)
    raw_storage_key = models.CharField(
        max_length=1000,
        default=_raw_message_storage_key,
        editable=False,
        unique=True,
    )
    payload_sha256 = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=200)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_inbound_correspondence_receipts",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = InboundCorrespondenceReceiptQuerySet.as_manager()

    class Meta:
        ordering = ["received_at", "id"]
        base_manager_name = "objects"
        permissions = [
            (
                "submit_inbound_correspondence",
                "Can submit inbound correspondence messages",
            ),
        ]
        indexes = [
            models.Index(fields=["matching_status", "received_at"]),
            models.Index(fields=["provider_thread_id"]),
            models.Index(fields=["rfc_message_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_account", "provider_message_id"],
                name="correspondence_inbound_provider_message_unique",
            ),
            models.UniqueConstraint(
                fields=["submitted_by", "idempotency_key"],
                name="correspondence_inbound_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="correspondence_inbound_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(provider="")
                    & ~Q(provider_account="")
                    & ~Q(provider_message_id="")
                    & Q(
                        idempotency_key__regex=(
                            r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
                        )
                    )
                    & Q(raw_message_size__gte=1)
                ),
                name="correspondence_inbound_identity_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(raw_message_sha256__regex=r"\A[0-9a-f]{64}\Z")
                    & Q(payload_sha256__regex=r"\A[0-9a-f]{64}\Z")
                ),
                name="correspondence_inbound_hashes_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(
                            matching_status="matched",
                            parsing_status="parsed",
                            quarantined=False,
                            quarantine_reason="",
                            parsing_error="",
                        )
                        & Q(matched_request__isnull=False)
                        & Q(matched_correspondence__isnull=False)
                        & Q(matched_at__isnull=False)
                        & Q(candidate_request__isnull=True)
                        & Q(duplicate_of__isnull=True)
                        & (
                            Q(
                                match_method="manual",
                                matched_by__isnull=False,
                            )
                            | Q(
                                match_method__in=[
                                    "reply_token",
                                    "in_reply_to",
                                    "provider_thread",
                                    "references",
                                    "delivery_status",
                                    "email_intake",
                                ],
                                matched_by__isnull=True,
                            )
                        )
                    )
                    | Q(
                        matching_status="candidate",
                        parsing_status="parsed",
                        quarantined=False,
                        quarantine_reason="",
                        parsing_error="",
                        match_method__in=["subject", "body"],
                        candidate_request__isnull=False,
                        matched_correspondence__isnull=False,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=True,
                    )
                    | Q(
                        matching_status="duplicate",
                        parsing_status="duplicate",
                        quarantined=True,
                        parsing_error="",
                        match_method="",
                        candidate_request__isnull=True,
                        matched_correspondence__isnull=True,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=False,
                    )
                    | (
                        Q(
                            matching_status="unmatched",
                            parsing_status="pending",
                            quarantined=True,
                            quarantine_reason="",
                            parsing_error="",
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=True,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                    )
                    | (
                        Q(
                            matching_status="unmatched",
                            parsing_status="parsed",
                            quarantined=False,
                            quarantine_reason="",
                            parsing_error="",
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=False,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                    )
                    | Q(
                        matching_status="ambiguous",
                        parsing_status="parsed",
                        quarantined=False,
                        quarantine_reason="",
                        parsing_error="",
                        match_method="",
                        candidate_request__isnull=True,
                        matched_correspondence__isnull=False,
                        matched_request__isnull=True,
                        matched_at__isnull=True,
                        matched_by__isnull=True,
                        duplicate_of__isnull=True,
                    )
                    | (
                        Q(
                            matching_status="quarantined",
                            parsing_status__in=["failed", "quarantined"],
                            quarantined=True,
                            match_method="",
                            candidate_request__isnull=True,
                            matched_correspondence__isnull=True,
                            matched_request__isnull=True,
                            matched_at__isnull=True,
                            matched_by__isnull=True,
                            duplicate_of__isnull=True,
                        )
                        & (
                            ~Q(quarantine_reason="")
                            | ~Q(parsing_error="")
                        )
                    )
                ),
                name="correspondence_inbound_state_exact",
            ),
            models.CheckConstraint(
                condition=Q(
                    parsing_status__in=[
                        "pending",
                        "parsed",
                        "failed",
                        "quarantined",
                        "duplicate",
                    ]
                ),
                name="correspondence_inbound_parsing_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(parsing_status="pending", completed_at__isnull=True)
                    | (
                        ~Q(parsing_status="pending")
                        & Q(completed_at__isnull=False)
                    )
                ),
                name="correspondence_inbound_parsing_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(parsing_status="duplicate", matching_status="duplicate")
                    | (
                        ~Q(parsing_status="duplicate")
                        & ~Q(matching_status="duplicate")
                    )
                ),
                name="correspondence_inbound_duplicate_state",
            ),
            models.CheckConstraint(
                condition=~Q(duplicate_of=models.F("id")),
                name="correspondence_inbound_not_self_duplicate",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        matching_status="matched",
                        match_method="delivery_status",
                        matched_delivery__isnull=False,
                    )
                    & ~Q(delivery_status_original_message_id="")
                    | (
                        ~Q(match_method="delivery_status")
                        & Q(matched_delivery__isnull=True)
                    )
                ),
                name="correspondence_inbound_dsn_delivery_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.raw_storage_key = _raw_message_storage_key()
        else:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "provider",
                "provider_account",
                "provider_message_id",
                "provider_thread_id",
                "rfc_message_id",
                "in_reply_to",
                "references",
                "received_at",
                "envelope_sender",
                "envelope_recipients",
                "raw_message_size",
                "raw_message_sha256",
                "raw_storage_key",
                "payload_sha256",
                "idempotency_key",
                "ip_address",
                "submitted_by_id",
                "matched_correspondence_id",
                "created_at",
            )
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in immutable_fields
            ):
                raise ValidationError(
                    "Inbound receipt transport identity and raw evidence are "
                    "immutable."
                )
            parsing_fields = (
                "parsing_status",
                "quarantined",
                "quarantine_reason",
                "parsing_error",
                "completed_at",
            )
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in parsing_fields
            ):
                raise ValidationError(
                    "Inbound receipt parsing evidence may change only through "
                    "the locked one-shot finalization workflow."
                )
            matching_fields = (
                "matching_status",
                "match_method",
                "delivery_status_original_message_id",
                "matched_delivery_id",
                "candidate_request_id",
                "matched_request_id",
                "matched_at",
                "matched_by_id",
                "duplicate_of_id",
                "version",
            )
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in matching_fields
            ):
                raise ValidationError(
                    "Inbound receipt associations may change only through the "
                    "locked matching workflow."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Inbound correspondence receipts are immutable and cannot be deleted."
        )


class CorrespondenceDeliveryNotificationQuerySet(models.QuerySet):
    """Append-only manager for per-recipient delivery-status evidence."""

    _IMMUTABLE_MESSAGE = (
        "Delivery-status notifications are append-only and cannot be changed."
    )

    def update(self, **kwargs):
        raise ValidationError(self._IMMUTABLE_MESSAGE)

    def delete(self):
        raise ValidationError(self._IMMUTABLE_MESSAGE)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(self._IMMUTABLE_MESSAGE)

    def update_or_create(
        self,
        defaults=None,
        create_defaults=None,
        **kwargs,
    ):
        raise ValidationError(
            "Delivery-status notifications cannot be written through an upsert."
        )

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise ValidationError(
                "Delivery-status notifications cannot use conflict handling."
            )
        objs = list(objs)
        for notification in objs:
            notification.full_clean()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class CorrespondenceDeliveryNotification(models.Model):
    """Immutable per-recipient evidence parsed from one inbound DSN."""

    class RecipientMatchStatus(models.TextChoices):
        EXACT = "exact", "Exact"
        UNMATCHED = "unmatched", "Unmatched"
        AMBIGUOUS = "ambiguous", "Ambiguous"

    class Action(models.TextChoices):
        FAILED = "failed", "Failed"
        DELAYED = "delayed", "Delayed"
        DELIVERED = "delivered", "Delivered"
        RELAYED = "relayed", "Relayed"
        EXPANDED = "expanded", "Expanded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbound_receipt = models.ForeignKey(
        InboundCorrespondenceReceipt,
        on_delete=models.PROTECT,
        related_name="delivery_notifications",
    )
    delivery = models.ForeignKey(
        CorrespondenceDelivery,
        on_delete=models.PROTECT,
        related_name="delivery_notifications",
    )
    recipient_index = models.PositiveIntegerField()
    recipient = models.ForeignKey(
        CorrespondenceRecipient,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="delivery_notifications",
    )
    recipient_match_status = models.CharField(
        max_length=10,
        choices=RecipientMatchStatus.choices,
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    enhanced_status = models.CharField(max_length=9)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CorrespondenceDeliveryNotificationQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["inbound_receipt", "recipient_index"],
                name="corr_delivery_notice_receipt_index_unique",
            ),
            models.CheckConstraint(
                condition=Q(recipient_index__gte=1),
                name="corr_delivery_notice_recipient_index_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    recipient_match_status__in=[
                        "exact",
                        "unmatched",
                        "ambiguous",
                    ]
                ),
                name="corr_delivery_notice_recipient_match_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        recipient_match_status="exact",
                        recipient__isnull=False,
                    )
                    | Q(
                        recipient_match_status__in=["unmatched", "ambiguous"],
                        recipient__isnull=True,
                    )
                ),
                name="corr_delivery_notice_recipient_link_consistent",
            ),
            models.CheckConstraint(
                condition=Q(
                    action__in=[
                        "failed",
                        "delayed",
                        "delivered",
                        "relayed",
                        "expanded",
                    ]
                ),
                name="corr_delivery_notice_action_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    enhanced_status__regex=(
                        r"\A[245]\.[0-9]{1,3}\.[0-9]{1,3}\Z"
                    )
                ),
                name="corr_delivery_notice_status_syntax",
            ),
            models.CheckConstraint(
                condition=(
                    Q(action="failed", enhanced_status__startswith="5.")
                    | Q(action="delayed", enhanced_status__startswith="4.")
                    | Q(
                        action__in=["delivered", "relayed", "expanded"],
                        enhanced_status__startswith="2.",
                    )
                ),
                name="corr_delivery_notice_action_status_class",
            ),
        ]

    def clean(self):
        super().clean()
        if self.inbound_receipt_id is None or self.delivery_id is None:
            return
        receipt = (
            InboundCorrespondenceReceipt.objects
            .select_related("matched_correspondence")
            .filter(pk=self.inbound_receipt_id)
            .first()
        )
        delivery = (
            CorrespondenceDelivery.objects
            .select_related("correspondence")
            .filter(pk=self.delivery_id)
            .first()
        )
        if receipt is None or delivery is None:
            return
        if (
            receipt.parsing_status
            != InboundCorrespondenceReceipt.ParsingStatus.PARSED
            or receipt.matching_status
            != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            or receipt.match_method
            != InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
            or receipt.matched_delivery_id is None
            or receipt.matched_request_id is None
            or receipt.matched_correspondence_id is None
            or receipt.matched_at is None
        ):
            raise ValidationError(
                {
                    "inbound_receipt": (
                        "Delivery notifications require a parsed, matched "
                        "delivery-status receipt."
                    )
                }
            )

        inbound = receipt.matched_correspondence
        if (
            inbound.direction != Correspondence.Direction.INBOUND
            or inbound.kind
            != Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
            or inbound.status != Correspondence.Status.RECEIVED
            or inbound.request_id != receipt.matched_request_id
        ):
            raise ValidationError(
                {
                    "inbound_receipt": (
                        "The matched receipt must reference delivery-status "
                        "correspondence for its request."
                    )
                }
            )

        outbound = delivery.correspondence
        if (
            receipt.matched_delivery_id != delivery.pk
            or
            outbound.direction != Correspondence.Direction.OUTBOUND
            or outbound.request_id is None
            or outbound.request_id != receipt.matched_request_id
        ):
            raise ValidationError(
                {
                    "delivery": (
                        "The delivery and delivery-status receipt must belong "
                        "to the same request."
                    )
                }
            )

        if self.recipient_id is None:
            return
        recipient_correspondence_id = (
            CorrespondenceRecipient.objects.filter(pk=self.recipient_id)
            .values_list("correspondence_id", flat=True)
            .first()
        )
        if (
            recipient_correspondence_id is not None
            and recipient_correspondence_id != outbound.pk
        ):
            raise ValidationError(
                {
                    "recipient": (
                        "The matched recipient must belong to the delivery's "
                        "correspondence snapshot."
                    )
                }
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Delivery-status notifications are append-only and cannot be changed."
            )
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Delivery-status notifications are append-only and cannot be changed."
        )


class CorrespondenceAttachmentQuerySet(models.QuerySet):
    _CORRESPONDENCE_FIELDS = {"correspondence", "correspondence_id"}
    _APPROVED_IMMUTABLE_MESSAGE = (
        "Approved correspondence attachment identity is immutable."
    )
    _BLOB_IMMUTABLE_MESSAGE = (
        "Finalized correspondence attachment blob identity is immutable."
    )
    _REASSIGNMENT_MESSAGE = (
        "Correspondence attachments cannot be reassigned."
    )
    SCAN_EVIDENCE_FIELDS = {
        "scan_status",
        "quarantined",
        "quarantine_reason",
        "scanned_at",
    }
    BLOB_IDENTITY_FIELDS = {
        "size",
        "sha256",
        "storage_key",
    }
    IMMUTABLE_FIELDS = {
        "id",
        "correspondence",
        "correspondence_id",
        "original_filename",
        "content_type",
        "size",
        "sha256",
        "storage_key",
        "inbound_receipt",
        "inbound_receipt_id",
        "uploaded_by",
        "uploaded_by_id",
        "created_at",
    }

    def _lock_correspondences(self, correspondence_ids):
        correspondence_ids = {
            correspondence_id
            for correspondence_id in correspondence_ids
            if correspondence_id is not None
        }
        locked = list(
            Correspondence.objects.using(self.db)
            .select_for_update()
            .filter(pk__in=correspondence_ids)
            .order_by("pk")
            .values_list("pk", "status")
        )
        if len(locked) != len(correspondence_ids):
            raise ValidationError(
                "Attachment correspondence does not exist."
            )
        return dict(locked)

    def _locked_targets(self):
        targets = list(
            self.order_by("pk").values_list("pk", "correspondence_id")
        )
        if not targets:
            return (
                models.QuerySet(model=self.model, using=self.db).none(),
                [],
                {},
            )
        parent_statuses = self._lock_correspondences(
            correspondence_id for _, correspondence_id in targets
        )
        raw_queryset = models.QuerySet(
            model=self.model,
            using=self.db,
        ).filter(pk__in=[pk for pk, _ in targets])
        locked_targets = list(
            raw_queryset.select_for_update()
            .order_by("pk")
            .values("pk", "correspondence_id", "scan_status")
        )
        return raw_queryset, locked_targets, parent_statuses

    def _validate_locked_mutation(
        self,
        *,
        fields,
        locked_targets,
        parent_statuses,
    ):
        fields = set(fields)
        if self._CORRESPONDENCE_FIELDS.intersection(fields):
            raise ValidationError(self._REASSIGNMENT_MESSAGE)
        if (
            self.BLOB_IDENTITY_FIELDS.intersection(fields)
            and any(
                target["scan_status"]
                != CorrespondenceAttachment.ScanStatus.PENDING
                for target in locked_targets
            )
        ):
            raise ValidationError(self._BLOB_IMMUTABLE_MESSAGE)
        if (
            self.IMMUTABLE_FIELDS.intersection(fields)
            and any(
                parent_statuses[target["correspondence_id"]]
                != Correspondence.Status.DRAFT
                for target in locked_targets
            )
        ):
            raise ValidationError(self._APPROVED_IMMUTABLE_MESSAGE)

    def update(self, **kwargs):
        if self.SCAN_EVIDENCE_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Attachment scan evidence may change only through the locked "
                "scanner finalization workflow."
            )
        if {"inbound_receipt", "inbound_receipt_id"}.intersection(kwargs):
            raise ValidationError(
                "Inbound attachment provenance cannot be reassigned."
            )
        with transaction.atomic(using=self.db):
            raw_queryset, locked_targets, parent_statuses = (
                self._locked_targets()
            )
            self._validate_locked_mutation(
                fields=kwargs,
                locked_targets=locked_targets,
                parent_statuses=parent_statuses,
            )
            return models.QuerySet.update(raw_queryset, **kwargs)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError(
                "Attachment evidence cannot be rewritten through a conflict "
                "upsert."
            )
        attachments = list(objs)
        correspondence_ids = {
            attachment.correspondence_id
            for attachment in attachments
            if attachment.correspondence_id is not None
        }
        with transaction.atomic(using=self.db):
            self._lock_correspondences(correspondence_ids)
            for attachment in attachments:
                attachment._prepare_initial_evidence()
            return super().bulk_create(
                attachments,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

    def bulk_update(self, objs, fields, batch_size=None):
        if self.SCAN_EVIDENCE_FIELDS.intersection(fields):
            raise ValidationError(
                "Attachment scan evidence may change only through the locked "
                "scanner finalization workflow."
            )
        objs = list(objs)
        if self._CORRESPONDENCE_FIELDS.intersection(fields):
            raise ValidationError(self._REASSIGNMENT_MESSAGE)
        with transaction.atomic(using=self.db):
            targets = self.model._base_manager.using(self.db).filter(
                pk__in=[obj.pk for obj in objs]
            )
            raw_queryset, locked_targets, parent_statuses = (
                targets._locked_targets()
            )
            self._validate_locked_mutation(
                fields=fields,
                locked_targets=locked_targets,
                parent_statuses=parent_statuses,
            )
            return models.QuerySet.bulk_update(
                raw_queryset,
                objs,
                fields,
                batch_size=batch_size,
            )

    def create_from_quarantine_reservation(self, *, reservation, **kwargs):
        """Create one inbound attachment with its already-written object key."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Inbound attachment reservation adoption requires a transaction."
            )
        correspondence = kwargs.get("correspondence")
        correspondence_id = kwargs.get(
            "correspondence_id",
            getattr(correspondence, "pk", None),
        )
        self._lock_correspondences({correspondence_id})
        reservation = (
            InboundQuarantineWriteReservation.objects.select_for_update()
            .get(pk=reservation.pk)
        )
        if reservation.adopted_at is not None:
            raise InboundQuarantineReservationAlreadyAdopted(
                "Inbound attachment quarantine reservation was already adopted."
            )
        if (
            reservation.kind
            != InboundQuarantineWriteReservation.Kind.ATTACHMENT
            or reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
            or kwargs.get("size") != reservation.size
            or kwargs.get("sha256") != reservation.sha256
        ):
            raise ValidationError(
                "Inbound attachment evidence does not match its quarantine "
                "reservation."
            )
        attachment = self.model(**kwargs)
        attachment._prepare_initial_evidence()
        attachment.storage_key = reservation.storage_key
        models.Model.save(
            attachment,
            force_insert=True,
            using=self.db,
        )
        InboundQuarantineWriteReservation.objects.adopt_attachment_locked(
            reservation_id=reservation.pk,
            attachment_id=attachment.pk,
            adopted_at=timezone.now(),
        )
        return attachment

    def delete(self):
        with transaction.atomic(using=self.db):
            raw_queryset, locked_targets, parent_statuses = (
                self._locked_targets()
            )
            if any(
                parent_statuses[target["correspondence_id"]]
                != Correspondence.Status.DRAFT
                for target in locked_targets
            ):
                raise ValidationError(
                    "Approved correspondence attachments cannot be deleted."
                )
            return models.QuerySet.delete(raw_queryset)

    def finalize_locked_scan(
        self,
        *,
        expected_storage_key,
        scan_status,
        scanned_at,
        quarantine_reason="",
        promoted_storage_key=None,
    ):
        """Apply one complete malware verdict under a row lock.

        The quarantine object identity is a compare-and-swap input.  Only a
        clean verdict may replace it, and then only with the deterministic
        private key for this attachment.  No other frozen metadata is exposed
        through this mutation seam.
        """
        with transaction.atomic(using=self.db):
            correspondence_id = self.values_list(
                "correspondence_id",
                flat=True,
            ).get()
            self._lock_correspondences({correspondence_id})
            attachment = self.select_for_update().get()
            if attachment.scan_status not in {
                CorrespondenceAttachment.ScanStatus.PENDING,
                CorrespondenceAttachment.ScanStatus.ERROR,
            }:
                raise ValidationError(
                    "This correspondence attachment already has a final scan "
                    "verdict."
                )
            promoted_key = (
                f"correspondence/attachments/"
                f"{attachment.correspondence_id}/{attachment.id}"
            )
            if (
                attachment.scan_status
                == CorrespondenceAttachment.ScanStatus.ERROR
                and attachment.storage_key == promoted_key
            ):
                raise ValidationError(
                    "An explicitly requarantined attachment cannot be "
                    "released by a delayed scanner."
                )
            if attachment.storage_key != expected_storage_key:
                raise ValidationError(
                    "Correspondence attachment storage changed during scanning."
                )
            if scanned_at is None:
                raise ValidationError("A malware scan timestamp is required.")
            if scanned_at < attachment.created_at:
                raise ValidationError(
                    "A malware scan cannot predate attachment creation."
                )

            updates = {
                "scan_status": scan_status,
                "scanned_at": scanned_at,
            }
            if scan_status == CorrespondenceAttachment.ScanStatus.CLEAN:
                if promoted_storage_key != promoted_key:
                    raise ValidationError(
                        "Clean correspondence attachments require the generated "
                        "private storage key."
                    )
                if quarantine_reason:
                    raise ValidationError(
                        "Clean correspondence attachments cannot retain a "
                        "quarantine reason."
                    )
                updates.update(
                    storage_key=promoted_storage_key,
                    quarantined=False,
                    quarantine_reason="",
                )
            elif scan_status in {
                CorrespondenceAttachment.ScanStatus.INFECTED,
                CorrespondenceAttachment.ScanStatus.ERROR,
            }:
                if promoted_storage_key is not None:
                    raise ValidationError(
                        "Only a clean scan may promote attachment storage."
                    )
                if not (quarantine_reason or "").strip():
                    raise ValidationError(
                        "A failed malware verdict requires a safe quarantine "
                        "reason."
                    )
                updates.update(
                    quarantined=True,
                    quarantine_reason=quarantine_reason,
                )
            else:
                raise ValidationError(
                    "The attachment scanner cannot assign this scan status."
                )

            super().update(**updates)
            return type(attachment).objects.get(pk=attachment.pk)

    def requarantine_locked(
        self,
        *,
        quarantine_reason,
        quarantined_at,
    ):
        """Fail closed when a later security decision revokes clean release.

        This narrow compare-and-swap seam exists so an approved attachment can
        be made unavailable without reopening any content or identity fields.
        A service using it remains responsible for recording the accountable
        audit event in the same outer transaction.
        """
        with transaction.atomic(using=self.db):
            correspondence_id = self.values_list(
                "correspondence_id",
                flat=True,
            ).get()
            self._lock_correspondences({correspondence_id})
            attachment = self.select_for_update().get()
            if (
                attachment.scan_status
                != CorrespondenceAttachment.ScanStatus.CLEAN
                or attachment.quarantined
            ):
                raise ValidationError(
                    "Only a released clean attachment may be requarantined."
                )
            if not (quarantine_reason or "").strip():
                raise ValidationError(
                    "Requarantining an attachment requires a safe reason."
                )
            if quarantined_at is None:
                raise ValidationError(
                    "Requarantining an attachment requires a timestamp."
                )
            if (
                quarantined_at < attachment.created_at
                or (
                    attachment.scanned_at is not None
                    and quarantined_at < attachment.scanned_at
                )
            ):
                raise ValidationError(
                    "Attachment requarantine cannot predate its scan evidence."
                )
            super().update(
                scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
                quarantined=True,
                quarantine_reason=quarantine_reason,
                scanned_at=quarantined_at,
            )
            return type(attachment).objects.get(pk=attachment.pk)

    def finalize_infected_verdict_locked(
        self,
        *,
        expected_size,
        expected_sha256,
        scanned_at,
        quarantine_reason,
    ):
        """Make a positive malware verdict dominate a competing clean scan."""
        with transaction.atomic(using=self.db):
            correspondence_id = self.values_list(
                "correspondence_id",
                flat=True,
            ).get()
            self._lock_correspondences({correspondence_id})
            attachment = self.select_for_update().get()
            if (
                attachment.size != expected_size
                or attachment.sha256 != expected_sha256
            ):
                raise ValidationError(
                    "The malware verdict does not match this attachment blob."
                )
            if not (quarantine_reason or "").strip():
                raise ValidationError(
                    "A positive malware verdict requires a safe quarantine "
                    "reason."
                )
            if scanned_at is None or scanned_at < attachment.created_at:
                raise ValidationError(
                    "A malware verdict requires a valid scan timestamp."
                )
            if (
                attachment.scan_status
                == CorrespondenceAttachment.ScanStatus.INFECTED
            ):
                return attachment
            effective_scanned_at = max(
                scanned_at,
                attachment.scanned_at or attachment.created_at,
            )
            super().update(
                scan_status=CorrespondenceAttachment.ScanStatus.INFECTED,
                scanned_at=effective_scanned_at,
                quarantined=True,
                quarantine_reason=quarantine_reason,
            )
            return type(attachment).objects.get(pk=attachment.pk)


class CorrespondenceAttachment(models.Model):
    """Quarantined inbound or outbound file associated with correspondence."""

    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"
        ERROR = "error", "Scan error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correspondence = models.ForeignKey(
        Correspondence,
        on_delete=models.PROTECT,
        related_name="attachments",
    )
    inbound_receipt = models.ForeignKey(
        InboundCorrespondenceReceipt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attachments",
    )
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    storage_key = models.CharField(
        max_length=1000,
        default=_attachment_storage_key,
        editable=False,
        unique=True,
    )
    scan_status = models.CharField(
        max_length=20,
        choices=ScanStatus.choices,
        default=ScanStatus.PENDING,
    )
    quarantined = models.BooleanField(default=True)
    quarantine_reason = models.CharField(max_length=500, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="correspondence_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    objects = CorrespondenceAttachmentQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        scan_status="pending",
                        quarantined=True,
                        quarantine_reason="",
                        scanned_at__isnull=True,
                    )
                    | Q(
                        scan_status="clean",
                        quarantined=False,
                        quarantine_reason="",
                        scanned_at__isnull=False,
                    )
                    | (
                        Q(
                            scan_status__in=["infected", "error"],
                            quarantined=True,
                            scanned_at__isnull=False,
                        )
                        & ~Q(quarantine_reason="")
                    )
                ),
                name="correspondence_attachment_scan_state_exact",
            ),
            models.CheckConstraint(
                condition=(
                    Q(scanned_at__isnull=True)
                    | Q(scanned_at__gte=models.F("created_at"))
                ),
                name="correspondence_attachment_scan_chronology",
            ),
        ]

    @property
    def is_downloadable(self) -> bool:
        return self.scan_status == self.ScanStatus.CLEAN and not self.quarantined

    def _assert_initial_scan_evidence(self):
        if (
            self.scan_status != self.ScanStatus.PENDING
            or not self.quarantined
            or self.quarantine_reason != ""
            or self.scanned_at is not None
        ):
            raise ValidationError(
                "New correspondence attachments must begin pending in "
                "quarantine without a scanner verdict."
            )

    def _prepare_initial_evidence(self):
        self.original_filename = sanitize_filename(self.original_filename)
        direction, status = self._validate_provenance()
        self.validate_outbound_metadata(direction=direction)
        if (
            direction == Correspondence.Direction.OUTBOUND
            and status != Correspondence.Status.DRAFT
        ):
            raise ValidationError(
                "Attachments must be added before correspondence approval."
            )
        self._assert_initial_scan_evidence()
        self.storage_key = _attachment_storage_key()

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(
            type(self),
            instance=self,
        )
        attachments = type(self).objects.using(using)
        with transaction.atomic(using=using):
            if self._state.adding:
                attachments._lock_correspondences(
                    {self.correspondence_id}
                )
                self._prepare_initial_evidence()
            else:
                current_correspondence_id = (
                    attachments.filter(pk=self.pk)
                    .values_list("correspondence_id", flat=True)
                    .get()
                )
                parent_status = attachments._lock_correspondences(
                    {current_correspondence_id}
                )[current_correspondence_id]
                previous = attachments.select_for_update().get(pk=self.pk)
                if self.correspondence_id != previous.correspondence_id:
                    raise ValidationError(
                        CorrespondenceAttachmentQuerySet._REASSIGNMENT_MESSAGE
                    )
                self.original_filename = sanitize_filename(
                    self.original_filename
                )
                direction, _ = self._validate_provenance()
                self.validate_outbound_metadata(direction=direction)
                scan_evidence_fields = (
                    "scan_status",
                    "quarantined",
                    "quarantine_reason",
                    "scanned_at",
                )
                if any(
                    getattr(previous, field) != getattr(self, field)
                    for field in scan_evidence_fields
                ):
                    raise ValidationError(
                        "Attachment scan evidence may change only through the "
                        "locked scanner finalization workflow."
                    )
                if (
                    previous.scan_status != self.ScanStatus.PENDING
                    and any(
                        getattr(previous, field) != getattr(self, field)
                        for field in (
                            "size",
                            "sha256",
                            "storage_key",
                        )
                    )
                ):
                    raise ValidationError(
                        CorrespondenceAttachmentQuerySet
                        ._BLOB_IMMUTABLE_MESSAGE
                    )
                if parent_status != Correspondence.Status.DRAFT:
                    immutable_fields = (
                        "correspondence_id",
                        "original_filename",
                        "content_type",
                        "size",
                        "sha256",
                        "storage_key",
                        "inbound_receipt_id",
                        "uploaded_by_id",
                    )
                    changed = {
                        field
                        for field in immutable_fields
                        if getattr(previous, field) != getattr(self, field)
                    }
                    if changed:
                        raise ValidationError(
                            CorrespondenceAttachmentQuerySet
                            ._APPROVED_IMMUTABLE_MESSAGE
                        )
            return super().save(*args, **kwargs)

    def _validate_provenance(self):
        correspondence = Correspondence.objects.only(
            "direction",
            "status",
        ).get(pk=self.correspondence_id)
        if correspondence.direction == Correspondence.Direction.OUTBOUND:
            if self.inbound_receipt_id is not None:
                raise ValidationError(
                    "Outbound attachments cannot claim inbound receipt provenance."
                )
        elif correspondence.direction == Correspondence.Direction.INBOUND:
            if self.inbound_receipt_id is None:
                raise ValidationError(
                    "Inbound attachments require receipt provenance."
                )
            try:
                receipt_correspondence_id = (
                    InboundCorrespondenceReceipt.objects.only(
                        "matched_correspondence_id"
                    ).get(pk=self.inbound_receipt_id).matched_correspondence_id
                )
            except InboundCorrespondenceReceipt.DoesNotExist as exc:
                raise ValidationError(
                    "Inbound attachment receipt provenance does not exist."
                ) from exc
            if receipt_correspondence_id != self.correspondence_id:
                raise ValidationError(
                    "Inbound attachment receipt must identify the same "
                    "correspondence."
                )
        else:
            raise ValidationError("Attachment correspondence direction is invalid.")
        return correspondence.direction, correspondence.status

    def validate_outbound_metadata(self, *, direction=None):
        if direction is None:
            direction = Correspondence.objects.only("direction").get(
                pk=self.correspondence_id
            ).direction
        if direction != Correspondence.Direction.OUTBOUND:
            return
        if not _ATTACHMENT_CONTENT_TYPE.fullmatch(str(self.content_type or "")):
            raise ValidationError(
                "Outbound attachment content types must be safe MIME identifiers."
            )
        if not _SHA256_HEX.fullmatch(str(self.sha256 or "")):
            raise ValidationError(
                "Outbound attachment SHA-256 evidence must be lowercase hexadecimal."
            )

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(
            type(self),
            instance=self,
        )
        return type(self).objects.using(using).filter(pk=self.pk).delete()


class InboundQuarantineTransportAliasQuerySet(models.QuerySet):
    """Immutable transport identities claimed for canonical inbound evidence."""

    def update(self, **kwargs):
        raise ValidationError(
            "Inbound quarantine transport aliases are immutable."
        )

    def bulk_create(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine transport aliases must be claimed individually."
        )

    def bulk_update(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine transport aliases are immutable."
        )

    def delete(self):
        raise ValidationError(
            "Inbound quarantine transport aliases cannot be deleted."
        )

    def claim(self, *, kind, identity, logical_identity):
        if kind not in InboundQuarantineTransportAlias.Kind.values:
            raise ValidationError("Inbound transport alias kind is invalid.")
        if not _SHA256_HEX.fullmatch(str(identity or "")):
            raise ValidationError("Inbound transport alias identity is invalid.")
        if not _SHA256_HEX.fullmatch(str(logical_identity or "")):
            raise ValidationError(
                "Inbound transport alias payload identity is invalid."
            )
        try:
            with transaction.atomic(using=self.db):
                alias, _created = self.get_or_create(
                    kind=kind,
                    identity=identity,
                    defaults={"logical_identity": logical_identity},
                )
        except IntegrityError:
            alias = self.get(kind=kind, identity=identity)
        if alias.logical_identity != logical_identity:
            raise InboundQuarantineReservationConflict(kind)
        return alias


class InboundQuarantineTransportAlias(models.Model):
    """One durable idempotency/provider alias for canonical inbound evidence."""

    class Kind(models.TextChoices):
        IDEMPOTENCY = "idempotency", "Idempotency"
        PROVIDER = "provider", "Provider"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices, editable=False)
    identity = models.CharField(max_length=64, editable=False)
    logical_identity = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InboundQuarantineTransportAliasQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "identity"],
                name="corr_in_alias_kind_identity_unique",
            ),
            models.CheckConstraint(
                condition=Q(identity__regex=r"\A[0-9a-f]{64}\Z")
                & Q(logical_identity__regex=r"\A[0-9a-f]{64}\Z"),
                name="corr_in_alias_hashes_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Inbound quarantine transport aliases are immutable."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine transport aliases cannot be deleted."
        )


class InboundQuarantineWriteReservationQuerySet(models.QuerySet):
    """Narrow durable mutations for pre-domain quarantine writes."""

    @staticmethod
    def _update(queryset, **updates):
        return models.QuerySet.update(queryset, **updates)

    def update(self, **kwargs):
        raise ValidationError(
            "Inbound quarantine reservation evidence changes only through "
            "the reservation workflow."
        )

    def bulk_create(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine reservations must be created individually."
        )

    def bulk_update(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine reservation evidence changes only through "
            "the reservation workflow."
        )

    def delete(self):
        raise ValidationError(
            "Inbound quarantine reservation evidence cannot be deleted."
        )

    @transaction.atomic
    def reserve(
        self,
        *,
        logical_identity,
        kind,
        size,
        sha256,
        transport_idempotency_identity=None,
        transport_provider_identity=None,
    ):
        if not _SHA256_HEX.fullmatch(str(logical_identity or "")):
            raise ValidationError("Quarantine reservation identity is invalid.")
        if not _SHA256_HEX.fullmatch(str(sha256 or "")):
            raise ValidationError("Quarantine reservation digest is invalid.")
        if kind not in InboundQuarantineWriteReservation.Kind.values:
            raise ValidationError("Quarantine reservation kind is invalid.")
        if size < 0 or (kind == "raw_message" and size < 1):
            raise ValidationError("Quarantine reservation size is invalid.")
        transport_identities = (
            transport_idempotency_identity,
            transport_provider_identity,
        )
        if kind == "raw_message":
            if not all(
                _SHA256_HEX.fullmatch(str(value or ""))
                for value in transport_identities
            ):
                raise ValidationError(
                    "Raw quarantine transport reservation identity is invalid."
                )
        elif any(transport_identities):
            raise ValidationError(
                "Attachment reservations cannot claim transport identities."
            )
        if kind == "raw_message":
            InboundQuarantineTransportAlias.objects.claim(
                kind=InboundQuarantineTransportAlias.Kind.IDEMPOTENCY,
                identity=transport_idempotency_identity,
                logical_identity=logical_identity,
            )
            InboundQuarantineTransportAlias.objects.claim(
                kind=InboundQuarantineTransportAlias.Kind.PROVIDER,
                identity=transport_provider_identity,
                logical_identity=logical_identity,
            )
            historical_idempotency = self.filter(
                transport_idempotency_identity=(
                    transport_idempotency_identity
                )
            ).exclude(logical_identity=logical_identity)
            if historical_idempotency.exists():
                raise InboundQuarantineReservationConflict("idempotency")
            historical_provider = self.filter(
                transport_provider_identity=transport_provider_identity
            ).exclude(logical_identity=logical_identity)
            if historical_provider.exists():
                raise InboundQuarantineReservationConflict("provider")
        active = self.filter(
            logical_identity=logical_identity,
            completed_at__isnull=True,
        ).first()
        if active is not None:
            reservation = active
            created = False
        else:
            latest_generation = (
                self.filter(logical_identity=logical_identity)
                .order_by("-generation")
                .values_list("generation", flat=True)
                .first()
                or 0
            )
            try:
                with transaction.atomic(using=self.db):
                    reservation, created = self.get_or_create(
                        logical_identity=logical_identity,
                        generation=latest_generation + 1,
                        defaults={
                            "kind": kind,
                            "size": size,
                            "sha256": sha256,
                            "storage_key": _reservation_storage_key(kind),
                            "transport_idempotency_identity": (
                                transport_idempotency_identity
                            ),
                            "transport_provider_identity": (
                                transport_provider_identity
                            ),
                        },
                    )
            except IntegrityError:
                if kind == InboundQuarantineWriteReservation.Kind.ATTACHMENT:
                    reservation = self.get(
                        logical_identity=logical_identity,
                        completed_at__isnull=True,
                    )
                else:
                    reservation = self.filter(
                        completed_at__isnull=True
                    ).filter(
                        Q(
                            transport_idempotency_identity=(
                                transport_idempotency_identity
                            )
                        )
                        | Q(
                            transport_provider_identity=(
                                transport_provider_identity
                            )
                        )
                    ).first()
                    if reservation is None:
                        reservation = self.get(
                            logical_identity=logical_identity,
                            completed_at__isnull=True,
                        )
                if reservation.logical_identity != logical_identity:
                    if (
                        reservation.transport_idempotency_identity
                        == transport_idempotency_identity
                    ):
                        raise InboundQuarantineReservationConflict(
                            "idempotency"
                        ) from None
                    raise InboundQuarantineReservationConflict(
                        "provider"
                    ) from None
                created = False
        if not created and (
            reservation.kind != kind
            or reservation.size != size
            or reservation.sha256 != sha256
        ):
            raise ValidationError(
                "Quarantine reservation identity conflicts with stored evidence."
            )
        if reservation.completed_at is not None:
            return self.reserve(
                logical_identity=logical_identity,
                kind=kind,
                size=size,
                sha256=sha256,
                transport_idempotency_identity=(
                    transport_idempotency_identity
                ),
                transport_provider_identity=transport_provider_identity,
            )
        return reservation

    def adopt_receipt_locked(
        self,
        *,
        reservation_id,
        receipt_id,
        adopted_at,
    ):
        reservation = self.select_for_update().get(pk=reservation_id)
        receipt = InboundCorrespondenceReceipt.objects.only(
            "raw_storage_key",
            "raw_message_size",
            "raw_message_sha256",
        ).get(pk=receipt_id)
        if (
            reservation.kind != "raw_message"
            or reservation.stored_at is None
            or reservation.adopted_at is not None
            or reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
            or receipt.raw_storage_key != reservation.storage_key
            or receipt.raw_message_size != reservation.size
            or receipt.raw_message_sha256 != reservation.sha256
        ):
            raise ValidationError("Raw quarantine reservation cannot be adopted.")
        self._update(
            self.filter(pk=reservation.pk),
            adopted_receipt_id=receipt.pk,
            adopted_at=max(adopted_at, reservation.created_at),
            processing_expires_at=None,
        )

    def adopt_attachment_locked(
        self,
        *,
        reservation_id,
        attachment_id,
        adopted_at,
    ):
        reservation = self.select_for_update().get(pk=reservation_id)
        attachment = CorrespondenceAttachment.objects.only(
            "storage_key",
            "size",
            "sha256",
        ).get(pk=attachment_id)
        if (
            reservation.kind != "attachment"
            or reservation.stored_at is None
            or reservation.adopted_at is not None
            or reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
            or attachment.storage_key != reservation.storage_key
            or attachment.size != reservation.size
            or attachment.sha256 != reservation.sha256
        ):
            raise ValidationError(
                "Attachment quarantine reservation cannot be adopted."
            )
        self._update(
            self.filter(pk=reservation.pk),
            adopted_attachment_id=attachment.pk,
            adopted_at=max(adopted_at, reservation.created_at),
            processing_expires_at=None,
        )

    def mark_stored_locked(self, *, reservation_id, stored_at):
        reservation = self.select_for_update().get(pk=reservation_id)
        if (
            reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
        ):
            raise ValidationError(
                "This quarantine reservation cannot record a completed write."
            )
        if reservation.stored_at is not None:
            return reservation
        self._update(
            self.filter(pk=reservation.pk),
            stored_at=max(stored_at, reservation.created_at),
        )
        return self.model.objects.get(pk=reservation.pk)

    def refresh_processing_locked(
        self,
        *,
        reservation_id,
        processing_expires_at,
    ):
        reservation = self.select_for_update().get(pk=reservation_id)
        if reservation.adopted_at is not None:
            return reservation
        if (
            reservation.completed_at is not None
            or reservation.lease_token is not None
            or reservation.attempt_count > 0
        ):
            raise InboundQuarantineCleanupInProgress(
                "This quarantine reservation cannot renew processing."
            )
        effective_expiry = max(
            processing_expires_at,
            reservation.processing_expires_at or reservation.created_at,
        )
        self._update(
            self.filter(pk=reservation.pk),
            processing_expires_at=effective_expiry,
        )
        return self.model.objects.get(pk=reservation.pk)

    def heal_domain_reference_locked(
        self,
        *,
        reservation_id,
        lease_token,
        adopted_at,
    ):
        """Adopt a proven exact domain reference instead of deleting its key."""
        with transaction.atomic(using=self.db):
            reservation = self.select_for_update().get(pk=reservation_id)
            if reservation.adopted_at is not None:
                return True
            if (
                reservation.completed_at is not None
                or reservation.lease_token != lease_token
                or reservation.attempt_count > 0
            ):
                return False

            receipt = InboundCorrespondenceReceipt.objects.filter(
                raw_storage_key=reservation.storage_key,
                raw_message_size=reservation.size,
                raw_message_sha256=reservation.sha256,
            ).first()
            attachment = CorrespondenceAttachment.objects.filter(
                storage_key=reservation.storage_key,
                size=reservation.size,
                sha256=reservation.sha256,
            ).first()
            if reservation.kind == "raw_message" and receipt and not attachment:
                updates = {
                    "adopted_receipt_id": receipt.pk,
                    "adopted_attachment_id": None,
                }
            elif (
                reservation.kind == "attachment"
                and attachment
                and not receipt
            ):
                updates = {
                    "adopted_receipt_id": None,
                    "adopted_attachment_id": attachment.pk,
                }
            else:
                return False
            self._update(
                self.filter(pk=reservation.pk),
                **updates,
                adopted_at=max(adopted_at, reservation.created_at),
                processing_expires_at=None,
                lease_token=None,
                lease_expires_at=None,
            )
            return True

    def begin_cleanup_attempt_locked(
        self,
        *,
        reservation_id,
        lease_token,
        attempted_at,
    ):
        """Commit an irreversible cleanup barrier before object deletion."""
        if not transaction.get_connection(self.db).in_atomic_block:
            raise ValidationError(
                "Quarantine cleanup must begin inside a transaction."
            )
        reservation = self.select_for_update().get(pk=reservation_id)
        if reservation.adopted_at is not None:
            return "retained"
        if reservation.completed_at is not None:
            return "deleted"
        if reservation.lease_token != lease_token:
            return "deferred"

        receipt = InboundCorrespondenceReceipt.objects.filter(
            raw_storage_key=reservation.storage_key
        ).first()
        attachment = CorrespondenceAttachment.objects.filter(
            storage_key=reservation.storage_key
        ).first()
        if receipt or attachment:
            if reservation.attempt_count > 0:
                return "conflict"
            if (
                reservation.kind == "raw_message"
                and receipt
                and not attachment
                and receipt.raw_message_size == reservation.size
                and receipt.raw_message_sha256 == reservation.sha256
            ):
                updates = {
                    "adopted_receipt_id": receipt.pk,
                    "adopted_attachment_id": None,
                }
            elif (
                reservation.kind == "attachment"
                and attachment
                and not receipt
                and attachment.size == reservation.size
                and attachment.sha256 == reservation.sha256
            ):
                updates = {
                    "adopted_receipt_id": None,
                    "adopted_attachment_id": attachment.pk,
                }
            else:
                return "conflict"
            self._update(
                self.filter(pk=reservation.pk),
                **updates,
                adopted_at=max(attempted_at, reservation.created_at),
                processing_expires_at=None,
                lease_token=None,
                lease_expires_at=None,
            )
            return "healed"

        effective_attempted_at = max(
            attempted_at,
            reservation.last_attempt_at or reservation.created_at,
        )
        self._update(
            self.filter(pk=reservation.pk),
            attempt_count=models.F("attempt_count") + 1,
            last_attempt_at=effective_attempted_at,
            processing_expires_at=None,
        )
        return "delete"

    def claim_due(
        self,
        *,
        reservation_id,
        now,
        stale_before,
        lease_expires_at,
    ):
        with transaction.atomic(using=self.db):
            reservation = self.select_for_update().get(pk=reservation_id)
            if (
                reservation.adopted_at is not None
                or reservation.completed_at is not None
                or reservation.next_attempt_at > now
                or reservation.created_at > stale_before
                or (
                    reservation.processing_expires_at is not None
                    and reservation.processing_expires_at > now
                )
                or (
                    reservation.lease_expires_at is not None
                    and reservation.lease_expires_at > now
                )
            ):
                return None
            lease_token = uuid.uuid4()
            self._update(
                self.filter(pk=reservation.pk),
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                processing_expires_at=None,
            )
            return self.model.objects.get(pk=reservation.pk)

    def record_failure_locked(
        self,
        *,
        reservation_id,
        lease_token,
        attempted_at,
        next_attempt_at,
    ):
        with transaction.atomic(using=self.db):
            reservation = self.select_for_update().get(pk=reservation_id)
            if (
                reservation.adopted_at is not None
                or reservation.completed_at is not None
                or reservation.lease_token != lease_token
                or reservation.attempt_count < 1
                or reservation.last_attempt_at is None
            ):
                return False
            effective_attempted_at = max(
                attempted_at,
                reservation.last_attempt_at or reservation.created_at,
            )
            if next_attempt_at <= effective_attempted_at:
                raise ValidationError(
                    "A reservation retry must follow its failed attempt."
                )
            self._update(
                self.filter(pk=reservation.pk),
                last_attempt_at=effective_attempted_at,
                next_attempt_at=next_attempt_at,
                lease_token=None,
                lease_expires_at=None,
            )
            return True

    def release_claim(
        self,
        *,
        reservation_id,
        lease_token,
        next_attempt_at,
    ):
        with transaction.atomic(using=self.db):
            reservation = self.select_for_update().get(pk=reservation_id)
            if (
                reservation.adopted_at is not None
                or reservation.completed_at is not None
                or reservation.lease_token != lease_token
            ):
                return False
            self._update(
                self.filter(pk=reservation.pk),
                next_attempt_at=next_attempt_at,
                lease_token=None,
                lease_expires_at=None,
            )
            return True

    def complete_locked(
        self,
        *,
        reservation_id,
        lease_token,
        completed_at,
    ):
        with transaction.atomic(using=self.db):
            reservation = self.select_for_update().get(pk=reservation_id)
            if (
                reservation.adopted_at is not None
                or reservation.completed_at is not None
                or reservation.lease_token != lease_token
                or reservation.attempt_count < 1
                or reservation.last_attempt_at is None
            ):
                return False
            effective_completed_at = max(
                completed_at,
                reservation.last_attempt_at or reservation.created_at,
            )
            self._update(
                self.filter(pk=reservation.pk),
                last_attempt_at=effective_completed_at,
                completed_at=effective_completed_at,
                processing_expires_at=None,
                lease_token=None,
                lease_expires_at=None,
            )
            return True


class InboundQuarantineWriteReservation(models.Model):
    """Committed identity for an inbound object written before domain commit."""

    class Kind(models.TextChoices):
        RAW_MESSAGE = "raw_message", "Raw message"
        ATTACHMENT = "attachment", "Attachment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    logical_identity = models.CharField(max_length=64, editable=False)
    generation = models.PositiveIntegerField(default=1, editable=False)
    transport_idempotency_identity = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        editable=False,
    )
    transport_provider_identity = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        editable=False,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, editable=False)
    size = models.PositiveBigIntegerField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    storage_key = models.CharField(max_length=1000, unique=True, editable=False)
    adopted_receipt = models.OneToOneField(
        InboundCorrespondenceReceipt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="raw_quarantine_reservation",
    )
    adopted_attachment = models.OneToOneField(
        CorrespondenceAttachment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quarantine_write_reservation",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    stored_at = models.DateTimeField(null=True, blank=True)
    adopted_at = models.DateTimeField(null=True, blank=True)
    processing_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
    )
    next_attempt_at = models.DateTimeField(default=timezone.now, editable=False)
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = InboundQuarantineWriteReservationQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["next_attempt_at", "created_at", "id"],
                condition=Q(adopted_at__isnull=True, completed_at__isnull=True),
                name="corr_in_resv_pending_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["logical_identity", "generation"],
                name="corr_in_resv_identity_generation_unique",
            ),
            models.UniqueConstraint(
                fields=["logical_identity"],
                condition=Q(completed_at__isnull=True),
                name="corr_in_resv_one_active_identity",
            ),
            models.UniqueConstraint(
                fields=["transport_idempotency_identity"],
                condition=Q(
                    completed_at__isnull=True,
                    transport_idempotency_identity__isnull=False,
                ),
                name="corr_in_resv_one_active_idempotency",
            ),
            models.UniqueConstraint(
                fields=["transport_provider_identity"],
                condition=Q(
                    completed_at__isnull=True,
                    transport_provider_identity__isnull=False,
                ),
                name="corr_in_resv_one_active_provider",
            ),
            models.CheckConstraint(
                condition=(
                    Q(logical_identity__regex=r"\A[0-9a-f]{64}\Z")
                    & Q(sha256__regex=r"\A[0-9a-f]{64}\Z")
                    & Q(generation__gte=1)
                    & (
                        Q(kind="raw_message", size__gte=1)
                        | Q(kind="attachment", size__gte=0)
                    )
                ),
                name="corr_in_resv_evidence_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        kind="raw_message",
                        storage_key__startswith="quarantine/correspondence/raw/",
                    )
                    | Q(
                        kind="attachment",
                        storage_key__startswith=(
                            "quarantine/correspondence/attachments/"
                        ),
                    )
                ),
                name="corr_in_resv_key_namespace",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(kind="raw_message")
                        & Q(transport_idempotency_identity__isnull=False)
                        & Q(transport_provider_identity__isnull=False)
                    )
                    | (
                        Q(kind="attachment")
                        & Q(transport_idempotency_identity__isnull=True)
                        & Q(transport_provider_identity__isnull=True)
                    )
                ),
                name="corr_in_resv_transport_identity_exact",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        adopted_at__isnull=True,
                        adopted_receipt__isnull=True,
                        adopted_attachment__isnull=True,
                    )
                    | Q(
                        kind="raw_message",
                        adopted_at__isnull=False,
                        adopted_receipt__isnull=False,
                        adopted_attachment__isnull=True,
                    )
                    | Q(
                        kind="attachment",
                        adopted_at__isnull=False,
                        adopted_receipt__isnull=True,
                        adopted_attachment__isnull=False,
                    )
                ),
                name="corr_in_resv_adoption_exact",
            ),
            models.CheckConstraint(
                condition=(
                    Q(stored_at__isnull=True)
                    | Q(stored_at__gte=models.F("created_at"))
                ),
                name="corr_in_resv_storage_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(processing_expires_at__isnull=True)
                    | Q(
                        processing_expires_at__gte=models.F("created_at")
                    )
                ),
                name="corr_in_resv_processing_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        adopted_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | Q(processing_expires_at__isnull=True)
                ),
                name="corr_in_resv_terminal_unprocessed",
            ),
            models.CheckConstraint(
                condition=(
                    Q(adopted_at__isnull=True)
                    | (
                        Q(adopted_at__gte=models.F("created_at"))
                        & (
                            Q(stored_at__isnull=True)
                            | Q(adopted_at__gte=models.F("stored_at"))
                        )
                    )
                ),
                name="corr_in_resv_adoption_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(completed_at__isnull=True)
                    | Q(
                        adopted_at__isnull=True,
                        adopted_receipt__isnull=True,
                        adopted_attachment__isnull=True,
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                ),
                name="corr_in_resv_completed_unadopted",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                    | Q(
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                    )
                ),
                name="corr_in_resv_lease_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        attempt_count=0,
                        last_attempt_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | Q(attempt_count__gt=0, last_attempt_at__isnull=False)
                ),
                name="corr_in_resv_attempt_evidence",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Inbound quarantine reservation evidence changes only through "
                "the reservation workflow."
            )
        if (
            self.adopted_receipt_id is not None
            or self.adopted_attachment_id is not None
            or self.stored_at is not None
            or self.adopted_at is not None
            or self.processing_expires_at is not None
            or self.attempt_count != 0
            or self.last_attempt_at is not None
            or self.lease_token is not None
            or self.lease_expires_at is not None
            or self.completed_at is not None
        ):
            raise ValidationError(
                "New inbound quarantine reservations must begin unadopted "
                "and unclaimed."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Inbound quarantine reservation evidence cannot be deleted."
        )


class CorrespondenceAttachmentQuarantineCleanupQuerySet(models.QuerySet):
    """Narrow compare-and-swap mutations for destructive cleanup evidence."""

    @staticmethod
    def _update(queryset, **updates):
        return models.QuerySet.update(queryset, **updates)

    def update(self, **kwargs):
        raise ValidationError(
            "Quarantine cleanup evidence changes only through the cleanup "
            "worker."
        )

    def bulk_create(self, *args, **kwargs):
        raise ValidationError(
            "Quarantine cleanup work must be created with one validated row."
        )

    def bulk_update(self, *args, **kwargs):
        raise ValidationError(
            "Quarantine cleanup evidence changes only through the cleanup "
            "worker."
        )

    def delete(self):
        raise ValidationError("Quarantine cleanup evidence cannot be deleted.")

    def claim_due(
        self,
        *,
        cleanup_id,
        now,
        lease_expires_at,
    ):
        with transaction.atomic(using=self.db):
            cleanup = self.select_for_update().get(pk=cleanup_id)
            if cleanup.completed_at is not None:
                return None
            if cleanup.next_attempt_at > now:
                return None
            if (
                cleanup.lease_expires_at is not None
                and cleanup.lease_expires_at > now
            ):
                return None
            lease_token = uuid.uuid4()
            self._update(
                self.filter(pk=cleanup.pk),
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )
            return type(cleanup).objects.get(pk=cleanup.pk)

    def release_claim(self, *, cleanup_id, lease_token, next_attempt_at):
        with transaction.atomic(using=self.db):
            cleanup = self.select_for_update().get(pk=cleanup_id)
            if (
                cleanup.completed_at is not None
                or cleanup.lease_token != lease_token
            ):
                return False
            self._update(
                self.filter(pk=cleanup.pk),
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=next_attempt_at,
            )
            return True

    def record_failure_locked(
        self,
        *,
        cleanup_id,
        lease_token,
        attempted_at,
        next_attempt_at,
    ):
        with transaction.atomic(using=self.db):
            cleanup = self.select_for_update().get(pk=cleanup_id)
            if (
                cleanup.completed_at is not None
                or cleanup.lease_token != lease_token
            ):
                return False
            effective_attempted_at = max(
                attempted_at,
                cleanup.last_attempt_at or cleanup.created_at,
            )
            if next_attempt_at <= effective_attempted_at:
                raise ValidationError(
                    "A cleanup retry must follow its failed attempt."
                )
            self._update(
                self.filter(pk=cleanup.pk),
                attempt_count=models.F("attempt_count") + 1,
                last_attempt_at=effective_attempted_at,
                next_attempt_at=next_attempt_at,
                lease_token=None,
                lease_expires_at=None,
            )
            return True

    def complete_locked(
        self,
        *,
        cleanup_id,
        lease_token,
        completed_at,
    ):
        with transaction.atomic(using=self.db):
            cleanup = self.select_for_update().get(pk=cleanup_id)
            if cleanup.completed_at is not None:
                return False
            if cleanup.lease_token != lease_token:
                return False
            effective_completed_at = max(
                completed_at,
                cleanup.last_attempt_at or cleanup.created_at,
            )
            self._update(
                self.filter(pk=cleanup.pk),
                attempt_count=models.F("attempt_count") + 1,
                last_attempt_at=effective_completed_at,
                lease_token=None,
                lease_expires_at=None,
                completed_at=effective_completed_at,
            )
            return True


class CorrespondenceAttachmentQuarantineCleanup(models.Model):
    """Durable, retryable deletion work for a finalized quarantine object."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attachment = models.OneToOneField(
        CorrespondenceAttachment,
        on_delete=models.PROTECT,
        related_name="quarantine_cleanup",
    )
    source_storage_key = models.CharField(
        max_length=1000,
        editable=False,
        unique=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = CorrespondenceAttachmentQuarantineCleanupQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["next_attempt_at", "created_at", "id"],
                condition=Q(completed_at__isnull=True),
                name="corr_cleanup_pending_due_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    source_storage_key__startswith=(
                        "quarantine/correspondence/attachments/"
                    )
                ),
                name="correspondence_cleanup_key_namespace",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        attempt_count=0,
                        last_attempt_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | Q(
                        attempt_count__gt=0,
                        last_attempt_at__isnull=False,
                    )
                ),
                name="correspondence_cleanup_attempt_evidence",
            ),
            models.CheckConstraint(
                condition=(
                    Q(completed_at__isnull=True)
                    | Q(completed_at__gte=models.F("last_attempt_at"))
                ),
                name="correspondence_cleanup_completion_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                    | Q(
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                    )
                ),
                name="correspondence_cleanup_lease_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(completed_at__isnull=True)
                    | Q(
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                ),
                name="correspondence_cleanup_completed_unleased",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Quarantine cleanup evidence changes only through the "
                "cleanup worker."
            )
        if (
            self.attempt_count != 0
            or self.last_attempt_at is not None
            or self.lease_token is not None
            or self.lease_expires_at is not None
            or self.completed_at is not None
        ):
            raise ValidationError(
                "New quarantine cleanup work must begin pending and unclaimed."
            )
        if not self.source_storage_key.startswith(
            "quarantine/correspondence/attachments/"
        ):
            raise ValidationError(
                "Quarantine cleanup requires an application-generated "
                "correspondence quarantine key."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Quarantine cleanup evidence cannot be deleted.")


class ClarificationCycle(models.Model):
    """Legal clarification state; receiving a reply never resolves it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        "records.PublicRecordsRequest",
        on_delete=models.PROTECT,
        related_name="clarification_cycles",
    )
    outbound_correspondence = models.OneToOneField(
        Correspondence,
        on_delete=models.PROTECT,
        related_name="opened_clarification_cycle",
    )
    inbound_response_correspondence = models.OneToOneField(
        Correspondence,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="responded_clarification_cycle",
    )
    opened_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_clarification_cycles",
    )
    resolution_notes = models.TextField(blank=True)
    opened_status_event = models.ForeignKey(
        "records.StatusEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="opened_clarification_cycles",
    )
    resolved_status_event = models.ForeignKey(
        "records.StatusEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="resolved_clarification_cycles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["opened_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(resolved_at__isnull=True, cancelled_at__isnull=True),
                name="correspondence_one_open_clarification_per_request",
            ),
            models.CheckConstraint(
                condition=~(Q(resolved_at__isnull=False) & Q(cancelled_at__isnull=False)),
                name="correspondence_clarification_not_resolved_and_cancelled",
            ),
            models.CheckConstraint(
                condition=(
                    Q(inbound_response_correspondence__isnull=True, responded_at__isnull=True)
                    | Q(inbound_response_correspondence__isnull=False, responded_at__isnull=False)
                ),
                name="correspondence_clarification_response_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(resolved_at__isnull=True)
                    | (
                        Q(inbound_response_correspondence__isnull=False)
                        & Q(responded_at__isnull=False)
                        & Q(resolved_by__isnull=False)
                        & ~Q(resolution_notes="")
                    )
                ),
                name="correspondence_clarification_resolution_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(responded_at__isnull=True)
                    | Q(responded_at__gte=models.F("opened_at"))
                ),
                name="correspondence_clarification_response_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(resolved_at__isnull=True)
                    | Q(resolved_at__gte=models.F("responded_at"))
                ),
                name="correspondence_clarification_resolution_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(cancelled_at__isnull=True)
                    | Q(cancelled_at__gte=models.F("opened_at"))
                ),
                name="correspondence_clarification_cancellation_chronology",
            ),
        ]

    def _validate_domain_invariants(self):
        errors = {}
        if self.outbound_correspondence_id:
            outbound = self.outbound_correspondence
            if (
                outbound.request_id != self.request_id
                or outbound.direction != Correspondence.Direction.OUTBOUND
                or outbound.kind
                not in {
                    Correspondence.Kind.CLARIFICATION_REQUEST,
                    Correspondence.Kind.SCOPE_CLARIFICATION,
                }
            ):
                errors["outbound_correspondence"] = (
                    "The cycle must use an outbound clarification for this request."
                )
        if self.inbound_response_correspondence_id:
            inbound = self.inbound_response_correspondence
            if (
                inbound.request_id != self.request_id
                or inbound.direction != Correspondence.Direction.INBOUND
                or inbound.kind != Correspondence.Kind.CLARIFICATION_RESPONSE
            ):
                errors["inbound_response_correspondence"] = (
                    "The response must be inbound clarification correspondence for this request."
                )
        if (
            self.responded_at is not None
            and self.responded_at < self.opened_at
        ):
            errors["responded_at"] = (
                "The clarification response cannot predate the cycle."
            )
        if self.resolved_at is not None:
            if not self.resolved_by_id or not self.resolved_by.is_staff:
                errors["resolved_by"] = "A human staff resolver is required."
            if not self.resolution_notes.strip():
                errors["resolution_notes"] = "Substantive resolution notes are required."
            if not self.inbound_response_correspondence_id or self.responded_at is None:
                errors["inbound_response_correspondence"] = (
                    "A received clarification response is required before resolution."
                )
            elif self.resolved_at < self.responded_at:
                errors["resolved_at"] = (
                    "Clarification resolution cannot predate the response."
                )
        if (
            self.cancelled_at is not None
            and self.cancelled_at < self.opened_at
        ):
            errors["cancelled_at"] = (
                "Clarification cancellation cannot predate the cycle."
            )
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self._validate_domain_invariants()

    def save(self, *args, **kwargs):
        self._validate_domain_invariants()
        return super().save(*args, **kwargs)
