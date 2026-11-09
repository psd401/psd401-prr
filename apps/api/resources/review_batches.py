# ABOUTME: Staff API for trust-ladder batch review (#39): open a cluster or
# ABOUTME: Ready-to-discard batch, then decide it from the sampled judgments.
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
from apps.records.agreement import annotate_latest_suggestion
from apps.records.models import PublicRecordsRequest, RecordItem, ReviewBatch
from apps.records.review_batches import (
    create_cluster_batch,
    create_discard_batch,
    decide_batch,
    discard_candidates,
    policy,
)

_REVIEW_ROLES = {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER}

BATCH_SCHEMA = inline_serializer(
    "ReviewBatch",
    fields={
        "id": serializers.IntegerField(),
        "request": serializers.CharField(),
        "kind": serializers.CharField(),
        "label": serializers.CharField(),
        "proposed_state": serializers.CharField(),
        "record_count": serializers.IntegerField(),
        "samples": inline_serializer(
            "ReviewBatchSample",
            many=True,
            fields={
                "id": serializers.UUIDField(),
                "original_filename": serializers.CharField(),
                "advisory_suggested_state": serializers.CharField(allow_null=True),
                "advisory_confidence": serializers.FloatField(allow_null=True),
            },
        ),
        "min_confidence": serializers.FloatField(allow_null=True),
        "outcome": serializers.CharField(),
        "applied_count": serializers.IntegerField(),
        "skipped_count": serializers.IntegerField(),
        "judgments": serializers.DictField(child=serializers.CharField()),
    },
)


class ClusterBatchInputSerializer(serializers.Serializer):
    record_item_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    state = serializers.ChoiceField(choices=["responsive", "non_responsive"])
    cluster_label = serializers.CharField(required=False, allow_blank=True, max_length=500)


class DiscardBatchInputSerializer(serializers.Serializer):
    request = serializers.CharField()


class DecideBatchInputSerializer(serializers.Serializer):
    judgments = serializers.DictField(
        child=serializers.ChoiceField(choices=["responsive", "non_responsive"]),
    )


def _batch_data(batch: ReviewBatch) -> dict:
    samples = {
        str(row["pk"]): row
        for row in annotate_latest_suggestion(
            RecordItem.objects.filter(pk__in=batch.sample_ids)
        ).values("pk", "original_filename", "advisory_suggested_state", "advisory_confidence")
    }
    return {
        "id": batch.pk,
        "request": batch.request.number,
        "kind": batch.kind,
        "label": batch.label,
        "proposed_state": batch.proposed_state,
        "record_count": len(batch.record_item_ids),
        "samples": [
            {
                "id": pk,
                "original_filename": samples[pk]["original_filename"],
                "advisory_suggested_state": samples[pk]["advisory_suggested_state"] or None,
                "advisory_confidence": samples[pk]["advisory_confidence"],
            }
            for pk in batch.sample_ids
            if pk in samples
        ],
        "min_confidence": batch.min_confidence,
        "outcome": batch.outcome,
        "applied_count": batch.applied_count,
        "skipped_count": batch.skipped_count,
        "judgments": batch.judgments,
    }


def _service_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages) from exc


class ReviewBatchViewSet(viewsets.GenericViewSet):
    """Open and decide sampled batch reviews. A batch applies nothing until
    a reviewer has judged every sampled record and all agree."""

    allowed_roles = _REVIEW_ROLES
    queryset = ReviewBatch.objects.select_related("request")

    @extend_schema(request=ClusterBatchInputSerializer, responses={201: BATCH_SCHEMA})
    def create(self, request):
        payload = ClusterBatchInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        batch = _service_call(
            create_cluster_batch,
            payload.validated_data["record_item_ids"],
            state=payload.validated_data["state"],
            actor=request.user,
            label=payload.validated_data.get("cluster_label", ""),
        )
        return Response(_batch_data(batch), status=status.HTTP_201_CREATED)

    @extend_schema(request=DiscardBatchInputSerializer, responses={201: BATCH_SCHEMA})
    @action(detail=False, methods=["post"], url_path="discard")
    def discard(self, request):
        payload = DiscardBatchInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        case = get_object_or_404(PublicRecordsRequest, number=payload.validated_data["request"])
        batch = _service_call(create_discard_batch, case, actor=request.user)
        return Response(_batch_data(batch), status=status.HTTP_201_CREATED)

    @extend_schema(request=DecideBatchInputSerializer, responses={200: BATCH_SCHEMA})
    @action(detail=True, methods=["post"], url_path="decide")
    def decide(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="review_batches.decide",
            action="decide_review_batch",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        payload = DecideBatchInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        batch = self.get_object()
        decided = _service_call(
            decide_batch, batch.pk, actor=request.user,
            judgments=payload.validated_data["judgments"],
        )
        return Response(_batch_data(decided))

    @extend_schema(
        parameters=[OpenApiParameter("request", str, required=True)],
        responses={
            200: inline_serializer(
                "ReviewTrustPolicy",
                fields={
                    "responsive": serializers.CharField(),
                    "non_responsive": serializers.CharField(),
                    "min_confidence": serializers.FloatField(),
                    "batch_sample_size": serializers.IntegerField(),
                    "elusion_sample_size": serializers.IntegerField(),
                    "discard_candidates": serializers.IntegerField(),
                },
            )
        },
    )
    @action(detail=False, methods=["get"], url_path="policy")
    def trust_policy(self, request):
        case = get_object_or_404(PublicRecordsRequest, number=request.query_params.get("request"))
        return Response({**policy(), "discard_candidates": discard_candidates(case).count()})


def register(router):
    router.register("review-batches", ReviewBatchViewSet, basename="review-batches")
