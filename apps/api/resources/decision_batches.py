# ABOUTME: Staff API for decision batches (#43): a token caller proposes,
# ABOUTME: a person approves or rejects in the staff application.
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Exists, OuterRef
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
    inline_serializer,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole, has_staff_role
from apps.records.decision_batches import (
    _KIND_ROLES,
    approve_decision_batch,
    propose_decision_batch,
    reject_decision_batch,
)
from apps.records.models import DecisionBatch, DecisionBatchDecision, RecordItem


class DecisionBatchSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    proposed_by_username = serializers.CharField(source="proposed_by.username", read_only=True)
    state = serializers.SerializerMethodField()
    decided_by_username = serializers.SerializerMethodField()
    decided_at = serializers.SerializerMethodField()
    decision_reason = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    records = serializers.SerializerMethodField()

    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride]
        model = DecisionBatch
        fields = [
            "id",
            "kind",
            "request_number",
            "payload",
            "evidence_summary",
            "proposed_by_username",
            "created_at",
            "state",
            "decided_by_username",
            "decided_at",
            "decision_reason",
            "result",
            "records",
        ]

    def _decision(self, obj):
        return DecisionBatchDecision.objects.filter(batch=obj).select_related("decided_by").first()

    def get_state(self, obj) -> str:
        decision = self._decision(obj)
        return decision.outcome if decision else "proposed"

    def get_decided_by_username(self, obj) -> str | None:
        decision = self._decision(obj)
        return decision.decided_by.username if decision else None

    def get_decided_at(self, obj) -> str | None:
        decision = self._decision(obj)
        return decision.decided_at.isoformat() if decision else None

    def get_decision_reason(self, obj) -> str:
        decision = self._decision(obj)
        return decision.reason if decision else ""

    @extend_schema_field(
        inline_serializer(
            "DecisionBatchRecord",
            many=True,
            fields={
                "id": serializers.UUIDField(),
                "original_filename": serializers.CharField(),
                "review_state": serializers.CharField(),
            },
        )
    )
    def get_records(self, obj) -> list:
        """The batch's records by name, so a person can check them."""
        ids = obj.payload.get("record_item_ids") or []
        rows = RecordItem.objects.filter(pk__in=ids, request=obj.request).values(
            "id", "original_filename", "review_state",
        )
        return [{**row, "id": str(row["id"])} for row in rows.order_by("original_filename")]

    def get_result(self, obj) -> dict:
        decision = self._decision(obj)
        return decision.result if decision else {}


class ProposeSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=DecisionBatch.Kind.choices)
    payload = serializers.DictField()
    evidence_summary = serializers.CharField(max_length=4000)


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField()


def _service_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages) from exc


class DecisionBatchViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    """Propose (tokens may), list, and decide (people only) decision batches."""

    allowed_roles = {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER}
    serializer_class = DecisionBatchSerializer

    def get_queryset(self):
        # A caller sees the kinds their roles can decide.
        user = self.request.user
        kinds = [kind for kind, roles in _KIND_ROLES.items() if has_staff_role(user, *roles)]
        queryset = DecisionBatch.objects.select_related("request", "proposed_by").filter(kind__in=kinds)
        decided = DecisionBatchDecision.objects.filter(batch=OuterRef("pk"))
        state = self.request.query_params.get("state", "").strip()
        if state == "proposed":
            queryset = queryset.filter(~Exists(decided))
        elif state in DecisionBatchDecision.Outcome.values:
            queryset = queryset.filter(Exists(decided.filter(outcome=state)))
        number = self.request.query_params.get("request", "").strip()
        if number:
            queryset = queryset.filter(request__number=number)
        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter("state", str, enum=["proposed", "approved", "rejected"]),
            OpenApiParameter("request", str),
        ],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=ProposeSerializer, responses={201: DecisionBatchSerializer})
    def create(self, request):
        """Propose a batch. Records the proposal only; nothing else changes."""
        payload = ProposeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        batch = _service_call(
            propose_decision_batch,
            payload.validated_data["kind"],
            payload.validated_data["payload"],
            actor=request.user,
            evidence_summary=payload.validated_data["evidence_summary"],
        )
        return Response(self.get_serializer(batch).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: DecisionBatchSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="decision_batches.approve",
            action="Approve the proposed decision batch in the staff application.",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        batch = self.get_object()
        decided = _service_call(approve_decision_batch, batch.pk, actor=request.user)
        return Response(self.get_serializer(decided).data)

    @extend_schema(request=RejectSerializer, responses={200: DecisionBatchSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="decision_batches.reject",
            action="Reject the proposed decision batch in the staff application.",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        payload = RejectSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        batch = self.get_object()
        decided = _service_call(
            reject_decision_batch, batch.pk, actor=request.user, reason=payload.validated_data["reason"],
        )
        return Response(self.get_serializer(decided).data)


def register(router):
    router.register("decision-batches", DecisionBatchViewSet, basename="decision-batches")
