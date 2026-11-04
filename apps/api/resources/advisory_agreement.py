# ABOUTME: Read-only staff API for stored AI-human agreement on record
# ABOUTME: responsiveness (#38), district-wide or for one case.
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, viewsets
from rest_framework.response import Response

from apps.api.identity import StaffRole
from apps.records.agreement import agreement_report
from apps.records.models import PublicRecordsRequest

# Declared through inline_serializer: a serializer *class* attribute named
# ``label`` collides with Field.label in the type stubs.
AGREEMENT_REPORT_SCHEMA = inline_serializer(
    "AdvisoryAgreementReport",
    fields={
        "request": serializers.CharField(allow_null=True),
        "decisions": serializers.IntegerField(),
        "scored": serializers.IntegerField(),
        "agreed": serializers.IntegerField(),
        "agreement": serializers.FloatField(allow_null=True),
        "uncertain": serializers.IntegerField(),
        "without_suggestion": serializers.IntegerField(),
        "rows": inline_serializer(
            "AdvisoryAgreementRow",
            many=True,
            fields={
                "label": serializers.CharField(),
                "band": serializers.CharField(),
                "decisions": serializers.IntegerField(),
                "agreed": serializers.IntegerField(allow_null=True),
                "agreement": serializers.FloatField(allow_null=True),
            },
        ),
    },
)


class AdvisoryAgreementViewSet(viewsets.ViewSet):
    """How often the AI's suggestion matched the human decision, by label and
    confidence band. ``?request=<number>`` narrows it to one case."""

    allowed_roles = {StaffRole.RECORDS_OFFICER}

    @extend_schema(
        parameters=[OpenApiParameter("request", str, required=False)],
        responses=AGREEMENT_REPORT_SCHEMA,
    )
    def list(self, request):
        number = request.query_params.get("request")
        case = get_object_or_404(PublicRecordsRequest, number=number) if number else None
        return Response({"request": number or None, **agreement_report(request=case)})


def register(router):
    router.register("advisory-agreement", AdvisoryAgreementViewSet, basename="advisory-agreement")
