"""Django admin guardrails — the admin is a staff surface, so the same
architecture rules apply: status changes only via services.transition()
(rule 2), StatusEvent append-only (rule 4), estimates only via
letters.revise_estimate() (rule 6), description immutable (rule 9)."""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from apps.records.admin import RequestAdmin, StatusEventInline
from apps.records.models import PublicRecordsRequest


class AdminGuardrailTestCase(TestCase):
    def setUp(self):
        self.site = AdminSite()
        user = get_user_model().objects.create_superuser(
            username="officer", email="officer@psd401.net", password="x"
        )
        self.request = RequestFactory().get("/admin/")
        self.request.user = user


class RequestAdminGuardrailsTests(AdminGuardrailTestCase):
    COMPLIANCE_FIELDS = (
        "number", "received_at", "status", "description",
        "statutory_response_due", "estimated_completion",
        "tolled_business_days", "acknowledgment_sent_at", "closed_at",
        "assigned_officer",
    )

    def test_compliance_fields_are_readonly(self):
        admin = RequestAdmin(PublicRecordsRequest, self.site)
        readonly = set(admin.get_readonly_fields(self.request))
        for field in self.COMPLIANCE_FIELDS:
            self.assertIn(field, readonly, f"{field} must not be editable in admin")

    def test_requests_cannot_be_deleted_from_admin(self):
        # Deleting a request cascades away its StatusEvents, RecordItems,
        # and Redactions — the compliance record. Admin must not allow it.
        admin = RequestAdmin(PublicRecordsRequest, self.site)
        self.assertFalse(admin.has_delete_permission(self.request))

    def test_requests_cannot_be_created_from_admin(self):
        # A request created in admin skips intake entirely: no triage, no
        # statutory deadline, no footage hold. Until a staff intake path
        # exists that runs the pipeline, admin must not offer "add".
        admin = RequestAdmin(PublicRecordsRequest, self.site)
        self.assertFalse(admin.has_add_permission(self.request))


class EstimateRevisionInlineGuardrailsTests(AdminGuardrailTestCase):
    def test_inline_offers_no_add_affordance(self):
        # Rows are readonly and deletion is off, but without this Django still
        # renders an "Add another" link; revisions only come from guarded
        # domain services (rule 6).
        from apps.records.admin import EstimateRevisionInline

        inline = EstimateRevisionInline(PublicRecordsRequest, self.site)
        self.assertFalse(inline.has_add_permission(self.request, obj=None))

    def test_delivery_evidence_fields_are_readonly(self):
        from apps.records.admin import EstimateRevisionInline

        inline = EstimateRevisionInline(PublicRecordsRequest, self.site)
        readonly = set(inline.get_readonly_fields(self.request))
        self.assertIn("communicated_at", readonly)
        self.assertIn("correspondence", readonly)


class StatusEventInlineGuardrailsTests(AdminGuardrailTestCase):
    def test_inline_is_append_only(self):
        inline = StatusEventInline(PublicRecordsRequest, self.site)
        self.assertFalse(inline.can_delete)
        self.assertFalse(inline.has_add_permission(self.request, obj=None))
        readonly = set(inline.get_readonly_fields(self.request))
        for field in ("occurred_at", "actor", "from_status", "to_status",
                      "note", "visible_to_requester"):
            self.assertIn(field, readonly, f"{field} must be readonly on the timeline")
