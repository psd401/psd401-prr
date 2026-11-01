"""Django admin doubles as the Phase 1 staff dashboard — free UI while the
real staff app gets built."""
from django.contrib import admin

from .models import (
    CollectionTask,
    EstimateRevision,
    ExemptionCitation,
    Installment,
    IntakeAttachment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    StatusEvent,
    TimeEntry,
)


class StatusEventInline(admin.TabularInline):
    model = StatusEvent
    extra = 0
    # Append-only per rule 4 — this is the compliance timeline, not an
    # editable log. Entries are written by services.transition(); staff
    # never hand-edit or backdate one.
    readonly_fields = ("occurred_at", "actor", "from_status", "to_status",
                       "note", "visible_to_requester",
                       "correspondence_effect")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ReadOnlyEvidenceAdmin(admin.ModelAdmin):
    """Display legal review/production artifacts without mutation paths."""

    def get_readonly_fields(self, request, obj=None):
        return tuple(
            field.name
            for field in self.model._meta.get_fields()
            if field.concrete and not field.auto_created
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EstimateRevisionInline(admin.TabularInline):
    model = EstimateRevision
    extra = 0
    readonly_fields = ("created_at", "previous_date", "new_date", "reason",
                       "revised_by", "automated", "communicated_at",
                       "correspondence")
    can_delete = False

    # Revisions only materialize after a successful durable correspondence
    # delivery (rule 6); without this Django still renders a dead "Add another"
    # link.
    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PublicRecordsRequest)
class RequestAdmin(admin.ModelAdmin):
    # View-only: number/status/dates/description are compliance fields that
    # may only change through services.transition() (rule 2) or the successful
    # delivery effects of a human-approved estimate-revision correspondence
    # (rule 6). Staff use the SPA/API correspondence lifecycle, not this admin.
    # Add/delete are blocked too: an admin-created request skips intake triage
    # and deadline calc, and deleting one cascades away its StatusEvents and
    # RecordItems, the compliance record itself (rule 4).
    list_display = ("number", "status", "received_at", "statutory_response_due",
                    "estimated_completion", "revision_count",
                    "assigned_officer", "acknowledgment_overdue")
    list_filter = ("status", "channel")
    search_fields = ("number", "description", "requester__email")
    inlines = [StatusEventInline, EstimateRevisionInline]

    @admin.display(description="Est. revisions")
    def revision_count(self, obj):
        return obj.estimate_revisions.count()
    readonly_fields = ("number", "received_at", "status", "description",
                       "statutory_response_due", "estimated_completion",
                       "tolled_business_days", "acknowledgment_sent_at",
                       "closed_at", "assigned_officer")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IntakeAttachment)
class IntakeAttachmentAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename", "request", "size", "scan_status", "created_at",
        "scanned_at",
    )
    list_filter = ("scan_status", "content_type")
    search_fields = ("original_filename", "request__number", "sha256")
    readonly_fields = (
        "request", "original_filename", "content_type", "size", "sha256",
        "storage_key", "scan_status", "scan_detail", "created_at", "scanned_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExemptionCitation)
class ExemptionAdmin(admin.ModelAdmin):
    list_display = ("citation", "short_name", "requires_second_review", "active")
    list_filter = ("requires_second_review", "active")


for m in (Requester, CollectionTask, TimeEntry):
    admin.site.register(m)

for m in (RecordItem, Redaction, Installment):
    admin.site.register(m, ReadOnlyEvidenceAdmin)


from .models import CameraHold, FootagePreservation  # noqa: E402


class CameraHoldInline(admin.TabularInline):
    model = CameraHold
    extra = 0
    readonly_fields = ("camera_id", "camera_name", "site", "clip_id", "state",
                       "error", "excluded_at", "excluded_by", "exclusion_reason")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(FootagePreservation)
class FootagePreservationAdmin(admin.ModelAdmin):
    list_display = ("request", "state", "window_start", "window_end", "held_count")
    list_filter = ("state",)
    inlines = [CameraHoldInline]
    readonly_fields = (
        "request",
        "state",
        "window_start",
        "window_end",
        "detected_dates_note",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import RecordSystem  # noqa: E402


@admin.register(RecordSystem)
class RecordSystemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "custodian_title", "custodian_email",
                    "retention_days", "urgent_hold", "needs_verification", "active")
    list_filter = ("urgent_hold", "needs_verification", "active", "category")
    list_editable = ("custodian_email",)   # assign people fast, right from the list
    search_fields = ("name", "vendor", "custodian_title")
