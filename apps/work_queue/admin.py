from django.contrib import admin

from .models import WorkItem, WorkItemEvent, WorkItemReceipt


class ReadOnlyWorkQueueAdmin(admin.ModelAdmin):
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


admin.site.register(WorkItem, ReadOnlyWorkQueueAdmin)
admin.site.register(WorkItemEvent, ReadOnlyWorkQueueAdmin)
admin.site.register(WorkItemReceipt, ReadOnlyWorkQueueAdmin)
