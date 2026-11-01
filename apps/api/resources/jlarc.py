# ABOUTME: Human-reviewed JLARC annual worksheets and immutable supplements.
# ABOUTME: Outputs are transfer aids; this module never submits to JLARC.
import csv
import io
import json
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.identity import StaffRole
from apps.records.jlarc import (
    build_jlarc_report,
    record_jlarc_annual_supplement,
)
from apps.records.models import JlarcAnnualSupplement


class JlarcSupplementSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username",
        read_only=True,
    )

    class Meta:
        model = JlarcAnnualSupplement
        fields = [
            "id",
            "reporting_year",
            "revision",
            "unallocated_staff_minutes",
            "staff_compensation_cost",
            "legal_review_cost",
            "supplies_services_cost",
            "claims_filed",
            "claims_by_type",
            "claims_by_exemption",
            "litigation_agency_cost",
            "litigation_settlements",
            "litigation_penalties",
            "litigation_requester_attorney_fees",
            "litigation_requester_costs",
            "records_management_staff_cost",
            "records_management_systems_cost",
            "records_management_other_cost",
            "other_expenses_recovered",
            "customized_service_expenses_recovered_external",
            "methodology_notes",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = fields


class JlarcSupplementInputSerializer(serializers.Serializer):
    unallocated_staff_minutes = serializers.IntegerField(
        min_value=0,
        required=False,
        default=0,
    )
    staff_compensation_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    legal_review_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    supplies_services_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    claims_filed = serializers.IntegerField(min_value=0, required=False, default=0)
    claims_by_type = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
        required=False,
        default=dict,
    )
    claims_by_exemption = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
        required=False,
        default=dict,
    )
    litigation_agency_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    litigation_settlements = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    litigation_penalties = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    litigation_requester_attorney_fees = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    litigation_requester_costs = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    records_management_staff_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    records_management_systems_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    records_management_other_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    other_expenses_recovered = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    customized_service_expenses_recovered_external = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(0), required=False, default=0
    )
    methodology_notes = serializers.CharField(min_length=10, max_length=5000)


def _validation_response(exc):
    detail = getattr(exc, "message_dict", None) or exc.messages
    return Response(detail, status=status.HTTP_400_BAD_REQUEST)


class JlarcReportViewSet(viewsets.ViewSet):
    serializer_class = JlarcSupplementSerializer
    role_permissions = {
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "export_csv": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "supplement": {StaffRole.RECORDS_OFFICER},
    }
    lookup_value_regex = r"\d{4}"

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def retrieve(self, request, pk=None):
        try:
            return Response(build_jlarc_report(pk))
        except DjangoValidationError as exc:
            return _validation_response(exc)

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=JlarcSupplementInputSerializer,
        responses={201: JlarcSupplementSerializer},
    )
    def supplement(self, request, pk=None):
        payload = JlarcSupplementInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            result = record_jlarc_annual_supplement(
                pk,
                actor=request.user,
                **payload.validated_data,
            )
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response(
            JlarcSupplementSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="export-csv")
    @extend_schema(
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
    )
    def export_csv(self, request, pk=None):
        try:
            report = build_jlarc_report(pk)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["metric", "field", "value"])
        for field, value in report["baseline"].items():
            writer.writerow(["baseline", field, value])
        for metric_id, metric in report["metrics"].items():
            for field, value in metric.items():
                if field == "label":
                    continue
                rendered = (
                    json.dumps(value, sort_keys=True, separators=(",", ":"))
                    if isinstance(value, (dict, list))
                    else value
                )
                writer.writerow([metric_id, field, rendered])
        writer.writerow(["report", "data_sha256", report["data_sha256"]])
        writer.writerow([
            "report",
            "quality_warnings",
            json.dumps(report["quality_warnings"], separators=(",", ":")),
        ])
        response = HttpResponse(
            output.getvalue(),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="psd401-jlarc-{int(pk)}-worksheet.csv"'
        )
        response["Cache-Control"] = "no-store, private"
        response["X-Content-Type-Options"] = "nosniff"
        return response


def register(router):
    router.register("jlarc-reports", JlarcReportViewSet, basename="jlarc-report")
