# ABOUTME: Application-admin CRUD for Google group role mappings and for named
# ABOUTME: direct grants, with audit and immediate session expiry on every change.
from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.api.identity import (
    StaffRole,
    expire_google_authorizations,
    has_staff_role,
)
from apps.api.models import DirectStaffRoleGrant, GoogleGroupRoleMapping
from apps.audit.models import audit
from apps.configuration import runtime

#: A direct grant is an exception to group-based governance, so it has to say
#: why in words an access review can read.
MIN_GRANT_REASON_LENGTH = 20


def _other_active_admin_exists(*, exclude_mapping=None, exclude_grant=None):
    """Is any Application Admin left, counting groups and direct grants?

    Admin can now arrive two ways, so the lockout guard has to look at both.
    Checking only the mappings would let the last group mapping be removed
    while a direct grant still holds the fort - or, worse, let the last direct
    grant go while nobody at all remains.
    """
    mappings = GoogleGroupRoleMapping.objects.filter(
        role=StaffRole.ADMIN,
        active=True,
    )
    if exclude_mapping is not None:
        mappings = mappings.exclude(pk=exclude_mapping)
    if mappings.exists():
        return True
    grants = DirectStaffRoleGrant.objects.filter(
        role=StaffRole.ADMIN,
        active=True,
    )
    if exclude_grant is not None:
        grants = grants.exclude(pk=exclude_grant)
    return grants.exists()


def _reject_last_active_admin_removal(
    mapping,
    *,
    resulting_role,
    resulting_active,
):
    if mapping.role != StaffRole.ADMIN or not mapping.active:
        return
    removes_admin = resulting_role != StaffRole.ADMIN or not resulting_active
    if removes_admin and not _other_active_admin_exists(exclude_mapping=mapping.pk):
        raise ValidationError(
            "The last active Application Admin mapping cannot be deactivated."
        )


class GoogleGroupRoleMappingSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = GoogleGroupRoleMapping
        fields = [
            "id", "role", "role_label", "google_group", "active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_google_group(self, value):
        value = value.strip().lower()
        if value.rpartition("@")[2] not in runtime.get("PRR_GOOGLE_GROUP_ALLOWED_DOMAINS"):
            raise serializers.ValidationError(
                "Role groups must use an approved Workspace domain."
            )
        return value

    def validate(self, attrs):
        instance = self.instance
        resulting_role = attrs.get(
            "role",
            instance.role if instance else None,
        )
        resulting_active = attrs.get(
            "active",
            instance.active if instance else True,
        )
        if instance:
            _reject_last_active_admin_removal(
                instance,
                resulting_role=resulting_role,
                resulting_active=resulting_active,
            )
        return attrs


def _expire_google_authorizations(actor):
    expire_google_authorizations(keep_sessions_of=actor)


class GoogleGroupRoleMappingViewSet(viewsets.ModelViewSet):
    queryset = GoogleGroupRoleMapping.objects.all()
    serializer_class = GoogleGroupRoleMappingSerializer
    allowed_roles = {StaffRole.ADMIN}
    allow_readonly_agent = False

    def _require_records_officer_mapping_authority(self, *roles):
        if (
            StaffRole.RECORDS_OFFICER in roles
            and not has_staff_role(
                self.request.user,
                StaffRole.RECORDS_OFFICER,
            )
        ):
            raise PermissionDenied(
                "Records Officer authority is required to configure the "
                "Records Officer role mapping."
            )

    def perform_create(self, serializer):
        with transaction.atomic():
            list(
                GoogleGroupRoleMapping.objects.select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            self._require_records_officer_mapping_authority(
                serializer.validated_data["role"]
            )
            mapping = serializer.save()
            audit(
                "identity.role_mapping_created",
                target=str(mapping.pk),
                actor=self.request.user,
                role=mapping.role,
                google_group=mapping.google_group,
                active=mapping.active,
            )
        _expire_google_authorizations(self.request.user)

    def perform_update(self, serializer):
        with transaction.atomic():
            list(
                GoogleGroupRoleMapping.objects.select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            mapping = GoogleGroupRoleMapping.objects.get(
                pk=serializer.instance.pk
            )
            serializer.instance = mapping
            resulting_role = serializer.validated_data.get(
                "role",
                mapping.role,
            )
            resulting_active = serializer.validated_data.get(
                "active",
                mapping.active,
            )
            _reject_last_active_admin_removal(
                mapping,
                resulting_role=resulting_role,
                resulting_active=resulting_active,
            )
            self._require_records_officer_mapping_authority(
                mapping.role,
                resulting_role,
            )
            before = {
                "role": mapping.role,
                "google_group": mapping.google_group,
                "active": mapping.active,
            }
            mapping = serializer.save()
            audit(
                "identity.role_mapping_changed",
                target=str(mapping.pk),
                actor=self.request.user,
                before=before,
                after={
                    "role": mapping.role,
                    "google_group": mapping.google_group,
                    "active": mapping.active,
                },
            )
        _expire_google_authorizations(self.request.user)

    def perform_destroy(self, instance):
        with transaction.atomic():
            list(
                GoogleGroupRoleMapping.objects.select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            mapping = GoogleGroupRoleMapping.objects.get(pk=instance.pk)
            self._require_records_officer_mapping_authority(mapping.role)
            if (
                mapping.role == StaffRole.ADMIN
                and mapping.active
                and not _other_active_admin_exists(exclude_mapping=mapping.pk)
            ):
                raise ValidationError(
                    "The last active Application Admin mapping cannot be deleted."
                )
            detail = {
                "role": mapping.role,
                "google_group": mapping.google_group,
            }
            target = str(mapping.pk)
            mapping.delete()
            audit(
                "identity.role_mapping_deleted",
                target=target,
                actor=self.request.user,
                **detail,
            )
        _expire_google_authorizations(self.request.user)


class DirectStaffRoleGrantSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    granted_by_username = serializers.CharField(
        source="granted_by.username",
        read_only=True,
    )

    class Meta:
        model = DirectStaffRoleGrant
        fields = [
            "id", "email", "role", "role_label", "active", "reason",
            "granted_by_username", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "granted_by_username", "created_at", "updated_at",
        ]

    def validate_email(self, value):
        value = value.strip().lower()
        if value.rpartition("@")[2] != runtime.get("PRR_GOOGLE_HOSTED_DOMAIN"):
            raise serializers.ValidationError(
                "A direct grant must name an address in the district's "
                "Workspace domain."
            )
        return value

    def validate_reason(self, value):
        value = value.strip()
        if len(value) < MIN_GRANT_REASON_LENGTH:
            raise serializers.ValidationError(
                "Record why this person is granted a role directly instead of "
                f"through a group, in at least {MIN_GRANT_REASON_LENGTH} "
                "characters."
            )
        return value

    def validate(self, attrs):
        instance = self.instance
        if instance is None:
            return attrs
        resulting_role = attrs.get("role", instance.role)
        resulting_active = attrs.get("active", instance.active)
        removes_admin = (
            instance.role == StaffRole.ADMIN
            and instance.active
            and (resulting_role != StaffRole.ADMIN or not resulting_active)
        )
        if removes_admin and not _other_active_admin_exists(
            exclude_grant=instance.pk
        ):
            raise ValidationError(
                "The last active Application Admin grant cannot be deactivated."
            )
        return attrs


class DirectStaffRoleGrantViewSet(viewsets.ModelViewSet):
    """Name an individual who needs a role but belongs to no syncable group.

    Deliberately the same shape as the group mapping viewset: administrator
    only, Records Officer authority required to hand out Records Officer,
    every change audited, and every change expiring live authorizations so a
    revoked grant stops working now rather than at the next login.
    """

    queryset = DirectStaffRoleGrant.objects.select_related("granted_by")
    serializer_class = DirectStaffRoleGrantSerializer
    allowed_roles = {StaffRole.ADMIN}
    allow_readonly_agent = False

    def _require_records_officer_grant_authority(self, *roles):
        if (
            StaffRole.RECORDS_OFFICER in roles
            and not has_staff_role(
                self.request.user,
                StaffRole.RECORDS_OFFICER,
            )
        ):
            raise PermissionDenied(
                "Records Officer authority is required to grant the Records "
                "Officer role."
            )

    def _lock(self):
        list(
            DirectStaffRoleGrant.objects.select_for_update()
            .order_by("pk")
            .values_list("pk", flat=True)
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            self._lock()
            self._require_records_officer_grant_authority(
                serializer.validated_data["role"]
            )
            grant = serializer.save(granted_by=self.request.user)
            audit(
                "identity.direct_role_grant_created",
                target=str(grant.pk),
                actor=self.request.user,
                email=grant.email,
                role=grant.role,
                active=grant.active,
                reason=grant.reason,
            )
        _expire_google_authorizations(self.request.user)

    def perform_update(self, serializer):
        with transaction.atomic():
            self._lock()
            grant = DirectStaffRoleGrant.objects.get(pk=serializer.instance.pk)
            serializer.instance = grant
            self._require_records_officer_grant_authority(
                grant.role,
                serializer.validated_data.get("role", grant.role),
            )
            before = {
                "email": grant.email,
                "role": grant.role,
                "active": grant.active,
            }
            grant = serializer.save()
            audit(
                "identity.direct_role_grant_changed",
                target=str(grant.pk),
                actor=self.request.user,
                before=before,
                after={
                    "email": grant.email,
                    "role": grant.role,
                    "active": grant.active,
                },
            )
        _expire_google_authorizations(self.request.user)

    def perform_destroy(self, instance):
        with transaction.atomic():
            self._lock()
            grant = DirectStaffRoleGrant.objects.get(pk=instance.pk)
            self._require_records_officer_grant_authority(grant.role)
            if (
                grant.role == StaffRole.ADMIN
                and grant.active
                and not _other_active_admin_exists(exclude_grant=grant.pk)
            ):
                raise ValidationError(
                    "The last active Application Admin grant cannot be deleted."
                )
            detail = {"email": grant.email, "role": grant.role}
            target = str(grant.pk)
            grant.delete()
            audit(
                "identity.direct_role_grant_deleted",
                target=target,
                actor=self.request.user,
                **detail,
            )
        _expire_google_authorizations(self.request.user)


def register(router):
    router.register(
        "google-role-mappings",
        GoogleGroupRoleMappingViewSet,
        basename="google-role-mapping",
    )
    router.register(
        "direct-role-grants",
        DirectStaffRoleGrantViewSet,
        basename="direct-role-grant",
    )
