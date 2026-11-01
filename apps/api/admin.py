# ABOUTME: Audited administration of Google-to-application role mappings and
# ABOUTME: one-time approval for safe linking of existing local accounts.
from django.contrib import admin

from apps.audit.models import audit

from .identity import expire_google_authorizations
from .models import GoogleGroupRoleMapping, StaffIdentity


@admin.register(GoogleGroupRoleMapping)
class GoogleGroupRoleMappingAdmin(admin.ModelAdmin):
    # Bulk deletion would bypass the per-mapping audit and immediate session
    # revocation performed by delete_model().
    actions = None
    list_display = ("role", "google_group", "active", "updated_at")
    list_filter = ("role", "active")
    search_fields = ("google_group",)
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        before = None
        if change:
            before = GoogleGroupRoleMapping.objects.filter(pk=obj.pk).values(
                "role", "google_group", "active"
            ).first()
        super().save_model(request, obj, form, change)
        audit(
            "identity.role_mapping_changed",
            target=str(obj.pk),
            actor=request.user,
            before=before,
            after={"role": obj.role, "google_group": obj.google_group, "active": obj.active},
        )
        expire_google_authorizations()

    def delete_model(self, request, obj):
        detail = {"role": obj.role, "google_group": obj.google_group}
        target = str(obj.pk)
        super().delete_model(request, obj)
        audit("identity.role_mapping_deleted", target=target, actor=request.user, **detail)
        expire_google_authorizations()


@admin.register(StaffIdentity)
class StaffIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "source", "last_group_check_at", "allow_next_google_link")
    list_filter = ("source", "allow_next_google_link")
    search_fields = ("user__username", "user__email", "google_subject")
    readonly_fields = ("source", "last_group_check_at", "last_group_error", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.google_subject:
            fields.append("google_subject")
        return fields

    def save_model(self, request, obj, form, change):
        before_link_approval = None
        if change:
            before_link_approval = StaffIdentity.objects.get(pk=obj.pk).allow_next_google_link
        super().save_model(request, obj, form, change)
        if before_link_approval != obj.allow_next_google_link:
            audit(
                "identity.link_approval_changed",
                target=str(obj.user_id),
                actor=request.user,
                approved=obj.allow_next_google_link,
            )
