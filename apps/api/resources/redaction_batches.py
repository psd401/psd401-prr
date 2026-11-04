# ABOUTME: Staff API for group acceptance of redaction proposals (#40) and the
# ABOUTME: one action that builds every ready redacted PDF on a case.
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole
from apps.configuration import runtime
from apps.records.models import (
    PublicRecordsRequest,
    RecordItem,
    RedactionBatch,
    RedactionProposal,
)
from apps.records.redaction_batches import (
    build_ready_redacted_records,
    create_redaction_batch,
    decide_redaction_batch,
    proposal_groups,
)

BATCH_SCHEMA = inline_serializer(
    "RedactionBatch",
    fields={
        "id": serializers.IntegerField(),
        "request": serializers.CharField(),
        "exemption_citation": serializers.CharField(),
        "pattern": serializers.CharField(),
        "pattern_label": serializers.CharField(),
        "proposal_count": serializers.IntegerField(),
        "samples": inline_serializer(
            "RedactionBatchSample",
            many=True,
            fields={
                "id": serializers.UUIDField(),
                "record_item_id": serializers.UUIDField(),
                "original_filename": serializers.CharField(),
                "page_number": serializers.IntegerField(),
                "confidence": serializers.FloatField(),
                "rationale": serializers.CharField(),
            },
        ),
        "outcome": serializers.CharField(),
        "accepted_count": serializers.IntegerField(),
        "skipped_count": serializers.IntegerField(),
    },
)


class OpenRedactionBatchInputSerializer(serializers.Serializer):
    request = serializers.CharField()
    exemption_id = serializers.IntegerField()
    pattern = serializers.ChoiceField(choices=RedactionProposal.Pattern.values)


class RedactionJudgmentSerializer(serializers.Serializer):
    accept = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True)


class DecideRedactionBatchInputSerializer(serializers.Serializer):
    judgments = serializers.DictField(child=RedactionJudgmentSerializer())


class BuildInputSerializer(serializers.Serializer):
    request = serializers.CharField()


def _batch_data(batch: RedactionBatch) -> dict:
    samples = {
        str(p.pk): p
        for p in RedactionProposal.objects.filter(pk__in=batch.sample_ids).select_related("record_item")
    }
    return {
        "id": batch.pk,
        "request": batch.request.number,
        "exemption_citation": batch.exemption.citation,
        "pattern": batch.pattern,
        "pattern_label": RedactionProposal.Pattern(batch.pattern).label,
        "proposal_count": len(batch.proposal_ids),
        "samples": [
            {
                "id": pk,
                "record_item_id": str(samples[pk].record_item_id),
                "original_filename": samples[pk].record_item.original_filename,
                "page_number": samples[pk].page_number,
                "confidence": samples[pk].confidence,
                "rationale": samples[pk].rationale,
            }
            for pk in batch.sample_ids
            if pk in samples
        ],
        "outcome": batch.outcome,
        "accepted_count": batch.accepted_count,
        "skipped_count": batch.skipped_count,
    }


def _service_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages) from exc


class RedactionBatchViewSet(viewsets.GenericViewSet):
    """Group acceptance of redaction proposals. Nothing is accepted until a
    reviewer has accepted every sampled proposal in the group."""

    allowed_roles = {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER}
    queryset = RedactionBatch.objects.select_related("request", "exemption")

    @extend_schema(
        parameters=[OpenApiParameter("request", str, required=True)],
        responses={
            200: inline_serializer(
                "RedactionProposalGroups",
                fields={
                    "mode": serializers.CharField(),
                    "sample_size": serializers.IntegerField(),
                    "groups": serializers.ListField(child=serializers.DictField()),
                    "records_with_redactions": serializers.IntegerField(),
                },
            )
        },
    )
    @action(detail=False, methods=["get"], url_path="groups")
    def groups(self, request):
        case = get_object_or_404(PublicRecordsRequest, number=request.query_params.get("request"))
        return Response({
            "mode": runtime.get("PRR_TRUST_REDACTION_MODE"),
            "sample_size": runtime.get("PRR_TRUST_BATCH_SAMPLE_SIZE"),
            "groups": proposal_groups(case),
            "records_with_redactions": RecordItem.objects.filter(
                request=case,
                review_state=RecordItem.ReviewState.RESPONSIVE,
                redactions__voided_at__isnull=True,
            ).distinct().count(),
        })

    @extend_schema(request=OpenRedactionBatchInputSerializer, responses={201: BATCH_SCHEMA})
    def create(self, request):
        payload = OpenRedactionBatchInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        case = get_object_or_404(PublicRecordsRequest, number=payload.validated_data["request"])
        batch = _service_call(
            create_redaction_batch,
            case,
            exemption_id=payload.validated_data["exemption_id"],
            pattern=payload.validated_data["pattern"],
            actor=request.user,
        )
        return Response(_batch_data(batch), status=status.HTTP_201_CREATED)

    @extend_schema(request=DecideRedactionBatchInputSerializer, responses={200: BATCH_SCHEMA})
    @action(detail=True, methods=["post"], url_path="decide")
    def decide(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="redaction_batches.decide",
            action="decide_redaction_batch",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        payload = DecideRedactionBatchInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        batch = self.get_object()
        decided = _service_call(
            decide_redaction_batch, batch.pk, actor=request.user,
            judgments=payload.validated_data["judgments"],
        )
        return Response(_batch_data(decided))

    @extend_schema(
        request=BuildInputSerializer,
        responses={
            200: inline_serializer(
                "RedactedRecordsBuilt",
                fields={
                    "built": serializers.IntegerField(),
                    "awaiting_second_review": serializers.IntegerField(),
                    "already_current": serializers.IntegerField(),
                    "failed": serializers.IntegerField(),
                },
            )
        },
    )
    @action(detail=False, methods=["post"], url_path="build")
    def build(self, request):
        handoff = human_only_handoff(
            request,
            capability_id="redaction_batches.build",
            action="build_ready_redacted_records",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        payload = BuildInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        case = get_object_or_404(PublicRecordsRequest, number=payload.validated_data["request"])
        return Response(_service_call(build_ready_redacted_records, case, actor=request.user))


def register(router):
    router.register("redaction-batches", RedactionBatchViewSet, basename="redaction-batches")
