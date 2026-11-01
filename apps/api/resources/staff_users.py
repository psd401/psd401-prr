# ABOUTME: Minimal active-staff choices for accountable workflow assignment.
# ABOUTME: The selector deliberately omits email and all broader identity data.
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Exists, OuterRef, Q
from rest_framework import mixins, serializers, viewsets

from apps.api.identity import MANAGED_GROUP_PREFIX, StaffRole, role_group_name
from apps.configuration import runtime


class StaffUserChoiceSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ["id", "username", "display_name"]

    def get_display_name(self, obj) -> str:
        return obj.get_full_name().strip() or obj.username


class StaffUserChoiceViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = StaffUserChoiceSerializer
    allowed_roles = {StaffRole.RECORDS_OFFICER}
    allow_readonly_agent = False
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = (
            get_user_model()
            .objects.filter(is_active=True, is_staff=True)
        )
        role = self.request.query_params.get("role", "").strip()
        if role:
            if role not in StaffRole.values:
                return queryset.none()
            managed_membership = Group.objects.filter(
                user=OuterRef("pk"),
                name__startswith=MANAGED_GROUP_PREFIX,
            )
            target_membership = Group.objects.filter(
                user=OuterRef("pk"),
                name=role_group_name(role),
            )
            queryset = queryset.annotate(
                _has_managed_role=Exists(managed_membership),
                _has_target_role=Exists(target_membership),
            )
            role_filter = Q(is_superuser=True) | Q(_has_target_role=True)
            if (
                str(runtime.get("PRR_AUTH_MODE")).lower() == "local"
                and role in settings.PRR_LOCAL_IMPLICIT_STAFF_ROLES
            ):
                role_filter |= Q(_has_managed_role=False)
            queryset = queryset.filter(role_filter)
        return queryset.distinct().order_by(
            "first_name", "last_name", "username", "id"
        )


def register(router):
    router.register("staff-users", StaffUserChoiceViewSet, basename="staff-user")
