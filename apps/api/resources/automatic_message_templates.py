"""Governed staff API for every editable requester-message template."""

import datetime as dt
from types import SimpleNamespace

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from apps.api.identity import WORKFLOW_STAFF_ROLES, StaffRole
from apps.correspondence import guardrails, services
from apps.correspondence.automatic_templates import (
    FREE_FORM_TEMPLATE_KINDS,
    SCOPE_RESPONSE_PARAGRAPHS,
    TEMPLATE_SPECS,
    active_template,
    active_template_names,
    fee_paragraph,
    narrowing_options_block,
    render_source,
)
from apps.correspondence.models import AutomaticMessageTemplate


def _service_validation(exc):
    """Translate a service error, keeping guardrail warnings machine-readable.

    A tripped guardrail is not "your input is malformed" -- it is a decision
    the officer is entitled to make and has not made yet. The response carries
    the warnings and their consequences so the screen can show what publishing
    would cost the requester, instead of flattening it to a sentence.
    """
    if isinstance(exc, guardrails.GuardrailOverrideRequired):
        raise GuardrailOverrideNeeded(
            {
                "detail": exc.messages[0],
                "code": "guardrail_override_required",
                "may_override": True,
                "guardrail_warnings": [
                    warning.as_dict() for warning in exc.warnings
                ],
            }
        ) from exc
    if isinstance(exc, guardrails.GuardrailNotOverridable):
        raise GuardrailOverrideNeeded(
            {
                "detail": exc.messages[0],
                "code": "guardrail_not_overridable",
                "may_override": False,
                "guardrail_warnings": [
                    warning.as_dict() for warning in exc.warnings
                ],
            }
        ) from exc
    detail = getattr(exc, "message_dict", None) or exc.messages
    raise serializers.ValidationError(detail) from exc


class GuardrailOverrideNeeded(APIException):
    """409: the request is well-formed, the officer has a decision to make."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "guardrail_override_required"


class AutomaticMessageTemplateSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)
    activated_by_username = serializers.CharField(
        source="activated_by.username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = AutomaticMessageTemplate
        fields = [
            "id",
            "kind",
            "name",
            "template_version",
            "state",
            "source",
            "subject_template",
            "body_template",
            "change_reason",
            "guardrail_overrides",
            "guardrail_override_reason",
            "created_by_username",
            "activated_by_username",
            "created_at",
            "updated_at",
            "activated_at",
            "revision",
        ]
        read_only_fields = fields

    def get_source(self, _obj) -> str:
        return "managed"


class AutomaticMessageTemplateWriteSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=AutomaticMessageTemplate.Kind.choices)
    name = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=120,
        help_text=(
            "Only general correspondence may carry several named templates. "
            "Leave blank for the district default."
        ),
    )
    subject_template = serializers.CharField(max_length=998, trim_whitespace=False)
    body_template = serializers.CharField(max_length=20000, trim_whitespace=False)
    change_reason = serializers.CharField(min_length=10, max_length=1000)
    guardrail_override_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
        help_text=(
            "Why this template is published despite a guardrail. Required "
            "only when one is tripped, and only a Records Officer or "
            "Application Admin may supply it."
        ),
    )


class AutomaticMessageTemplateEditSerializer(serializers.Serializer):
    subject_template = serializers.CharField(max_length=998, trim_whitespace=False)
    body_template = serializers.CharField(max_length=20000, trim_whitespace=False)
    change_reason = serializers.CharField(min_length=10, max_length=1000)
    expected_version = serializers.IntegerField(min_value=1)
    guardrail_override_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
    )


class AutomaticMessageTemplateActivateSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    confirmed = serializers.BooleanField()
    reason = serializers.CharField(min_length=10, max_length=1000)


def _builtin_template(kind, spec):
    return {
        "id": None,
        "kind": kind,
        "name": "",
        "template_version": spec.builtin_version or "default",
        "state": "active",
        "source": "built_in",
        "subject_template": spec.subject_template,
        "body_template": spec.body_template,
        "change_reason": "Source-controlled application default.",
        "created_by_username": None,
        "activated_by_username": None,
        "created_at": None,
        "updated_at": None,
        "activated_at": None,
        "revision": None,
    }


def _sample_context():
    """A PII-free example context supplying every structured token."""
    requester = SimpleNamespace(name="Jane Public")
    request = SimpleNamespace(
        requester_id="sample-requester",
        requester=requester,
        number="PRR-2026-0001",
        received_at=dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC),
        description="Emails and attachments concerning the capital project.",
    )
    return {
        "request": request,
        "estimated_completion": dt.date(2026, 9, 10),
        "clarification_questions": (
            "Which schools and which date range should the District search?"
        ),
        "clarification_due": dt.date(2026, 8, 24),
        "previous_date": dt.date(2026, 9, 10),
        "new_date": dt.date(2026, 10, 8),
        "reason": (
            "A third-party notice under RCW 42.56.540 added review time."
        ),
        "installment": SimpleNamespace(sequence=2),
        "item_count": 14,
        "download_url": "https://prr.psd401.net/downloads/sample",
        "download_expires_at": dt.datetime(2026, 9, 24, 17, 0, tzinfo=dt.UTC),
        "has_exemptions": True,
        "is_final": False,
        "search_completed_at": dt.datetime(2026, 9, 2, 16, 0, tzinfo=dt.UTC),
        "search_steps": [
            {
                "location": "District email (Google Vault)",
                "custodian": "Chief Information Officer",
                "method": "Vault matter export by keyword",
                "date_range": "2026-01-01 to 2026-06-30",
                "result": "No responsive messages",
            }
        ],
        "adequacy_reason": (
            "Every system in the registry that could hold these records was "
            "searched by its custodian."
        ),
        "match_count_phrase": "29,380 items",
        "query_summary": (
            "district Gmail for the named employee and the capital-project "
            "terms, through the date the request arrived"
        ),
        "narrowing_options": narrowing_options_block(
            [
                {
                    "terms": ["capital project", "bid"],
                    "rationale": (
                        "Only the bid correspondence for the capital project."
                    ),
                }
            ]
        ),
        "fee_paragraph": fee_paragraph(),
        "interpretation": (
            "Emails about the capital project's bid, January through June 2026."
        ),
        "record_estimate": (
            "The District expects about 40 responsive records (between 31 and 52)."
        ),
        "charge_paragraph": (
            "The estimated copying charge is about $1.25, calculated under "
            "RCW 42.56.120 from the District's adopted fee schedule."
        ),
        "completion_date": dt.date(2026, 10, 8),
        "scope_response_paragraph": SCOPE_RESPONSE_PARAGRAPHS["clarification"],
        "determination": "outside_retention",
        "retention_schedule_reference": "WA Local Government CORE, GS50-01A-11",
        "production_summary": (
            "The District produced 214 pages across three installments and "
            "withheld nothing."
        ),
        "amount_due": "$125.00",
        "payment_requested_on": dt.date(2026, 8, 12),
        "estimated_amount": "$367.25",
        "estimate_basis": (
            "29,380 electronic files at five cents per four files under the "
            "adopted schedule, plus $2.00 for the transfer media."
        ),
        "deposit_amount": "$36.72",
        "employee_name": "Alex Sample",
        "disclosed_requesters": "Jane Public",
        "response_by": dt.date(2026, 9, 24),
    }


class AutomaticMessageTemplateViewSet(viewsets.ModelViewSet):
    queryset = AutomaticMessageTemplate.objects.select_related(
        "created_by",
        "activated_by",
    )
    serializer_class = AutomaticMessageTemplateSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    allow_readonly_agent = False
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "preview": WORKFLOW_STAFF_ROLES,
        "create": {StaffRole.RECORDS_OFFICER},
        "update": {StaffRole.RECORDS_OFFICER},
        "partial_update": {StaffRole.RECORDS_OFFICER},
        "activate": {StaffRole.RECORDS_OFFICER},
    }

    def list(self, request, *args, **kwargs):
        templates = []
        for kind, spec in TEMPLATE_SPECS.items():
            active = active_template(kind)
            managed = list(self.get_queryset().filter(kind=kind))
            templates.append(
                {
                    "kind": kind,
                    "label": spec.label,
                    "group": spec.group,
                    "authoring": (
                        "free_form"
                        if kind in FREE_FORM_TEMPLATE_KINDS
                        else "structured"
                    ),
                    "allowed_tokens": sorted(spec.allowed_tokens),
                    "required_tokens": sorted(spec.required_tokens),
                    "active": (
                        AutomaticMessageTemplateSerializer(active).data
                        if active is not None
                        else _builtin_template(kind, spec)
                    ),
                    # Every other activated starting point for this kind.
                    # Empty for the single-slot kinds, which is what makes
                    # "named" visible in the response rather than implied.
                    "named_active": AutomaticMessageTemplateSerializer(
                        [
                            template
                            for template in managed
                            if template.name
                            and template.state
                            == AutomaticMessageTemplate.State.ACTIVE
                        ],
                        many=True,
                    ).data,
                    "names": active_template_names(kind),
                    "drafts": AutomaticMessageTemplateSerializer(
                        [
                            template
                            for template in managed
                            if template.state == AutomaticMessageTemplate.State.DRAFT
                        ],
                        many=True,
                    ).data,
                    "history": AutomaticMessageTemplateSerializer(
                        [
                            template
                            for template in managed
                            if template.state == AutomaticMessageTemplate.State.RETIRED
                        ],
                        many=True,
                    ).data,
                }
            )
        return Response({"templates": templates})

    def create(self, request, *args, **kwargs):
        serializer = AutomaticMessageTemplateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            template = services.create_automatic_message_template_draft(
                actor=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            _service_validation(exc)
        return Response(
            AutomaticMessageTemplateSerializer(template).data,
            status=status.HTTP_201_CREATED,
        )

    def _edit(self, request):
        template = self.get_object()
        serializer = AutomaticMessageTemplateEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            template = services.edit_automatic_message_template_draft(
                template,
                actor=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            _service_validation(exc)
        return Response(AutomaticMessageTemplateSerializer(template).data)

    def update(self, request, *args, **kwargs):
        return self._edit(request)

    def partial_update(self, request, *args, **kwargs):
        return self._edit(request)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        template = self.get_object()
        try:
            rendered = render_source(
                template.kind,
                template.subject_template,
                template.body_template,
                _sample_context(),
            )
        except DjangoValidationError as exc:
            _service_validation(exc)
        return Response({"subject": rendered.subject, "body": rendered.body})

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        template = self.get_object()
        serializer = AutomaticMessageTemplateActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            template = services.activate_automatic_message_template(
                template,
                actor=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            _service_validation(exc)
        return Response(AutomaticMessageTemplateSerializer(template).data)


def register(router):
    router.register(
        "automatic-message-templates",
        AutomaticMessageTemplateViewSet,
        basename="automatic-message-template",
    )
