"""
Core data model for the PSD401 Public Records Request system.

Design principles:
- Every state change is an immutable StatusEvent (audit + requester timeline).
- Statutory deadline logic lives in deadlines.py, not scattered in views.
- Redactions REQUIRE an exemption citation at the DB level (PRA compliance).
- Requester PII is minimized by design (PRA prohibits demanding purpose/ID
  beyond what's needed to respond).
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.append_only import AppendOnlyManager, AppendOnlyModelMixin


class Requester(models.Model):
    """Minimal-PII requester record; intake validates a response method."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=40, blank=True)
    # Retain the historical API name for street address while keeping the
    # remaining address components structured. Intake never folds these into
    # the immutable legal request description.
    mailing_address = models.TextField(blank=True)
    city = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    company = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.email


class RequestStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    CLARIFICATION = "clarification", "Awaiting Clarification"   # clock tolls
    GATHERING = "gathering", "Gathering Records"
    REVIEW = "review", "Review & Redaction"
    INSTALLMENT = "installment", "Producing Installments"
    FULFILLED = "fulfilled", "Fulfilled"
    CLOSED_WITHDRAWN = "closed_withdrawn", "Closed – Withdrawn"
    CLOSED_ABANDONED = "closed_abandoned", "Closed – Abandoned"
    CLOSED_NON_REQUEST = (
        "closed_non_request",
        "Closed – Not a Records Request",
    )
    CLOSED_NO_RECORDS = (
        "closed_no_records",
        "Closed – No Responsive Records",
    )
    #: Reached only through ``services.merge_requests()``: the requester's
    #: correspondence moved to the case of record and this number is retired.
    CLOSED_DUPLICATE = "closed_duplicate", "Closed – Duplicate"
    DENIED = "denied", "Denied (fully exempt)"


class Channel(models.TextChoices):
    PORTAL = "portal", "Online Portal"
    EMAIL = "email", "Email"
    MAIL = "mail", "US Mail"
    IN_PERSON = "in_person", "In Person"
    PHONE = "phone", "Phone"


#: Channels a requester reaches on their own. Every submission arriving this
#: way is a public records request; a litigation pull or a FERPA request is
#: opened by a named staff member on a staff channel (ADR 0004).
REQUESTER_ORIGINATED_CHANNELS = frozenset({Channel.PORTAL, Channel.EMAIL})


class MatterKind(models.TextChoices):
    """What law governs a case. See ``docs/decisions/0004-matter-kinds.md``.

    Collection, review, redaction, production, and the audit trail are shared
    by every kind. The RCW 42.56.520 clock, the day-5 safety net, the JLARC
    report, and the PRA letter instruments are not: they belong to
    ``PUBLIC_RECORDS`` alone.
    """

    PUBLIC_RECORDS = "public_records", "Public records request (RCW 42.56)"
    LITIGATION_DISCOVERY = (
        "litigation_discovery",
        "Litigation / discovery pull",
    )
    FERPA_PARENT = (
        "ferpa_parent",
        "FERPA parent or eligible student (34 CFR 99.10)",
    )


#: Per-kind case-number prefixes over independent sequences, so a number is
#: never mistaken for a public records request in mail, an export, or a filing.
MATTER_KIND_NUMBER_PREFIXES = {
    MatterKind.PUBLIC_RECORDS: "PRR",
    MatterKind.LITIGATION_DISCOVERY: "LIT",
    MatterKind.FERPA_PARENT: "FER",
}


class FerpaIdentityVerificationMethod(models.TextChoices):
    """How a parent or eligible student proved who they are (34 CFR 99.10)."""

    IN_PERSON_PHOTO_ID = "in_person_photo_id", "In-person photo identification"
    NOTARIZED_STATEMENT = "notarized_statement", "Notarized written statement"
    DISTRICT_RECORDS_MATCH = (
        "district_records_match",
        "Match against district student records",
    )
    COURT_ORDER = "court_order", "Court order or custody decree"


INITIAL_RESPONSE_STATUSES = frozenset(
    {
        RequestStatus.ACKNOWLEDGED,
        RequestStatus.CLARIFICATION,
    }
)


def awaiting_initial_response(queryset):
    """Return requests with no delivered acknowledgment or clarification."""
    return queryset.filter(
        acknowledgment_sent_at__isnull=True,
        legacy_import__legacy_initial_response_at__isnull=True,
        initial_response_attestation__isnull=True,
    ).exclude(
        events__to_status__in=INITIAL_RESPONSE_STATUSES,
    )


#: What law a case is governed by is decided when it is opened. Changing it
#: later would retroactively rewrite which clock the case owed, which letters
#: it was allowed to send, and whether it belonged in a report already filed.
MATTER_KIND_IMMUTABLE_MESSAGE = (
    "A matter's kind is fixed at intake; close a miscategorised case with a "
    "reason and open the correct kind."
)


class PublicRecordsRequestQuerySet(models.QuerySet):
    """Prevent the legal intake wording from being rewritten in bulk."""

    def update(self, **kwargs):
        if "description" in kwargs:
            raise ValidationError(
                "Request descriptions are immutable after intake; record "
                "clarifications as new status events."
            )
        if "matter_kind" in kwargs:
            raise ValidationError(MATTER_KIND_IMMUTABLE_MESSAGE)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if "description" in fields:
            raise ValidationError(
                "Request descriptions are immutable after intake; record "
                "clarifications as new status events."
            )
        if "matter_kind" in fields:
            raise ValidationError(MATTER_KIND_IMMUTABLE_MESSAGE)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def public_records(self):
        """Only RCW 42.56 requests — the set the PRA machinery may see."""
        return self.filter(matter_kind=MatterKind.PUBLIC_RECORDS)


class PublicRecordsRequest(models.Model):
    """A single PRA request. `number` is the public-facing ID (PRR-2026-0001)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=20, unique=True, editable=False)
    requester = models.ForeignKey(Requester, on_delete=models.PROTECT, related_name="requests")
    description = models.TextField(help_text="The request as originally worded. Never edit; clarify via events.")
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.PORTAL)
    status = models.CharField(max_length=30, choices=RequestStatus.choices, default=RequestStatus.RECEIVED)
    # Which law governs this matter (ADR 0004). Immutable after intake.
    matter_kind = models.CharField(
        max_length=30,
        choices=MatterKind.choices,
        default=MatterKind.PUBLIC_RECORDS,
    )

    received_at = models.DateTimeField(default=timezone.now)
    # RCW 42.56.520: respond within 5 business days. Computed by deadlines.py.
    # Only a public_records matter ever has one.
    statutory_response_due = models.DateField(null=True, blank=True)
    # 34 CFR 99.10(b): 45 CALENDAR days. Only a ferpa_parent matter has one.
    # Deliberately a separate field so no PRA query, watchdog, or report can
    # pick up a FERPA date by reading statutory_response_due.
    ferpa_access_due = models.DateField(null=True, blank=True)
    estimated_completion = models.DateField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    assigned_officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_requests",
    )
    # Tolling: total business days the clock was paused awaiting clarification.
    tolled_business_days = models.PositiveIntegerField(default=0)
    acknowledgment_sent_at = models.DateTimeField(null=True, blank=True)
    # Set only by services.merge_requests(): the case that absorbed this one.
    # Every requester email moved there and this case closed as a duplicate;
    # its own timeline and sent correspondence stay here, on the record.
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="merged_sources",
    )

    objects = PublicRecordsRequestQuerySet.as_manager()

    class Meta:
        ordering = ["-received_at"]
        permissions = [
            ("read_staff_api", "Can read the staff API through a service integration"),
        ]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["statutory_response_due"]),
            models.Index(fields=["matter_kind"]),
        ]

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if (
            self.pk
            and not self._state.adding
            and (
                update_fields is None
                or "description" in update_fields
                or "matter_kind" in update_fields
            )
        ):
            original = (
                type(self)._base_manager.filter(pk=self.pk)
                .values("description", "matter_kind")
                .first()
            )
            original_description = (
                original["description"] if original else None
            )
            if (
                original_description is not None
                and original_description != self.description
                and (update_fields is None or "description" in update_fields)
            ):
                raise ValidationError(
                    "Request descriptions are immutable after intake; record "
                    "clarifications as new status events."
                )
            if (
                original is not None
                and original["matter_kind"] != self.matter_kind
                and (update_fields is None or "matter_kind" in update_fields)
            ):
                raise ValidationError(MATTER_KIND_IMMUTABLE_MESSAGE)
        auto_assigning = not self.number
        if auto_assigning:
            self.number = self._next_number()
        if not auto_assigning:
            super().save(*args, **kwargs)
            return
        # Concurrent intake can race _next_number(): two requests may compute
        # the same next sequence and collide on the unique constraint. Retry
        # with a freshly generated number instead of surfacing the
        # IntegrityError to the requester.
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                # atomic() scopes the rollback to this one INSERT: inside an
                # outer transaction (TestCase, or any future atomic caller)
                # it's a savepoint; with no outer transaction (the intake
                # view today — no ATOMIC_REQUESTS) it's the outermost block.
                # Either way a failed attempt can't poison the caller.
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                if "number" not in str(exc).lower() or attempt == max_attempts - 1:
                    raise
                self.number = self._next_number()

    def _next_number(self):
        year = timezone.now().year
        kind_prefix = MATTER_KIND_NUMBER_PREFIXES[
            MatterKind(self.matter_kind)
        ]
        prefix = f"{kind_prefix}-{year}-"
        cls = type(self)
        last = (
            cls.objects.filter(number__startswith=prefix)
            .order_by("-number").values_list("number", flat=True).first()
        )
        seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{seq:04d}"

    @property
    def acknowledgment_overdue(self) -> bool:
        if (
            self.initial_response_delivered
            or self.statutory_response_due is None
        ):
            return False
        return timezone.localdate() > self.statutory_response_due

    @property
    def initial_response_delivered(self) -> bool:
        """Whether a valid RCW 42.56.520 first response was delivered."""
        if self.acknowledgment_sent_at is not None:
            return True
        try:
            if self.legacy_import.legacy_initial_response_at is not None:
                return True
        except LegacyRequestImport.DoesNotExist:
            pass
        # A Records Officer's written statement that the response went out
        # through a channel this system never delivered (see
        # InitialResponseAttestation). Recorded, not proven; the timeline and
        # audit trail say who said so and on what.
        try:
            if self.initial_response_attestation is not None:
                return True
        except InitialResponseAttestation.DoesNotExist:
            pass
        prefetched_events = getattr(
            self,
            "_prefetched_objects_cache",
            {},
        ).get("events")
        if prefetched_events is not None:
            return any(
                event.to_status in INITIAL_RESPONSE_STATUSES
                for event in prefetched_events
            )
        return self.events.filter(
            to_status__in=INITIAL_RESPONSE_STATUSES,
        ).exists()

    def __str__(self):
        return self.number


class StaffIntakeSubmission(models.Model):
    """Actor-scoped idempotency evidence for staff-entered legal intake."""

    class State(models.TextChoices):
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="staff_intake_submissions",
    )
    idempotency_key = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)
    request = models.ForeignKey(
        PublicRecordsRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff_intake_submissions",
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PROCESSING,
    )
    last_error = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submitted_by", "idempotency_key"],
                name="records_staff_intake_actor_key_unique",
            ),
        ]


class PublicIntakeSubmission(models.Model):
    """PII-free idempotency receipt for one signed public intake form."""

    class State(models.TextChoices):
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    token_sha256 = models.CharField(max_length=64, unique=True)
    payload_sha256 = models.CharField(max_length=64)
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PROCESSING,
    )
    request = models.OneToOneField(
        PublicRecordsRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="public_submission",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    last_error = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class IntakeAttachment(models.Model):
    """Untrusted requester-supplied context attached to an intake submission.

    Bytes enter a private quarantine prefix and are never exposed to staff
    until ClamAV reports them clean. They are not responsive RecordItems: they
    describe what the requester asked for rather than records gathered by the
    district in response.
    """

    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"
        ERROR = "error", "Scan error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="intake_attachments",
    )
    source_correspondence_attachment = models.OneToOneField(
        "correspondence.CorrespondenceAttachment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="intake_attachment",
    )
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    storage_key = models.CharField(max_length=1000)
    scan_status = models.CharField(
        max_length=20,
        choices=ScanStatus.choices,
        default=ScanStatus.PENDING,
    )
    scan_detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def effective_scan_status(self):
        """Use the original correspondence verdict for email intake files."""
        if self.source_correspondence_attachment_id is not None:
            return self.source_correspondence_attachment.scan_status
        return self.scan_status

    @property
    def effective_scanned_at(self):
        if self.source_correspondence_attachment_id is not None:
            return self.source_correspondence_attachment.scanned_at
        return self.scanned_at

    @property
    def effective_storage_key(self):
        if self.source_correspondence_attachment_id is not None:
            return self.source_correspondence_attachment.storage_key
        return self.storage_key

    @property
    def is_downloadable(self):
        if self.source_correspondence_attachment_id is not None:
            return self.source_correspondence_attachment.is_downloadable
        return self.scan_status == self.ScanStatus.CLEAN


class StatusEvent(AppendOnlyModelMixin, models.Model):
    """Append-only lifecycle log. Powers the requester-visible timeline,
    internal audit, and JLARC response-time metrics."""

    class CorrespondenceEffect(models.TextChoices):
        ACKNOWLEDGMENT_DELIVERY = (
            "acknowledgment_delivery",
            "Acknowledgment delivery",
        )
        CLARIFICATION_DELIVERY = (
            "clarification_delivery",
            "Clarification delivery",
        )
        ESTIMATE_REVISION_DELIVERY = (
            "estimate_revision_delivery",
            "Estimate-revision delivery",
        )
        DENIAL_DELIVERY = "denial_delivery", "Denial delivery"

    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="events",
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30, blank=True)
    note = models.TextField(blank=True)
    visible_to_requester = models.BooleanField(default=False)
    correspondence_effect = models.CharField(
        max_length=40,
        choices=CorrespondenceEffect.choices,
        blank=True,
        default="",
    )

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["occurred_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(correspondence_effect="")
                    | models.Q(
                        correspondence_effect=(
                            "acknowledgment_delivery"
                        ),
                        to_status=RequestStatus.ACKNOWLEDGED,
                    )
                    | models.Q(
                        correspondence_effect=(
                            "acknowledgment_delivery"
                        ),
                        from_status="",
                        to_status="",
                    )
                    | models.Q(
                        correspondence_effect=(
                            "clarification_delivery"
                        ),
                        to_status=RequestStatus.CLARIFICATION,
                    )
                    | models.Q(
                        correspondence_effect=(
                            "estimate_revision_delivery"
                        ),
                        from_status="",
                        to_status="",
                    )
                    | models.Q(
                        correspondence_effect="denial_delivery",
                        to_status=RequestStatus.DENIED,
                    )
                ),
                name="status_event_correspondence_effect_shape",
            ),
        ]


class ExemptionCitationQuerySet(models.QuerySet):
    MEANING_FIELDS = frozenset(
        {
            "citation",
            "short_name",
            "description",
            "requires_second_review",
        }
    )

    def _contains_used_citation(self):
        return self.filter(
            Q(withheld_items__isnull=False)
            | Q(primary_withheld_items__isnull=False)
            | Q(redaction__isnull=False)
            | Q(redaction_proposals__isnull=False)
            | Q(denial_correspondence_bases__isnull=False)
        ).exists()

    def update(self, **kwargs):
        if self.MEANING_FIELDS.intersection(kwargs) and (
            self._contains_used_citation()
        ):
            raise ValidationError(
                "A citation already used as legal evidence cannot be "
                "rewritten. Deactivate it and create a replacement citation."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self.MEANING_FIELDS.intersection(fields):
            used_ids = set(
                self.model.objects.filter(
                    Q(withheld_items__isnull=False)
                    | Q(primary_withheld_items__isnull=False)
                    | Q(redaction__isnull=False)
                    | Q(redaction_proposals__isnull=False)
                    | Q(denial_correspondence_bases__isnull=False),
                    pk__in=[obj.pk for obj in objs if obj.pk is not None],
                ).values_list("pk", flat=True)
            )
            if used_ids:
                raise ValidationError(
                    "A citation already used as legal evidence cannot be "
                    "rewritten. Deactivate it and create a replacement citation."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)


class ExemptionCitation(models.Model):
    """Seeded taxonomy of RCW 42.56 / FERPA / other exemptions.
    Every redaction and withholding must FK to one of these."""
    citation = models.CharField(max_length=100, unique=True)   # e.g. "RCW 42.56.230(1)"
    short_name = models.CharField(max_length=200)              # e.g. "Personal information – students"
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    requires_second_review = models.BooleanField(
        default=False, help_text="e.g. FERPA-adjacent exemptions get a second reviewer gate."
    )

    objects = ExemptionCitationQuerySet.as_manager()

    def is_in_use(self):
        return (
            self.withheld_items.exists()
            or self.primary_withheld_items.exists()
            or self.redaction_set.exists()
            or self.redaction_proposals.exists()
            or self.denial_correspondence_bases.exists()
        )

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            current = type(self).objects.filter(pk=self.pk).first()
            if current is not None:
                changed_meaning = any(
                    getattr(current, field) != getattr(self, field)
                    for field in ExemptionCitationQuerySet.MEANING_FIELDS
                )
                if changed_meaning and current.is_in_use():
                    raise ValidationError(
                        "A citation already used as legal evidence cannot be "
                        "rewritten. Deactivate it and create a replacement "
                        "citation."
                    )
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.citation} — {self.short_name}"


class CollectionTask(models.Model):
    """A search assignment: either a manual department task or an automated
    Vault matter/search/export (tracked via vault_* fields)."""
    class Kind(models.TextChoices):
        VAULT_MAIL = "vault_mail", "Vault – Gmail"
        VAULT_DRIVE = "vault_drive", "Vault – Drive"
        MANUAL = "manual", "Manual / Department"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        INGESTING = "ingesting", "Ingesting export"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    request = models.ForeignKey(PublicRecordsRequest, on_delete=models.CASCADE, related_name="collection_tasks")
    system = models.ForeignKey("RecordSystem", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="collection_tasks")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    instructions = models.TextField(blank=True)
    custodian_emails = models.JSONField(default=list, blank=True)
    search_terms = models.CharField(max_length=1000, blank=True)
    date_range_start = models.DateField(null=True, blank=True)
    date_range_end = models.DateField(null=True, blank=True)
    source_vault_count = models.OneToOneField(
        "VaultCountRun",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="collection_task",
    )
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    due_date = models.DateField(null=True, blank=True)

    # Vault linkage (see integrations/vault/client.py)
    vault_matter_id = models.CharField(max_length=100, blank=True)
    vault_hold_id = models.CharField(max_length=100, blank=True)
    vault_export_id = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class VaultCountRun(models.Model):
    """Human-scoped Google Vault message count with durable provenance.

    Count runs estimate a search universe. They never create a hold, export,
    collection task, legal outcome, or requester communication.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    class SearchScope(models.TextChoices):
        ACCOUNTS = "accounts", "Specific district accounts"
        ENTIRE_ORG = "entire_org", "All district accounts"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        AUTOMATIC = "automatic", "Automatic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="vault_count_runs",
    )
    trigger = models.CharField(
        max_length=20,
        choices=Trigger.choices,
        default=Trigger.MANUAL,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_vault_count_runs",
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
    )
    corpus = models.CharField(max_length=20, default="MAIL", editable=False)
    search_scope = models.CharField(
        max_length=20,
        choices=SearchScope.choices,
        default=SearchScope.ACCOUNTS,
    )
    terms = models.CharField(max_length=1000)
    custodian_emails = models.JSONField(default=list)
    date_range_start = models.DateField(null=True, blank=True)
    date_range_end = models.DateField(null=True, blank=True)
    input_sha256 = models.CharField(max_length=64)

    # Provider linkage is retained for resumable polling but is not exposed by
    # the staff REST serializer.
    vault_matter_id = models.CharField(max_length=100, blank=True)
    operation_name = models.CharField(max_length=255, blank=True)

    total_count = models.PositiveBigIntegerField(null=True, blank=True)
    queried_accounts_count = models.PositiveIntegerField(null=True, blank=True)
    matching_accounts_count = models.PositiveIntegerField(null=True, blank=True)
    non_queryable_accounts_count = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    accounts_with_errors_count = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    failure_code = models.CharField(max_length=100, blank=True)
    failure_detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(state="complete")
                    | Q(total_count__isnull=False, completed_at__isnull=False)
                ),
                name="records_vault_count_complete_has_result",
            ),
            # A count with no accountable human is only ever the intake-time
            # automatic one. Every officer-scoped count stays attributed.
            models.CheckConstraint(
                condition=(
                    Q(requested_by__isnull=False) | Q(trigger="automatic")
                ),
                name="records_manual_vault_count_requires_requester",
            ),
        ]


class VaultIngestRun(models.Model):
    """Durable, resumable evidence for one CollectionTask export ingest."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        FAILED = "failed", "Failed"
        COMPLETE = "complete", "Complete"

    collection_task = models.OneToOneField(
        CollectionTask,
        on_delete=models.CASCADE,
        related_name="vault_ingest_run",
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
    )
    export_snapshot = models.JSONField(default=dict, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    files_total = models.PositiveIntegerField(default=0)
    files_processed = models.PositiveIntegerField(default=0)
    messages_seen = models.PositiveIntegerField(default=0)
    items_created = models.PositiveIntegerField(default=0)
    duplicates_skipped = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=100, blank=True)
    last_error_detail = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class VaultExportObject(models.Model):
    """One GCS export object retained immutably before any parsing."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RETAINED = "retained", "Retained"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    ingest_run = models.ForeignKey(
        VaultIngestRun,
        on_delete=models.CASCADE,
        related_name="export_objects",
    )
    object_identity_sha256 = models.CharField(max_length=64)
    bucket_name = models.CharField(max_length=255)
    object_name = models.CharField(max_length=1000)
    expected_size = models.PositiveBigIntegerField(default=0)
    storage_key = models.CharField(max_length=1000, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
    )
    last_error_code = models.CharField(max_length=100, blank=True)
    last_error_detail = models.CharField(max_length=500, blank=True)
    retained_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["ingest_run", "object_identity_sha256"],
                name="records_vault_export_object_identity_unique",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(storage_key="")
                        & Q(sha256="")
                        & Q(size__isnull=True)
                        & Q(retained_at__isnull=True)
                    )
                    | (
                        ~Q(storage_key="")
                        & ~Q(sha256="")
                        & Q(size__isnull=False)
                        & Q(retained_at__isnull=False)
                    )
                ),
                name="records_vault_export_retention_complete",
            ),
        ]


class VaultMessageArtifact(models.Model):
    """Per-message resume and deduplication evidence from a retained MBOX."""

    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        RAW_RETAINED = "raw_retained", "Raw message retained"
        CREATED = "created", "Record item created"
        DUPLICATE = "duplicate", "Duplicate skipped"
        FAILED = "failed", "Failed"

    export_object = models.ForeignKey(
        VaultExportObject,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    ordinal = models.PositiveIntegerField()
    message_id = models.CharField(max_length=998, blank=True)
    raw_storage_key = models.CharField(max_length=1000, blank=True)
    raw_sha256 = models.CharField(max_length=64, blank=True)
    raw_size = models.PositiveBigIntegerField(null=True, blank=True)
    rendered_sha256 = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        default=Outcome.PENDING,
    )
    record_item = models.ForeignKey(
        "RecordItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="vault_source_messages",
    )
    duplicate_of = models.ForeignKey(
        "RecordItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="duplicate_vault_messages",
    )
    last_error_code = models.CharField(max_length=100, blank=True)
    last_error_detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["export_object_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(
                fields=["export_object", "ordinal"],
                name="records_vault_message_ordinal_unique",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(raw_storage_key="")
                        & Q(raw_sha256="")
                        & Q(raw_size__isnull=True)
                    )
                    | (
                        ~Q(raw_storage_key="")
                        & ~Q(raw_sha256="")
                        & Q(raw_size__isnull=False)
                    )
                ),
                name="records_vault_message_raw_complete",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(outcome="created")
                        & Q(record_item__isnull=False)
                        & Q(duplicate_of__isnull=True)
                    )
                    | (
                        Q(outcome="duplicate")
                        & Q(record_item__isnull=True)
                        & Q(duplicate_of__isnull=False)
                    )
                    | (
                        Q(
                            outcome__in=[
                                "pending",
                                "raw_retained",
                                "failed",
                            ]
                        )
                        & Q(record_item__isnull=True)
                        & Q(duplicate_of__isnull=True)
                    )
                ),
                name="records_vault_message_outcome_evidence",
            ),
        ]


class RecordItem(models.Model):
    """One responsive (or candidate) record in the review queue."""
    class DerivativeState(models.TextChoices):
        NONE = "none", "Not needed"
        PENDING = "pending", "Waiting to be rendered"
        READY = "ready", "Rendered and reviewable"
        UNSUPPORTED = "unsupported", "No renderer for this format"
        FAILED = "failed", "Rendering failed"

    class ReviewState(models.TextChoices):
        UNREVIEWED = "unreviewed", "Unreviewed"
        RESPONSIVE = "responsive", "Responsive"
        NON_RESPONSIVE = "non_responsive", "Non-responsive"
        DUPLICATE = "duplicate", "Duplicate"
        WITHHELD = "withheld", "Withheld (fully exempt)"
        #: Left the review set when a Records Officer superseded the
        #: collection that brought it in (an over-broad export). Never set by
        #: a single-record review; see collection_supersession.
        SUPERSEDED = "superseded", "Superseded (collection re-scoped)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(PublicRecordsRequest, on_delete=models.CASCADE, related_name="record_items")
    collection_task = models.ForeignKey(CollectionTask, null=True, blank=True, on_delete=models.SET_NULL)
    source_system = models.CharField(max_length=100, blank=True)   # gmail, drive, upload, SIS...
    #: The primary custodian this record was found in. When one message was
    #: collected from several mailboxes it is deduplicated onto one record and
    #: every mailbox is recorded in ``custodians`` below; this field then names
    #: the first one so existing single-custodian displays keep working. It is
    #: never a comma-joined list.
    custodian = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=500, blank=True)
    storage_key = models.CharField(max_length=1000)                # object-store key (original)
    content_length = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(
        max_length=255,
        default="application/octet-stream",
    )
    #: A PDF rendition of a record that is not itself a PDF, so a human can
    #: read it and redact inside it. The original bytes above remain the
    #: record; this is only how the reviewer sees it. Empty when the record is
    #: already a PDF, or when no renderer exists for its format.
    derivative_storage_key = models.CharField(max_length=1000, blank=True)
    derivative_sha256 = models.CharField(max_length=64, blank=True)
    derivative_content_length = models.PositiveBigIntegerField(null=True, blank=True)
    derivative_state = models.CharField(
        max_length=20,
        default=DerivativeState.NONE,
        choices=DerivativeState.choices,
    )
    derivative_detail = models.CharField(max_length=500, blank=True)
    redacted_storage_key = models.CharField(max_length=1000, blank=True)
    redacted_sha256 = models.CharField(max_length=64, blank=True)
    redacted_content_length = models.PositiveBigIntegerField(null=True, blank=True)
    redacted_content_type = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)        # dedupe + chain of custody
    ocr_text = models.TextField(blank=True)                        # searchable text
    review_state = models.CharField(max_length=20, choices=ReviewState.choices, default=ReviewState.UNREVIEWED)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    second_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="second_reviewed_record_items",
    )
    second_reviewed_at = models.DateTimeField(null=True, blank=True)
    withholding_exemption = models.ForeignKey(
        ExemptionCitation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="primary_withheld_items",
        help_text=(
            "Required legal basis when the record is fully withheld. "
            "Additional bases may be attached below."
        ),
    )
    withholding_exemptions = models.ManyToManyField(
        ExemptionCitation,
        blank=True,
        related_name="withheld_items",
        through="RecordItemWithholdingExemption",
    )
    redacted_manifest_sha256 = models.CharField(max_length=64, blank=True)
    # --- Email metadata, read from the retained message at ingest -----------
    # Facts about the message, so review can filter, group, and deduplicate
    # without re-parsing bytes. Blank on a record that is not a message.
    email_message_id = models.CharField(max_length=998, blank=True)
    #: SHA-256 of the normalized body text, paired with the Message-ID so a
    #: sender-supplied header is never trusted as an identity on its own.
    email_body_sha256 = models.CharField(max_length=64, blank=True)
    email_in_reply_to = models.CharField(max_length=998, blank=True)
    email_from = models.CharField(max_length=500, blank=True)
    email_to = models.TextField(blank=True)
    email_cc = models.TextField(blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_sender_domain = models.CharField(max_length=253, blank=True, db_index=True)
    email_auto_submitted = models.CharField(max_length=100, blank=True)
    email_list_id = models.CharField(max_length=500, blank=True)
    email_precedence = models.CharField(max_length=100, blank=True)
    email_has_calendar_part = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The deduplication key, enforced by the database rather than by a
            # read-then-write race: two ingest runs for two custodian mailboxes
            # of the same request run concurrently, and without this both could
            # see no duplicate and each create a record for one message. Its
            # (request, email_message_id) prefix also serves the lookup.
            # Both halves must be present. A message with no text part at all
            # -- attachments or a bare calendar invitation -- has no body to
            # fingerprint, and an absent digest corroborates nothing. Trusting
            # the sender-supplied Message-ID on its own is precisely what the
            # digest exists to prevent, so such a record is simply not
            # deduplicated here and reaches review on its own.
            models.UniqueConstraint(
                fields=["request", "email_message_id", "email_body_sha256"],
                condition=~Q(email_message_id="") & ~Q(email_body_sha256=""),
                name="records_item_unique_message_per_request",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(review_state="withheld")
                    | Q(withholding_exemption__isnull=False)
                ),
                name="records_withheld_item_requires_exemption",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(redacted_storage_key="")
                        & Q(redacted_sha256="")
                        & Q(redacted_content_length__isnull=True)
                        & Q(redacted_content_type="")
                        & Q(redacted_manifest_sha256="")
                    )
                    | (
                        ~Q(redacted_storage_key="")
                        & ~Q(redacted_sha256="")
                        & Q(redacted_content_length__isnull=False)
                        & ~Q(redacted_content_type="")
                        & ~Q(redacted_manifest_sha256="")
                    )
                ),
                name="records_redacted_artifact_complete",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(second_reviewed_by__isnull=True)
                        & Q(second_reviewed_at__isnull=True)
                    )
                    | (
                        Q(second_reviewed_by__isnull=False)
                        & Q(second_reviewed_at__isnull=False)
                        & ~Q(second_reviewed_by=models.F("reviewed_by"))
                    )
                ),
                name="records_second_review_complete_and_distinct",
            ),
            models.CheckConstraint(
                condition=(
                    Q(redacted_storage_key="")
                    | Q(review_state="responsive")
                ),
                name="records_redacted_artifact_is_responsive",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.review_state == self.ReviewState.WITHHELD
            and self.withholding_exemption_id is None
        ):
            raise ValidationError(
                {
                    "withholding_exemption": (
                        "A fully withheld record requires an exemption citation."
                    )
                }
            )

    @property
    def second_review_required(self):
        if self.review_state == self.ReviewState.WITHHELD:
            if (
                self.withholding_exemption_id is not None
                and self.withholding_exemption.requires_second_review
            ):
                return True
            if self.withholding_exemptions.filter(
                requires_second_review=True,
            ).exists():
                return True
        return self.redactions.filter(
            voided_at__isnull=True,
            exemption__requires_second_review=True,
        ).exists()


class RecordItemCustodian(models.Model):
    """One mailbox or system a record was actually found in.

    A single message sent to several district accounts is one public record,
    not N. Review sees it once, and this table keeps the *whole* custodial
    truth — which is evidence about where the record lives, and is needed for
    an adequate-search account of the collection.
    """

    record_item = models.ForeignKey(
        RecordItem,
        on_delete=models.CASCADE,
        related_name="custodians",
    )
    custodian = models.CharField(max_length=255)
    collection_task = models.ForeignKey(
        CollectionTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="record_item_custodians",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["custodian"]
        constraints = [
            models.UniqueConstraint(
                fields=["record_item", "custodian"],
                name="records_unique_record_item_custodian",
            ),
        ]

    def __str__(self):
        return self.custodian


class CounselDomain(models.Model):
    """An email domain belonging to the district's own legal counsel.

    Seeded empty and maintained by an application administrator. Privilege
    attaches to communications with the district's *own* lawyers, so this list
    holds only those firms; a plaintiff's firm needs no entry because the
    external-sender tag and the sender-domain filter already separate it.

    An entry only ever *tags* records for a human to look at. It withholds
    nothing: the withheld path still requires a named reviewer, an
    ExemptionCitation, and second review.
    """

    domain = models.CharField(max_length=253, unique=True)
    note = models.CharField(max_length=500, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain"]

    def __str__(self):
        return self.domain


class RecordItemWithholdingExemption(models.Model):
    """A protected legal basis joining a withheld record to its citation."""

    record_item = models.ForeignKey(RecordItem, on_delete=models.CASCADE)
    exemption = models.ForeignKey(
        ExemptionCitation,
        on_delete=models.PROTECT,
        related_name="withheld_item_bases",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["record_item", "exemption"],
                name="records_unique_record_item_withholding_exemption",
            )
        ]


class AdvisoryRun(models.Model):
    """Operational AI run whose proposal is permanently non-applying."""

    class Kind(models.TextChoices):
        REQUEST_INTENT = "request_intent", "Request intent"
        #: A neutral one-line label for a case, for the staff list column. It
        #: reads the request and proposes nothing else: no search terms, no
        #: systems, no Vault query, and so nothing an unattended count could
        #: ever be built from. That is why it may run automatically at intake
        #: on its own switch, independently of request intent.
        CASE_TITLE = "case_title", "Case title"
        RECORD_RESPONSIVENESS = (
            "record_responsiveness",
            "Record responsiveness",
        )
        RECORD_REDACTIONS = "record_redactions", "Record redaction candidates"
        #: Whether a newly created case is a new request, a follow-up to or
        #: duplicate of an earlier case, related to one, not a records request,
        #: or spam. A label for a person to confirm; it changes nothing itself.
        INTAKE_CLASSIFICATION = "intake_classification", "Intake classification"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        AUTOMATIC = "automatic", "Automatic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
    )
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="advisory_runs",
    )
    record_item = models.ForeignKey(
        RecordItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="advisory_runs",
    )
    trigger = models.CharField(
        max_length=20,
        choices=Trigger.choices,
        default=Trigger.MANUAL,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_advisory_runs",
    )
    model_identifier = models.CharField(max_length=255)
    input_sha256 = models.CharField(max_length=64)
    input_truncated = models.BooleanField(default=False)
    proposal = models.JSONField(default=dict, blank=True)
    # A DB invariant makes the architectural boundary independently testable:
    # no provider result is ever an applied legal outcome.
    applied = models.BooleanField(default=False, editable=False)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(applied=False),
                name="records_advisory_never_applied",
            ),
            models.CheckConstraint(
                condition=(
                    Q(requested_by__isnull=False)
                    | Q(
                        trigger="automatic",
                        kind__in=[
                            "request_intent",
                            "case_title",
                            "record_responsiveness",
                            "record_redactions",
                            "intake_classification",
                        ],
                    )
                ),
                name="records_manual_advisory_requires_requester",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        kind="request_intent",
                        record_item__isnull=True,
                    )
                    | Q(
                        kind="case_title",
                        record_item__isnull=True,
                    )
                    | Q(
                        kind="intake_classification",
                        record_item__isnull=True,
                    )
                    | Q(
                        kind="record_responsiveness",
                        record_item__isnull=False,
                    )
                    | Q(
                        kind="record_redactions",
                        record_item__isnull=False,
                    )
                ),
                name="records_advisory_target_shape",
            ),
        ]


class RedactionProposal(AppendOnlyModelMixin, models.Model):
    """One coordinate-grounded model candidate, never a legal redaction."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    advisory_run = models.ForeignKey(
        AdvisoryRun,
        on_delete=models.PROTECT,
        related_name="redaction_proposals",
    )
    record_item = models.ForeignKey(
        RecordItem,
        on_delete=models.PROTECT,
        related_name="redaction_proposals",
    )
    page_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    x0 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    y0 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    x1 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    y1 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    exemption = models.ForeignKey(
        ExemptionCitation,
        on_delete=models.PROTECT,
        related_name="redaction_proposals",
    )
    confidence = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    rationale = models.CharField(max_length=1000)
    source_span_ids = models.JSONField()
    source_text_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    def clean(self):
        super().clean()
        if (
            not isinstance(self.source_span_ids, list)
            or not self.source_span_ids
            or any(not isinstance(value, str) or not value for value in self.source_span_ids)
        ):
            raise ValidationError(
                {"source_span_ids": "Grounded source-span evidence is required."}
            )
        if self.advisory_run_id is not None:
            run = self.advisory_run
            if (
                run.kind != AdvisoryRun.Kind.RECORD_REDACTIONS
                or run.record_item_id != self.record_item_id
            ):
                raise ValidationError(
                    "A redaction proposal must match its record-targeted advisory run."
                )

    class Meta:
        ordering = ["page_number", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(page_number__gte=1),
                name="records_redaction_proposal_page_positive",
            ),
            models.CheckConstraint(
                condition=Q(x0__lt=models.F("x1")),
                name="records_redaction_proposal_x_ordered",
            ),
            models.CheckConstraint(
                condition=Q(y0__lt=models.F("y1")),
                name="records_redaction_proposal_y_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    Q(x0__gte=0.0)
                    & Q(x0__lte=1.0)
                    & Q(y0__gte=0.0)
                    & Q(y0__lte=1.0)
                    & Q(x1__gte=0.0)
                    & Q(x1__lte=1.0)
                    & Q(y1__gte=0.0)
                    & Q(y1__lte=1.0)
                    & Q(confidence__gte=0.0)
                    & Q(confidence__lte=1.0)
                ),
                name="records_redaction_proposal_values_normalized",
            ),
        ]


class RedactionProposalDecision(AppendOnlyModelMixin, models.Model):
    """Named human disposition of one immutable AI redaction candidate."""

    class Outcome(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.OneToOneField(
        RedactionProposal,
        on_delete=models.PROTECT,
        related_name="decision",
    )
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="redaction_proposal_decisions",
    )
    reason = models.CharField(max_length=500, blank=True)
    redaction = models.OneToOneField(
        "Redaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proposal_decision",
    )
    decided_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    def clean(self):
        super().clean()
        if (
            self.redaction_id is not None
            and self.proposal_id is not None
            and self.redaction.record_item_id != self.proposal.record_item_id
        ):
            raise ValidationError(
                "An accepted proposal must create a redaction on the same record."
            )

    class Meta:
        ordering = ["decided_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(outcome="accepted", redaction__isnull=False)
                    | Q(outcome="rejected", redaction__isnull=True)
                ),
                name="records_redaction_proposal_decision_shape",
            ),
            models.CheckConstraint(
                condition=(~Q(outcome="rejected") | ~Q(reason="")),
                name="records_rejected_redaction_proposal_reason",
            ),
        ]


class AdequateSearchAttestation(AppendOnlyModelMixin, models.Model):
    """Human evidence supporting a no-records/out-of-retention closure."""

    class Determination(models.TextChoices):
        NO_RESPONSIVE_RECORDS = (
            "no_responsive_records",
            "No responsive records located",
        )
        OUTSIDE_RETENTION = (
            "outside_retention",
            "Requested records outside retention",
        )
        #: WAC 44-14-04003(6): a request for information rather than for an
        #: identifiable existing record. The District does not maintain a
        #: record answering it and is not required to create one. This is a
        #: no-record determination, not an exemption.
        NOT_MAINTAINED = (
            "not_maintained",
            "No record maintained; the District is not required to create one",
        )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="adequate_search_attestations",
    )
    correspondence = models.OneToOneField(
        "correspondence.Correspondence",
        on_delete=models.PROTECT,
        related_name="adequate_search_attestation",
    )
    determination = models.CharField(max_length=30, choices=Determination.choices)
    search_completed_at = models.DateTimeField()
    search_steps = models.JSONField()
    task_snapshot = models.JSONField()
    task_snapshot_sha256 = models.CharField(max_length=64)
    adequacy_reason = models.CharField(max_length=2000)
    retention_schedule_reference = models.CharField(max_length=1000, blank=True)
    incident_hold_checked = models.BooleanField(default=False)
    prior_exports_checked = models.BooleanField(default=False)
    email_drive_copies_checked = models.BooleanField(default=False)
    other_copies_checked = models.BooleanField(default=False)
    counsel_approval_reference = models.CharField(max_length=1000)
    attested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="adequate_search_attestations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    determination__in=[
                        "no_responsive_records",
                        "outside_retention",
                        "not_maintained",
                    ]
                ),
                name="records_search_determination_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(determination="outside_retention")
                    | (
                        ~Q(retention_schedule_reference="")
                        & Q(incident_hold_checked=True)
                        & Q(prior_exports_checked=True)
                        & Q(email_drive_copies_checked=True)
                        & Q(other_copies_checked=True)
                    )
                ),
                name="records_outside_retention_checks_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(adequacy_reason="")
                    & ~Q(counsel_approval_reference="")
                ),
                name="records_search_substantive_evidence",
            ),
        ]


class Redaction(models.Model):
    """A single redaction region. Exemption FK is NOT nullable — the PRA
    exemption log (RCW 42.56.210(3)) is generated from these rows."""
    record_item = models.ForeignKey(RecordItem, on_delete=models.CASCADE, related_name="redactions")
    page_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    # Normalized coordinates (0..1) so they survive rasterization at any DPI.
    x0 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    y0 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    x1 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    y1 = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    exemption = models.ForeignKey(ExemptionCitation, on_delete=models.PROTECT)
    explanation = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_redactions",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(page_number__gte=1),
                name="records_redaction_page_number_positive",
            ),
            models.CheckConstraint(
                condition=Q(x0__lt=models.F("x1")),
                name="records_redaction_x_coordinates_ordered",
            ),
            models.CheckConstraint(
                condition=Q(y0__lt=models.F("y1")),
                name="records_redaction_y_coordinates_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    Q(x0__gte=0.0)
                    & Q(x0__lte=1.0)
                    & Q(y0__gte=0.0)
                    & Q(y0__lte=1.0)
                    & Q(x1__gte=0.0)
                    & Q(x1__lte=1.0)
                    & Q(y1__gte=0.0)
                    & Q(y1__lte=1.0)
                ),
                name="records_redaction_coordinates_normalized",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(voided_at__isnull=True)
                        & Q(voided_by__isnull=True)
                        & Q(void_reason="")
                    )
                    | (
                        Q(voided_at__isnull=False)
                        & Q(voided_by__isnull=False)
                        & ~Q(void_reason="")
                    )
                ),
                name="records_redaction_void_evidence_complete",
            ),
        ]


class Installment(models.Model):
    """One production/release of records to the requester (RCW 42.56.120)."""
    request = models.ForeignKey(PublicRecordsRequest, on_delete=models.CASCADE, related_name="installments")
    sequence = models.PositiveIntegerField()
    items = models.ManyToManyField(RecordItem, related_name="installments")
    exemption_log_storage_key = models.CharField(max_length=1000, blank=True)
    exemption_log_sha256 = models.CharField(max_length=64, blank=True)
    exemption_log_size = models.PositiveBigIntegerField(null=True, blank=True)
    exemption_log_manifest_sha256 = models.CharField(max_length=64, blank=True)
    exemption_log_prepared_at = models.DateTimeField(null=True, blank=True)
    exemption_log_prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prepared_exemption_logs",
    )
    package_storage_key = models.CharField(max_length=1000, blank=True)
    package_sha256 = models.CharField(max_length=64, blank=True)
    package_size = models.PositiveBigIntegerField(null=True, blank=True)
    package_manifest_sha256 = models.CharField(max_length=64, blank=True)
    package_prepared_at = models.DateTimeField(null=True, blank=True)
    package_prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prepared_installments",
    )
    fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fee_paid_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    download_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    download_expires_at = models.DateTimeField(null=True, blank=True)
    #: Plain counters, not an audit substitute. ``AuditLog`` remains the legal
    #: record of every download; these exist so an officer can see at a glance
    #: whether the requester has collected the installment.
    download_count = models.PositiveIntegerField(default=0)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    #: An optional second factor the officer sets and communicates in a
    #: separate message. Stored only as a Django password hash: nothing in the
    #: application can read the code back, including the officer who set it.
    access_code_hash = models.CharField(max_length=255, blank=True)
    access_code_set_at = models.DateTimeField(null=True, blank=True)
    access_code_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="access_coded_installments",
    )
    #: Revocation withdraws requester access without destroying the released
    #: artifact or its audit trail. A revoked link answers 410, never 404, so
    #: the requester learns the link is dead rather than that it never existed.
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_installments",
    )
    revocation_reason = models.CharField(max_length=1000, blank=True)

    class Meta:
        unique_together = [("request", "sequence")]
        ordering = ["sequence"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (
                        Q(exemption_log_storage_key="")
                        & Q(exemption_log_sha256="")
                        & Q(exemption_log_size__isnull=True)
                        & Q(exemption_log_manifest_sha256="")
                        & Q(exemption_log_prepared_at__isnull=True)
                    )
                    | (
                        ~Q(exemption_log_storage_key="")
                        & ~Q(exemption_log_sha256="")
                        & Q(exemption_log_size__isnull=False)
                        & ~Q(exemption_log_manifest_sha256="")
                        & Q(exemption_log_prepared_at__isnull=False)
                    )
                ),
                name="records_exemption_log_artifact_complete",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(package_storage_key="")
                        & Q(package_sha256="")
                        & Q(package_size__isnull=True)
                        & Q(package_manifest_sha256="")
                        & Q(package_prepared_at__isnull=True)
                    )
                    | (
                        ~Q(package_storage_key="")
                        & ~Q(package_sha256="")
                        & Q(package_size__isnull=False)
                        & ~Q(package_manifest_sha256="")
                        & Q(package_prepared_at__isnull=False)
                    )
                ),
                name="records_installment_package_complete",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(revoked_at__isnull=True) & Q(revocation_reason=""))
                    | (Q(revoked_at__isnull=False) & ~Q(revocation_reason=""))
                ),
                name="records_installment_revocation_complete",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(access_code_hash="") & Q(access_code_set_at__isnull=True))
                    | (
                        ~Q(access_code_hash="")
                        & Q(access_code_set_at__isnull=False)
                    )
                ),
                name="records_installment_access_code_complete",
            ),
        ]


class FeeSchedule(AppendOnlyModelMixin, models.Model):
    """Immutable snapshot of one formally adopted RCW 42.56.120 schedule."""

    class Basis(models.TextChoices):
        STATUTORY_DEFAULT = "statutory_default", "Statutory default schedule"
        ACTUAL_COST = "actual_cost", "Published actual-cost schedule"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    basis = models.CharField(max_length=30, choices=Basis.choices)
    effective_on = models.DateField(db_index=True)
    counsel_approval_reference = models.CharField(max_length=500)
    adoption_reference = models.CharField(max_length=500)
    actual_cost_statement_reference = models.CharField(max_length=500, blank=True)
    photocopy_page_rate = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.15"),
        validators=[MinValueValidator(Decimal(0))],
    )
    scanned_page_rate = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.10"),
        validators=[MinValueValidator(Decimal(0))],
    )
    electronic_file_bundle_rate = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.05"),
        validators=[MinValueValidator(Decimal(0))],
        help_text="Charge for each four electronic files or attachments.",
    )
    electronic_gb_rate = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.10"),
        validators=[MinValueValidator(Decimal(0))],
    )
    flat_fee_amount = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True,
        blank=True,
        default=Decimal("2.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activated_fee_schedules",
    )
    activated_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["-effective_on", "-activated_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(photocopy_page_rate__gte=0)
                    & Q(scanned_page_rate__gte=0)
                    & Q(electronic_file_bundle_rate__gte=0)
                    & Q(electronic_gb_rate__gte=0)
                ),
                name="records_fee_schedule_rates_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(flat_fee_amount__isnull=True)
                    | (
                        Q(flat_fee_amount__gt=0)
                        & Q(flat_fee_amount__lte=Decimal("2.00"))
                    )
                ),
                name="records_fee_schedule_flat_fee_cap",
            ),
        ]


class FeeAssessment(AppendOnlyModelMixin, models.Model):
    """Append-only calculation snapshot projected onto one installment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installment = models.ForeignKey(
        Installment,
        on_delete=models.PROTECT,
        related_name="fee_assessments",
    )
    schedule = models.ForeignKey(
        FeeSchedule,
        on_delete=models.PROTECT,
        related_name="assessments",
    )
    revision = models.PositiveIntegerField()
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    photocopy_pages = models.PositiveIntegerField(default=0)
    scanned_pages = models.PositiveIntegerField(default=0)
    electronic_files = models.PositiveIntegerField(default=0)
    electronic_gb = models.DecimalField(
        max_digits=16,
        decimal_places=6,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    media_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    postage_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    customized_service_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    customized_service_explanation = models.TextField(blank=True)
    customized_service_expertise = models.CharField(max_length=500, blank=True)
    requester_notice_reference = models.CharField(max_length=500, blank=True)
    requester_approval_reference = models.CharField(max_length=500, blank=True)
    itemized_amount = models.DecimalField(max_digits=10, decimal_places=2)
    use_flat_fee = models.BooleanField(default=False)
    covered_by_request_flat_fee = models.BooleanField(default=False)
    waiver_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    waiver_reason = models.CharField(max_length=1000, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[
            MinValueValidator(Decimal(0)),
            MaxValueValidator(Decimal(10)),
        ],
    )
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=1000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_fee_assessments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["installment_id", "revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["installment", "revision"],
                name="records_fee_assessment_revision_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(itemized_amount__gte=0)
                    & Q(waiver_amount__gte=0)
                    & Q(amount__gte=0)
                    & Q(deposit_percent__gte=0)
                    & Q(deposit_percent__lte=10)
                    & Q(deposit_amount__gte=0)
                ),
                name="records_fee_assessment_amounts_valid",
            ),
            models.CheckConstraint(
                condition=~(
                    Q(use_flat_fee=True)
                    & Q(covered_by_request_flat_fee=True)
                ),
                name="records_fee_assessment_flat_modes_distinct",
            ),
        ]


class FeePayment(AppendOnlyModelMixin, models.Model):
    """Append-only receipt evidence for a fee assessment."""

    class Method(models.TextChoices):
        CHECK = "check", "Check"
        CASH = "cash", "Cash"
        CARD = "card", "District payment system"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installment = models.ForeignKey(
        Installment,
        on_delete=models.PROTECT,
        related_name="fee_payments",
    )
    assessment = models.ForeignKey(
        FeeAssessment,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    reference = models.CharField(max_length=500)
    received_on = models.DateField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_fee_payments",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["received_on", "recorded_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="records_fee_payment_amount_positive",
            ),
        ]


class FeeWaiverFloor(AppendOnlyModelMixin, models.Model):
    """The adopted amount below which an officer is prompted to waive a fee.

    RCW 42.56.120(2) lets an agency waive a copying charge; WAC 44-14-07003
    observes that collecting a charge smaller than the cost of collecting it
    serves nobody. The district adopts that number with counsel, exactly as it
    adopts the fee schedule, and the number is recorded here rather than in a
    setting so the counsel reference travels with it.

    The floor never waives anything. It only tells the officer that an
    assessment fell under the adopted amount; the waiver itself stays a named
    human decision recorded on a fee assessment.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Assessments strictly below this amount prompt a waiver.",
    )
    counsel_reference = models.CharField(
        max_length=500,
        help_text="The counsel approval that adopted this floor.",
    )
    effective_on = models.DateField(db_index=True)
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activated_fee_waiver_floors",
    )
    activated_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["-effective_on", "-activated_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="records_fee_waiver_floor_amount_positive",
            ),
        ]


class FeeDepositGate(models.Model):
    """A RCW 42.56.120(2) deposit the District requires before it works on.

    A deposit gate is the officer's decision to stop a specific kind of work
    on a case until the requester pays. ``scope`` says which work: collection
    (searching for and exporting the records) or production (packaging and
    releasing them). While a gate is open the gated work refuses to run.

    Nothing in this model closes a case, sends a message, or expires. A gate
    ends one of two ways, both with a named person on them: the deposit is
    recorded as paid, or the officer lifts the gate with a written reason.
    """

    class Scope(models.TextChoices):
        COLLECTION = "collection", "Collection"
        PRODUCTION = "production", "Production"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        "PublicRecordsRequest",
        on_delete=models.PROTECT,
        related_name="fee_deposit_gates",
    )
    scope = models.CharField(max_length=20, choices=Scope.choices)
    schedule = models.ForeignKey(
        FeeSchedule,
        on_delete=models.PROTECT,
        related_name="deposit_gates",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    estimated_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="The estimated charge the deposit is ten percent of.",
    )
    reason = models.CharField(max_length=1000)
    requested_on = models.DateField()
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="applied_fee_deposit_gates",
    )
    applied_at = models.DateTimeField(auto_now_add=True)

    satisfied_at = models.DateTimeField(null=True, blank=True)
    satisfied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="satisfied_fee_deposit_gates",
    )
    payment_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    payment_method = models.CharField(max_length=20, blank=True)
    payment_reference = models.CharField(max_length=500, blank=True)
    payment_received_on = models.DateField(null=True, blank=True)

    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lifted_fee_deposit_gates",
    )
    lift_reason = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ["-applied_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0) & Q(estimated_total__gt=0),
                name="records_fee_deposit_gate_amounts_positive",
            ),
            # RCW 42.56.120(2) caps a deposit at ten percent of the estimated
            # charge. The cap is enforced in the database as well as the
            # service so no later code path can widen it.
            models.CheckConstraint(
                condition=Q(amount__lte=F("estimated_total") * Decimal("0.1")),
                name="records_fee_deposit_gate_within_ten_percent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        satisfied_at__isnull=True,
                        satisfied_by__isnull=True,
                        payment_amount__isnull=True,
                        payment_method="",
                        payment_reference="",
                        payment_received_on__isnull=True,
                    )
                    | (
                        Q(
                            satisfied_at__isnull=False,
                            satisfied_by__isnull=False,
                            payment_amount__isnull=False,
                            payment_received_on__isnull=False,
                        )
                        & ~Q(payment_method="")
                        & ~Q(payment_reference="")
                    )
                ),
                name="records_fee_deposit_gate_payment_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(lifted_at__isnull=True, lifted_by__isnull=True, lift_reason="")
                    | (
                        Q(lifted_at__isnull=False, lifted_by__isnull=False)
                        & ~Q(lift_reason="")
                    )
                ),
                name="records_fee_deposit_gate_lift_complete",
            ),
            # A gate is either open, paid, or lifted — never paid and lifted.
            models.CheckConstraint(
                condition=(
                    Q(satisfied_at__isnull=True) | Q(lifted_at__isnull=True)
                ),
                name="records_fee_deposit_gate_single_resolution",
            ),
            # One open gate per scope. A second gate on the same work would
            # leave it ambiguous which deposit unblocks it.
            models.UniqueConstraint(
                fields=["request", "scope"],
                condition=Q(satisfied_at__isnull=True, lifted_at__isnull=True),
                name="records_fee_deposit_gate_one_open_per_scope",
            ),
        ]

    def __str__(self):  # pragma: no cover - admin convenience
        return f"{self.request_id} {self.scope} deposit {self.amount}"

    @property
    def is_open(self) -> bool:
        return self.satisfied_at is None and self.lifted_at is None


class JlarcRequestMetrics(AppendOnlyModelMixin, models.Model):
    """Optional, voluntarily known annual-report classification evidence."""

    class RequesterType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        LAW_FIRM = "law_firm", "Law firm"
        ORGANIZATION = "organization", "Organization / business"
        INSURER = "insurer", "Insurer"
        GOVERNMENT = "government", "Government"
        INCARCERATED = "incarcerated", "Incarcerated person"
        MEDIA = "media", "Media"
        EMPLOYEE = "employee", "Current or former employee"
        OTHER = "other", "Other"
        UNKNOWN = "unknown", "Unknown / anonymous"

    class FulfillmentFormat(models.TextChoices):
        ELECTRONIC = "electronic", "Entirely electronic"
        PHYSICAL = "physical", "Entirely physical"
        BOTH = "both", "Electronic and physical"
        NO_RECORDS_OR_OTHER = (
            "no_records_or_other",
            "No responsive records or otherwise closed without fulfillment",
        )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="jlarc_metric_snapshots",
    )
    revision = models.PositiveIntegerField()
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    requester_type = models.CharField(
        max_length=30,
        choices=RequesterType.choices,
        default=RequesterType.UNKNOWN,
    )
    fulfillment_format = models.CharField(
        max_length=30,
        choices=FulfillmentFormat.choices,
    )
    scanned_physical = models.BooleanField(default=False)
    records_produced_description = models.TextField(blank=True)
    records_withheld_description = models.TextField(blank=True)
    reason = models.CharField(max_length=1000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_jlarc_request_metrics",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["request_id", "revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "revision"],
                name="records_jlarc_request_metrics_revision_unique",
            ),
        ]


class JlarcAnnualSupplement(AppendOnlyModelMixin, models.Model):
    """Finance, risk, and unallocated-time inputs not derivable per request."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporting_year = models.PositiveIntegerField(db_index=True)
    revision = models.PositiveIntegerField()
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    unallocated_staff_minutes = models.PositiveBigIntegerField(default=0)
    staff_compensation_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    legal_review_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    supplies_services_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    claims_filed = models.PositiveIntegerField(default=0)
    claims_by_type = models.JSONField(default=dict, blank=True)
    claims_by_exemption = models.JSONField(default=dict, blank=True)
    litigation_agency_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    litigation_settlements = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    litigation_penalties = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    litigation_requester_attorney_fees = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    litigation_requester_costs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    records_management_staff_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    records_management_systems_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    records_management_other_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_expenses_recovered = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    customized_service_expenses_recovered_external = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
    )
    methodology_notes = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_jlarc_annual_supplements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["reporting_year", "revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["reporting_year", "revision"],
                name="records_jlarc_annual_supplement_revision_unique",
            ),
            models.CheckConstraint(
                condition=Q(reporting_year__gte=2017),
                name="records_jlarc_reporting_year_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(staff_compensation_cost__gte=0)
                    & Q(legal_review_cost__gte=0)
                    & Q(supplies_services_cost__gte=0)
                    & Q(litigation_agency_cost__gte=0)
                    & Q(litigation_settlements__gte=0)
                    & Q(litigation_penalties__gte=0)
                    & Q(litigation_requester_attorney_fees__gte=0)
                    & Q(litigation_requester_costs__gte=0)
                    & Q(records_management_staff_cost__gte=0)
                    & Q(records_management_systems_cost__gte=0)
                    & Q(records_management_other_cost__gte=0)
                    & Q(other_expenses_recovered__gte=0)
                    & Q(customized_service_expenses_recovered_external__gte=0)
                ),
                name="records_jlarc_annual_costs_nonnegative",
            ),
        ]


class LegacyImportBatch(AppendOnlyModelMixin, models.Model):
    """Immutable proof that one exact, reviewed legacy plan was committed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=50, default="nextrequest")
    source_sha256 = models.CharField(max_length=64)
    mapping_sha256 = models.CharField(max_length=64)
    plan_sha256 = models.CharField(max_length=64, unique=True)
    row_count = models.PositiveIntegerField()
    mapping_snapshot = models.JSONField()
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="legacy_import_batches",
    )
    imported_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["imported_at"]


class LegacyRequestImport(AppendOnlyModelMixin, models.Model):
    """Immutable provenance linking one source row to one legal request."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        LegacyImportBatch,
        on_delete=models.PROTECT,
        related_name="request_imports",
    )
    request = models.OneToOneField(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="legacy_import",
    )
    source_id = models.CharField(max_length=255, unique=True)
    source_number = models.CharField(max_length=255, blank=True)
    source_status = models.CharField(max_length=255)
    source_row_sha256 = models.CharField(max_length=64)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    legacy_initial_response_at = models.DateTimeField(null=True, blank=True)
    legacy_estimated_completion = models.DateField(null=True, blank=True)
    legacy_closed_at = models.DateTimeField(null=True, blank=True)
    closure_reason = models.CharField(max_length=1000, blank=True)
    #: How many files the source row reported, retained as evidence even when
    #: the files themselves stayed in the source system. A nonzero value here
    #: is only reachable through an explicit, reasoned
    #: ``attachments_retained_in_source`` acknowledgement in the mapping; the
    #: importer otherwise blocks the row. It must never be read as "these files
    #: were migrated" - it means the opposite.
    source_attachment_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["batch_id", "source_id"]


class InitialResponseAttestation(AppendOnlyModelMixin, models.Model):
    """A Records Officer's written statement that the RCW 42.56.520 first
    response reached the requester through a channel this system did not
    deliver: the retired NextRequest tenant, a district mailbox, paper.

    This is not a delivery record and never claims to be one. It carries who
    said so, which channel, the date when one is known, and what they relied
    on. ``responded_on`` is null when the officer can only state that the
    response preceded a cutover; the statement is still theirs to defend.
    Written only by ``services.record_external_initial_response()``.
    """

    class SentVia(models.TextChoices):
        NEXTREQUEST = "nextrequest", "NextRequest (retired system)"
        DISTRICT_EMAIL = "district_email", "District email outside this system"
        MAIL = "mail", "Paper letter or mail"
        OTHER = "other", "Other channel"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="initial_response_attestation",
    )
    sent_via = models.CharField(max_length=30, choices=SentVia.choices)
    responded_on = models.DateField(null=True, blank=True)
    evidence = models.CharField(max_length=2000)
    attested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="initial_response_attestations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    sent_via__in=["nextrequest", "district_email", "mail", "other"]
                ),
                name="records_initial_response_channel_valid",
            ),
            models.CheckConstraint(
                condition=~Q(evidence=""),
                name="records_initial_response_evidence_present",
            ),
        ]


class FerpaIdentityVerification(AppendOnlyModelMixin, models.Model):
    """A Records Officer's statement that a FERPA requester is the parent or
    eligible student they claim to be (34 CFR 99.10).

    Nothing is disclosed on a ``ferpa_parent`` matter until one exists, so it
    is kept the way ``InitialResponseAttestation`` is: its own append-only row,
    one per request, that neither the ORM nor the database lets anyone rewrite
    or erase. Written only by ``services.record_ferpa_identity_verification()``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="ferpa_identity_verification",
    )
    method = models.CharField(
        max_length=40, choices=FerpaIdentityVerificationMethod.choices
    )
    note = models.TextField()
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ferpa_identity_verifications",
    )
    verified_at = models.DateTimeField(default=timezone.now)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["verified_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(method__in=FerpaIdentityVerificationMethod.values),
                name="records_ferpa_identity_method_valid",
            ),
            models.CheckConstraint(
                condition=~Q(note=""),
                name="records_ferpa_identity_note_present",
            ),
        ]


#: Longest internal note the API accepts. Generous enough for a phone-call
#: write-up or a custodian's answer pasted in whole; bounded so one note
#: cannot become an unreviewable wall of text in a list cell.
CASE_NOTE_MAX_LENGTH = 10_000


class CaseNote(AppendOnlyModelMixin, models.Model):
    """One staff member's internal note on a case, in their own name.

    Staff working a case need somewhere to write down what they did and what
    they were told: who they called, what the custodian said, why they are
    waiting. Today that goes in email, in a spreadsheet, or nowhere, which
    means the next person to open the case cannot see it.

    It is **internal**. A note is never rendered into requester
    correspondence, a letter, a production manifest, or an export;
    ``test_case_notes`` asserts that against the real drafting and production
    paths rather than trusting this docstring.

    Internal is not the same as secret. A note about a public records request
    is very likely itself a public record, disclosable on a request for it,
    and staff should write it that way. What this model guarantees is that it
    is never sent to *this* case's requester as part of *this* case's
    response, which is a different question from whether it could be
    disclosed later.

    Append-only, like ``StatusEvent`` and ``AuditLog``: a note is evidence of
    what somebody knew and when. Correcting one means writing another that
    says so, which is also how a paper case file works. It applies to every
    matter kind (ADR 0004) -- a litigation pull and a FERPA request are worked
    by the same people and need the same running record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="case_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="case_notes",
    )
    body = models.TextField(max_length=CASE_NOTE_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        # Oldest first, the order a case file reads in. The API reverses it
        # for the list, where the newest note is the one being looked for.
        ordering = ["created_at", "pk"]
        indexes = [
            # The case page reads one case's notes, and the case list reads
            # the latest note per case. Both walk this index.
            models.Index(fields=["request", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(body=""),
                name="records_case_note_body_present",
            ),
        ]

    def __str__(self):
        return f"{self.request_id} note {self.pk}"


class LegacyRequestActivation(AppendOnlyModelMixin, models.Model):
    """Proof that an imported open request received explicit live triage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_import = models.OneToOneField(
        LegacyRequestImport,
        on_delete=models.PROTECT,
        related_name="activation",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="legacy_request_activations",
    )
    footage_preservation_created = models.BooleanField(default=False)
    collection_tasks_created = models.PositiveIntegerField(default=0)
    activated_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["activated_at"]


class TimeEntry(models.Model):
    """Staff time per request — feeds JLARC annual reporting (RCW 40.14.026)."""
    request = models.ForeignKey(PublicRecordsRequest, on_delete=models.CASCADE, related_name="time_entries")
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    date = models.DateField(default=timezone.localdate)
    minutes = models.PositiveIntegerField()
    activity = models.CharField(max_length=200, blank=True)


class EstimateRevision(models.Model):
    """Every change to estimated_completion, with a REQUIRED reason.
    This table is the district's good-faith record: if a request runs long,
    the revisions show *why* (volume, third-party notice, staff capacity),
    which is what RCW 42.56.550 penalty analysis actually examines."""
    request = models.ForeignKey(PublicRecordsRequest, on_delete=models.CASCADE, related_name="estimate_revisions")
    previous_date = models.DateField(null=True, blank=True)
    new_date = models.DateField()
    reason = models.CharField(max_length=1000)          # enforced non-empty in services
    revised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    automated = models.BooleanField(default=False)      # True only for the initial safety-net estimate
    created_at = models.DateTimeField(auto_now_add=True)
    communicated_at = models.DateTimeField(null=True, blank=True)
    correspondence = models.OneToOneField(
        "correspondence.Correspondence",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="communicated_estimate_revision",
    )

    class Meta:
        ordering = ["created_at"]


class FootagePreservation(models.Model):
    """Preservation hold for a camera-footage request. Philosophy:
    HOLD BROAD IMMEDIATELY (every camera, full window) the moment intake
    detects a footage request inside the retention window — destruction of
    responsive records after receipt violates RCW 42.56.100. Humans then
    NARROW by excluding irrelevant cameras (reason required)."""
    class State(models.TextChoices):
        PENDING = "pending", "Pending — hold executing"
        HELD = "held", "Held"
        PARTIAL = "partial", "Held (some cameras failed)"
        MANUAL = "manual", "Manual hold required (API unavailable)"
        EXPIRED = (
            "expired",
            "Window outside retention — adequate-search review required",
        )
        RELEASED = "released", "Released"

    request = models.OneToOneField(PublicRecordsRequest, on_delete=models.CASCADE, related_name="footage_preservation")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    detected_dates_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def held_count(self):
        return self.camera_holds.filter(state=CameraHold.State.HELD, excluded_at__isnull=True).count()


class CameraHold(models.Model):
    """One camera's clip within a preservation hold. Exclusion requires a
    human + reason (mirrors the estimate-revision gate)."""
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        HELD = "held", "Held (clip saved)"
        FAILED = "failed", "Failed"
        RELEASED = "released", "Released"

    preservation = models.ForeignKey(FootagePreservation, on_delete=models.CASCADE, related_name="camera_holds")
    camera_id = models.CharField(max_length=100)
    camera_name = models.CharField(max_length=255, blank=True)
    site = models.CharField(max_length=255, blank=True)
    clip_id = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    error = models.CharField(max_length=500, blank=True)
    excluded_at = models.DateTimeField(null=True, blank=True)
    excluded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    exclusion_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        unique_together = [("preservation", "camera_id")]


class RecordSystem(models.Model):
    """District system registry — every place responsive records can live.
    Pre-seeded from research (manage.py seed_systems); the district fills in
    custodian assignments and verifies retention windows. Triage keywords
    auto-route intake to per-system collection tasks."""
    class Integration(models.TextChoices):
        VAULT = "vault", "Google Vault (automated)"
        AVIGILON = "avigilon", "Avigilon Alta (automated)"
        MANUAL = "manual", "Manual collection"

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, blank=True)     # SIS, LMS, comms...
    vendor = models.CharField(max_length=200, blank=True)
    integration = models.CharField(max_length=20, choices=Integration.choices,
                                   default=Integration.MANUAL)
    # WHO — assign a job title now, a person/email when known.
    custodian_title = models.CharField(max_length=200, blank=True)
    custodian_email = models.EmailField(blank=True)
    default_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_record_systems",
        help_text="Staff owner automatically assigned to new collection tasks.",
    )
    # RETENTION — None means "not retention-limited / unknown".
    retention_days = models.PositiveIntegerField(null=True, blank=True)
    retention_note = models.CharField(max_length=500, blank=True)
    # Retention-race systems: a matching request fires an URGENT preservation
    # email immediately (the bus-DVR problem).
    urgent_hold = models.BooleanField(default=False)
    # Substring/phrase keywords that route intake to this system (lowercase).
    triage_keywords = models.JSONField(default=list, blank=True)
    # The adequate-search checklist that becomes the CollectionTask instructions.
    search_checklist = models.TextField(blank=True)
    needs_verification = models.BooleanField(
        default=True, help_text="Cleared once the district confirms vendor/retention/custodian.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def matches(self, description: str) -> bool:
        import re
        text = description.lower()
        for kw in self.triage_keywords:
            kw = kw.lower()
            if " " in kw:
                if kw in text:
                    return True
            elif re.search(rf"\b{re.escape(kw)}(?:s|es)?\b", text):
                return True
        return False

    def __str__(self):
        return self.name


# District knowledge pack (#35). What a Records Officer knows about where
# records live, kept in the app and edited only from the Knowledge page, so the
# request-intent advisory scopes a search the way the officer would. Nothing
# here routes, holds, collects, or reaches a requester; it is advisory input.


class KnowledgeRoleAccount(models.Model):
    """Which district account held a role, and when.

    A request names "the principal" or "the HR director"; the mailbox to search
    is whoever held that role during the dates the request reaches. Either end
    of the range may be open.
    """

    role = models.CharField(max_length=200)
    account_email = models.EmailField()
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "starts_on", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(starts_on__isnull=True)
                    | models.Q(ends_on__isnull=True)
                    | models.Q(ends_on__gte=models.F("starts_on"))
                ),
                name="knowledge_role_account_range_ordered",
            ),
        ]

    def __str__(self):
        return f"{self.role}: {self.account_email}"


class KnowledgeNoiseSender(models.Model):
    """A sender a responsive search almost never wants: district mass mail,
    newsletters, vendor notifications. The advisory is told to exclude it."""

    class Kind(models.TextChoices):
        DOMAIN = "domain", "Sender domain"
        ADDRESS = "address", "Sender address"
        LIST = "list", "Mailing list"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    value = models.CharField(max_length=254)
    note = models.CharField(max_length=500, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "value"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "value"],
                name="knowledge_noise_sender_unique",
            ),
        ]

    def __str__(self):
        return f"{self.kind}: {self.value}"


class KnowledgeSystemNote(models.Model):
    """The officer's own search notes for one registry system. Kept apart from
    RecordSystem so re-seeding the registry never touches them."""

    record_system = models.OneToOneField(
        RecordSystem,
        on_delete=models.PROTECT,
        related_name="knowledge_note",
    )
    notes = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["record_system__name"]

    def __str__(self):
        return f"Search notes: {self.record_system}"


class LegacyFileImport(AppendOnlyModelMixin, models.Model):
    """One migrated source file, and what became of it.

    The CSV row importer deliberately moves no files, so this is the only
    record tying a NextRequest document to the object this application now
    holds. It exists per *extracted item*, not per source document: an archive
    expands into many rows, and several source documents that turned out to be
    byte-identical collapse onto one stored item. Both of those facts are
    evidence about a public record, so they are written down rather than
    inferred later from filenames.

    ``LegacyRequestImport.source_attachment_count`` is NOT updated by this
    import and must never be. It counts what the source system held at export
    time, which stays true no matter what is migrated afterwards.
    """

    class Outcome(models.TextChoices):
        CREATED = "created", "Stored as a new item"
        DUPLICATE = "duplicate", "Byte-identical to an item already stored"
        SKIPPED = "skipped", "Carried no content to store"
        INFECTED = "infected", "Rejected by malware scanning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.CASCADE,
        related_name="legacy_file_imports",
    )
    source_document_id = models.CharField(max_length=255)
    source_title = models.CharField(max_length=500)
    #: Empty for a file migrated as-is; otherwise the path inside the archive.
    member_path = models.CharField(max_length=1000, blank=True)
    #: What the source system showed the requester. Recorded as provenance
    #: only: a file released by NextRequest still arrives here unreviewed,
    #: because this application's review is what governs release from now on.
    source_visibility = models.CharField(max_length=50, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField(default=0)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    record_item = models.ForeignKey(
        RecordItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_file_imports",
    )
    intake_attachment = models.ForeignKey(
        IntakeAttachment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_file_imports",
    )
    detail = models.CharField(max_length=500, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_document_id", "member_path"],
                name="records_legacy_file_source_member_unique",
            ),
        ]
        ordering = ["request_id", "source_document_id", "member_path"]

    def __str__(self):
        return f"{self.source_document_id}:{self.member_path or self.source_title}"


class MediaRedaction(models.Model):
    """A redaction over a span of time in audio or video.

    Deliberately a separate model from ``Redaction`` rather than a nullable
    page number bolted onto it. The two describe different things: a page
    redaction covers a rectangle on a fixed page, while this covers an
    interval, optionally within a region of the frame, and has to be applied
    by re-encoding rather than by drawing.

    The same non-negotiable holds as for paper: ``exemption`` is not nullable,
    because the RCW 42.56.210(3) log is generated from these rows and a
    district cannot log a withholding it never cited.

    Video is not a corner case in this system. The camera-footage triage
    exists precisely because bus and building recordings are requested, and
    FERPA means an uninvolved student's face is exempt even when the incident
    is disclosable. That is a blur across frames, not a box on a page.
    """

    class Treatment(models.TextChoices):
        #: Cover a region of the frame for the whole interval.
        OBSCURE_REGION = "obscure_region", "Obscure a region of the frame"
        #: Blank the picture entirely for the interval.
        OBSCURE_FRAME = "obscure_frame", "Obscure the whole frame"
        #: Silence the audio for the interval, picture untouched.
        MUTE_AUDIO = "mute_audio", "Mute the audio"
        #: Remove the interval from the released copy.
        CUT = "cut", "Remove the interval entirely"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record_item = models.ForeignKey(
        RecordItem,
        on_delete=models.CASCADE,
        related_name="media_redactions",
    )
    #: Seconds from the start of the recording. Float because frame-accurate
    #: boundaries rarely land on whole seconds.
    starts_at = models.FloatField(validators=[MinValueValidator(0.0)])
    ends_at = models.FloatField(validators=[MinValueValidator(0.0)])
    treatment = models.CharField(
        max_length=20,
        choices=Treatment.choices,
        default=Treatment.OBSCURE_REGION,
    )
    #: Normalized frame coordinates, as for page redactions, so they survive
    #: any output resolution. Only meaningful for OBSCURE_REGION.
    x0 = models.FloatField(null=True, blank=True)
    y0 = models.FloatField(null=True, blank=True)
    x1 = models.FloatField(null=True, blank=True)
    y1 = models.FloatField(null=True, blank=True)
    exemption = models.ForeignKey(ExemptionCitation, on_delete=models.PROTECT)
    explanation = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_media_redactions",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["record_item_id", "starts_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="records_media_redaction_interval_ordered",
            ),
            models.CheckConstraint(
                condition=Q(starts_at__gte=0.0),
                name="records_media_redaction_starts_non_negative",
            ),
            # A region treatment without a region would silently obscure
            # nothing, which reads afterwards as a redaction that was applied.
            models.CheckConstraint(
                condition=(
                    ~Q(treatment="obscure_region")
                    | (
                        Q(x0__isnull=False)
                        & Q(y0__isnull=False)
                        & Q(x1__isnull=False)
                        & Q(y1__isnull=False)
                    )
                ),
                name="records_media_redaction_region_requires_box",
            ),
            models.CheckConstraint(
                condition=(
                    Q(x0__isnull=True)
                    | (
                        Q(x0__gte=0.0) & Q(x0__lte=1.0)
                        & Q(y0__gte=0.0) & Q(y0__lte=1.0)
                        & Q(x1__gt=models.F("x0")) & Q(x1__lte=1.0)
                        & Q(y1__gt=models.F("y0")) & Q(y1__lte=1.0)
                    )
                ),
                name="records_media_redaction_box_normalized",
            ),
        ]

    def __str__(self):
        return f"{self.treatment} {self.starts_at:.2f}-{self.ends_at:.2f}"


class OpaqueRecordDisposition(AppendOnlyModelMixin, models.Model):
    """How a record that no renderer can open was decided, and on what basis.

    Some records genuinely cannot be displayed: a dead format, a proprietary
    container, a corrupt file. The PRA still requires a decision, and the
    district still has to be able to explain it later.

    Without this, the only options were to leave such a record permanently
    unreviewed, or to mark it reviewed and leave no trace of the fact that
    nobody could open it. The second is worse: it produces a record that looks
    reviewed on paper while the reviewer certified something they never saw.
    """

    class Basis(models.TextChoices):
        OPENED_ELSEWHERE = (
            "opened_elsewhere",
            "Opened outside the application and inspected",
        )
        DESCRIBED_BY_CUSTODIAN = (
            "described_by_custodian",
            "Described by the custodian who holds it",
        )
        NOT_INSPECTABLE = (
            "not_inspectable",
            "Could not be inspected by any available means",
        )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record_item = models.OneToOneField(
        RecordItem,
        on_delete=models.CASCADE,
        related_name="opaque_disposition",
    )
    basis = models.CharField(max_length=30, choices=Basis.choices)
    #: What the reviewer actually did and what they concluded. Long minimum
    #: enforced in the service layer, not here, so the message can explain.
    reasoning = models.TextField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    decided_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    def __str__(self):
        return f"{self.record_item_id}: {self.basis}"


class CollectionSupersession(AppendOnlyModelMixin, models.Model):
    """A Records Officer took one collection's unreviewed records out of review.

    An export can sweep in far more than the request asked for. The records it
    brought in stay -- they are the evidence of what was searched -- but the
    ones nobody has decided yet leave the review and release queues, and this
    row says who did that, why, and how many.
    """

    collection_task = models.ForeignKey(
        CollectionTask,
        on_delete=models.PROTECT,
        related_name="supersessions",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    reason = models.TextField()
    record_count = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    def __str__(self):
        return f"{self.collection_task.pk}: {self.record_count} superseded"


class AdvisoryFeedback(AppendOnlyModelMixin, models.Model):
    """What a person did with one advisory proposal.

    One row per run. ``confirmed`` means the person agreed with the proposal;
    applying it is still the ordinary staff action (merge, close, add to a
    case), which this row does not record and the proposal never performs. ``dismissed`` means they did not
    agree. ``final_label`` is what the person judged it to be, so agreement
    between model and people can be measured and thresholds set from data.
    """

    class Outcome(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        DISMISSED = "dismissed", "Dismissed"

    advisory_run = models.OneToOneField(
        AdvisoryRun,
        on_delete=models.PROTECT,
        related_name="feedback",
    )
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    final_label = models.CharField(max_length=40, blank=True)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    def __str__(self):
        return f"{self.advisory_run.pk}: {self.outcome}"


class ReviewDecision(AppendOnlyModelMixin, models.Model):
    """One human responsiveness decision on one record, and the AI suggestion
    standing when it was made.

    Written by ``review.review_record`` for single and bulk review alike. The
    suggestion is the record's latest complete responsiveness run at decision
    time -- the rule the review list uses to show one -- copied here as label
    and confidence so
    agreement between model and people can be measured. It never influences
    what the decision writes. Append-only: a later review is a new row.
    """

    record_item = models.ForeignKey(
        RecordItem,
        on_delete=models.PROTECT,
        related_name="review_decisions",
    )
    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="+",
    )
    review_state = models.CharField(max_length=20, choices=RecordItem.ReviewState.choices)
    advisory_run = models.ForeignKey(
        AdvisoryRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    advisory_label = models.CharField(max_length=40, blank=True)
    advisory_confidence = models.FloatField(null=True, blank=True)
    bulk = models.BooleanField(default=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        indexes = [models.Index(fields=["request", "record_item", "created_at"])]

    def __str__(self):
        return f"{self.pk}: {self.review_state}"


class RecordsEstimate(AppendOnlyModelMixin, models.Model):
    """One computed estimate for a request (#41): responsive range, fee, and
    completion date, with every input it was computed from.

    Evidence only. Nothing here changes ``estimated_completion`` or reaches
    the requester; a person revises the estimate (``letters.revise_estimate``)
    and approves any letter. ``inputs`` holds the counts, rates, adopted
    schedule, throughput window, and backlog the numbers came from, so a later
    revision can show exactly why the estimate said what it did.
    """

    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="records_estimates",
    )
    as_of = models.DateField()
    inputs = models.JSONField()
    result = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.pk}: estimate as of {self.as_of}"


class ReviewBatch(models.Model):
    """A batch decision a reviewer approves after judging a random sample (#39).

    ``cluster``: a reviewer proposes one outcome for a cluster's unreviewed
    records; ``discard``: the Ready to discard pile of records the AI calls
    non-responsive above the trust-policy floor. Either way a random sample
    (``sample_ids``) is drawn when the batch is opened, the reviewer judges
    each sampled record, and only if every judgment matches
    ``proposed_state`` is that outcome applied to the rest, in the
    reviewer's name. One disagreement blocks the batch: the sampled
    judgments are recorded as ordinary reviews and every other record goes
    back to per-record review. For a discard batch that sample is the
    elusion test. The AI applies nothing; the reviewer's decision does.
    """

    class Kind(models.TextChoices):
        CLUSTER = "cluster", "Cluster approval"
        DISCARD = "discard", "Ready to discard"

    class Outcome(models.TextChoices):
        OPEN = "open", "Open"
        APPLIED = "applied", "Applied"
        BLOCKED = "blocked", "Blocked"

    request = models.ForeignKey(
        PublicRecordsRequest,
        on_delete=models.PROTECT,
        related_name="review_batches",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    label = models.CharField(max_length=500, blank=True)
    proposed_state = models.CharField(max_length=20)
    record_item_ids = models.JSONField()
    sample_ids = models.JSONField()
    min_confidence = models.FloatField(null=True, blank=True)
    judgments = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.OPEN)
    applied_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(proposed_state__in=["responsive", "non_responsive"]),
                name="review_batch_proposed_state_allowed",
            ),
            # Nothing may discard as responsive: a discard batch only ever
            # proposes non-responsive.
            models.CheckConstraint(
                condition=~Q(kind="discard") | Q(proposed_state="non_responsive"),
                name="review_batch_discard_is_non_responsive",
            ),
        ]

    def __str__(self):
        return f"{self.kind} batch {self.pk}: {self.outcome}"


class EvaluationSet(models.Model):
    """Labelled examples for comparing advisory models, taken from decisions
    people already made (a merge, a non-request closure, a reviewed record).

    ``examples`` holds references and labels only -- request and record IDs,
    a coarse label, and a target case number -- never request or record
    text; the input is rebuilt from the live rows when a run sends it. See
    ``apps/records/evaluation.py``.
    """

    class Kind(models.TextChoices):
        INTAKE = "intake", "Intake classification"
        RESPONSIVENESS = "responsiveness", "Record responsiveness"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    examples = models.JSONField(default=list)
    label_counts = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.kind} set {self.pk} ({len(self.examples)} examples)"


class EvaluationRun(models.Model):
    """Every candidate model scored on one evaluation set.

    ``results`` maps each model identifier to its metrics: accuracy, a
    confusion matrix, the safety recall (real requests and responsive records
    not lost), token use, cost when a price was given, and latency. Per-item
    predictions are stored as labels only, so a disagreement can be traced to
    its case or record without copying text.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation_set = models.ForeignKey(
        EvaluationSet, on_delete=models.PROTECT, related_name="runs",
    )
    model_identifiers = models.JSONField(default=list)
    results = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"evaluation run {self.pk}"
