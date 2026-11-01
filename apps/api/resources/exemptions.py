# ABOUTME: Staff CRUD for the RCW exemption taxonomy. Exemplar resource —
# ABOUTME: other resources follow this serializer+viewset-in-one-module shape.
from django.db import transaction
from django.db.models.deletion import ProtectedError
from rest_framework import filters, serializers, viewsets
from rest_framework.exceptions import ValidationError

from apps.api.identity import StaffRole
from apps.audit.models import audit
from apps.records.models import ExemptionCitation

EXEMPTION_FIELDS = (
    "citation",
    "short_name",
    "description",
    "requires_second_review",
    "active",
)
EXEMPTION_MEANING_FIELDS = frozenset(EXEMPTION_FIELDS).difference({"active"})


def _snapshot(exemption):
    return {
        field: getattr(exemption, field)
        for field in EXEMPTION_FIELDS
    }


class ExemptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExemptionCitation
        fields = ["id", "citation", "short_name", "description",
                  "requires_second_review", "active"]

    def validate(self, attrs):
        if self.instance and self.instance.is_in_use():
            changed_meaning = [
                field
                for field in EXEMPTION_MEANING_FIELDS
                if (
                    field in attrs
                    and attrs[field] != getattr(self.instance, field)
                )
            ]
            if changed_meaning:
                raise serializers.ValidationError(
                    "A citation already used as legal evidence cannot be "
                    "rewritten. Deactivate it and create a replacement "
                    "citation."
                )
        return attrs


class ExemptionViewSet(viewsets.ModelViewSet):
    queryset = ExemptionCitation.objects.order_by("citation")
    serializer_class = ExemptionSerializer
    filterset_fields = None
    filter_backends = [filters.SearchFilter]
    search_fields = ["citation", "short_name"]
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "create": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "update": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "partial_update": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "destroy": {StaffRole.ADMIN},
    }

    def perform_create(self, serializer):
        with transaction.atomic():
            exemption = serializer.save()
            audit(
                "exemption.created",
                target=str(exemption.pk),
                actor=self.request.user,
                after=_snapshot(exemption),
            )

    def perform_update(self, serializer):
        with transaction.atomic():
            exemption = ExemptionCitation.objects.select_for_update().get(
                pk=serializer.instance.pk
            )
            serializer.instance = exemption
            before = _snapshot(exemption)
            exemption = serializer.save()
            audit(
                "exemption.updated",
                target=str(exemption.pk),
                actor=self.request.user,
                before=before,
                after=_snapshot(exemption),
            )

    def perform_destroy(self, instance):
        with transaction.atomic():
            instance = ExemptionCitation.objects.select_for_update().get(
                pk=instance.pk
            )
            before = _snapshot(instance)
            target = str(instance.pk)
            try:
                instance.delete()
            except ProtectedError as exc:
                raise ValidationError(
                    "A citation already used as legal evidence cannot be "
                    "deleted. Deactivate it instead."
                ) from exc
            audit(
                "exemption.deleted",
                target=target,
                actor=self.request.user,
                before=before,
            )


def register(router):
    router.register("exemptions", ExemptionViewSet, basename="exemption")
