# ABOUTME: Staff resources for requesters, record items, installments, and
# ABOUTME: PRA time entries — four small verticals grouped in one module.
import hashlib
from decimal import Decimal
from typing import NoReturn

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models, transaction
from django.http import HttpResponse, QueryDict
from drf_spectacular.utils import extend_schema, extend_schema_field, inline_serializer
from rest_framework import filters, mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.advisory import AdvisoryLatestSerializer, AdvisoryRunSerializer
from apps.api.handoffs import human_only_handoff
from apps.api.identity import WORKFLOW_STAFF_ROLES, StaffRole, has_staff_role
from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.advisory_ai import (
    request_record_redaction_advisory,
    request_record_responsiveness_advisory,
)
from apps.records.agreement import annotate_latest_suggestion
from apps.records.clusters import cluster_records
from apps.records.exemption_logs import generate_exemption_log
from apps.records.fees import (
    activate_fee_schedule,
    activate_fee_waiver_floor,
    apply_deposit_gate,
    assess_installment_fee,
    assessment_is_below_waiver_floor,
    current_fee_schedule,
    current_fee_waiver_floor,
    lift_deposit_gate,
    record_deposit_payment,
    record_fee_payment,
)
from apps.records.handling import (
    HANDLING_DETAIL,
    HANDLING_LABELS,
    handling_for_item,
)
from apps.records.installments import (
    InstallmentNoticeUnsent,
    clear_installment_access_code,
    create_installment,
    release_installment_and_send_notice,
    release_ready_record_items,
    revoke_installment,
    set_installment_access_code,
    set_installment_items,
    unreleased_record_items,
)
from apps.records.models import (
    AdvisoryRun,
    CollectionTask,
    ExemptionCitation,
    FeeAssessment,
    FeeDepositGate,
    FeePayment,
    FeeSchedule,
    FeeWaiverFloor,
    Installment,
    MediaRedaction,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    RedactionProposal,
    RedactionProposalDecision,
    Requester,
    TimeEntry,
)
from apps.records.preview import (
    is_previewable,
    preview_pdf_metadata,
    render_pdf_preview_page,
)
from apps.records.production import (
    prepare_installment,
    release_installment_with_notice,
)
from apps.records.record_tags import (
    PRIVILEGE_TAGS,
    TAG_LABELS,
    TAG_NAMES,
    annotate_tags,
    annotation_name,
    counsel_domains,
    filter_by_tag,
    tags_for_item,
)
from apps.records.redaction_ai import (
    accept_redaction_proposal,
    reject_redaction_proposal,
)
from apps.records.review import (
    BULK_REVIEW_FILTER_MAX_ITEMS,
    BULK_REVIEW_MAX_ITEMS,
    BULK_REVIEW_STATES,
    BulkSelectionChanged,
    approve_second_review,
    bulk_review_confirmed_selection,
    bulk_review_records,
    bulk_review_selection,
    create_redaction,
    render_redacted_record,
    review_record,
    undo_review,
    void_redaction,
)


def _raise_api_validation(exc) -> NoReturn:
    detail = getattr(exc, "message_dict", None) or exc.messages
    raise serializers.ValidationError(detail) from exc


class RequesterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Requester
        fields = [
            "id", "name", "email", "phone", "mailing_address", "city",
            "state", "postal_code", "company", "created_at",
        ]
        read_only_fields = ["id", "email", "created_at"]


class RequesterViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = Requester.objects.order_by("name")
    serializer_class = RequesterSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "email"]
    allowed_roles = {StaffRole.RECORDS_OFFICER}
    allow_readonly_agent = False


class RecordItemSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    reviewed_by_username = serializers.CharField(
        source="reviewed_by.username", read_only=True, default=None)
    redaction_count = serializers.SerializerMethodField()
    withholding_exemption_citations = serializers.SerializerMethodField()
    second_review_required = serializers.SerializerMethodField()
    second_reviewed_by_username = serializers.CharField(
        source="second_reviewed_by.username",
        read_only=True,
        default=None,
    )
    redacted_artifact_ready = serializers.SerializerMethodField()
    handling = serializers.SerializerMethodField()
    handling_label = serializers.SerializerMethodField()
    handling_detail = serializers.SerializerMethodField()
    reviewable = serializers.SerializerMethodField()
    custodians = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    advisory_suggested_state = serializers.SerializerMethodField()
    advisory_confidence = serializers.SerializerMethodField()

    class Meta:
        model = RecordItem
        fields = [
            "id", "request_number", "source_system", "custodian",
            "custodians",
            "original_filename", "sha256", "review_state",
            "reviewed_by_username", "reviewed_at", "redaction_count",
            "second_review_required", "second_reviewed_by_username",
            "second_reviewed_at", "redacted_artifact_ready",
            "withholding_exemption_citations", "created_at",
            "content_type", "content_length",
            "handling", "handling_label", "handling_detail",
            "derivative_state", "derivative_detail", "reviewable",
            "email_from", "email_to", "email_cc", "email_sent_at",
            "email_message_id", "email_in_reply_to", "email_sender_domain",
            "email_auto_submitted", "email_list_id", "email_precedence",
            "email_has_calendar_part",
            "tags", "advisory_suggested_state", "advisory_confidence",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_custodians(self, obj):
        """Every mailbox this record was found in, not a joined string."""
        recorded = [row.custodian for row in obj.custodians.all()]
        if recorded:
            return recorded
        return [obj.custodian] if obj.custodian else []

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_tags(self, obj):
        return tags_for_item(obj, counsel=self.context.get("counsel_domains"))

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_advisory_suggested_state(self, obj):
        return getattr(obj, "advisory_suggested_state", None) or None

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_advisory_confidence(self, obj):
        value = getattr(obj, "advisory_confidence", None)
        return None if value is None else float(value)

    #: Full correspondent addresses are requester-and-third-party identity.
    #: The request serializers already blank requester name and email for
    #: anyone who is not a Records Officer; these fields are the same identity
    #: arriving through the record, so they follow the same rule. The sending
    #: *domain* and the deterministic tags stay visible, which is what review
    #: triage actually needs.
    IDENTITY_FIELDS = ("email_from", "email_to", "email_cc")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not has_staff_role(user, StaffRole.RECORDS_OFFICER):
            for field in self.IDENTITY_FIELDS:
                data[field] = ""
        return data

    @extend_schema_field(serializers.BooleanField())
    def get_reviewable(self, obj):
        return is_previewable(obj)

    @extend_schema_field(serializers.CharField())
    def get_handling(self, obj):
        return handling_for_item(obj)

    @extend_schema_field(serializers.CharField())
    def get_handling_label(self, obj):
        return HANDLING_LABELS[handling_for_item(obj)]

    @extend_schema_field(serializers.CharField())
    def get_handling_detail(self, obj):
        return HANDLING_DETAIL[handling_for_item(obj)]

    @extend_schema_field(serializers.IntegerField())
    def get_redaction_count(self, obj):
        # Counted from the prefetched rows. Calling .filter() on the related
        # manager would bypass the prefetch and issue one query per record.
        return sum(
            1 for redaction in obj.redactions.all()
            if redaction.voided_at is None
        )

    @extend_schema_field(serializers.BooleanField())
    def get_second_review_required(self, obj):
        return obj.second_review_required

    @extend_schema_field(serializers.BooleanField())
    def get_redacted_artifact_ready(self, obj):
        return bool(
            obj.redacted_storage_key
            and obj.redacted_sha256
            and obj.redacted_content_length is not None
            and obj.redacted_content_type
            and obj.redacted_manifest_sha256
        )

    @extend_schema_field(
        serializers.ListField(child=serializers.CharField())
    )
    def get_withholding_exemption_citations(self, obj):
        citations = []
        if obj.withholding_exemption is not None:
            citations.append(obj.withholding_exemption.citation)
        citations.extend(
            exemption.citation
            for exemption in obj.withholding_exemptions.all()
        )
        return list(dict.fromkeys(citations))


class RecordReviewInputSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=RecordItem.ReviewState.choices)
    withholding_exemption_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )


#: The list filters a filter-mode bulk decision may carry. Ordering and paging
#: are left out: they change what is shown, never what matches.
BULK_FILTER_KEYS = (
    "request", "review_state", "source_system", "tag", "sender_domain",
    "custodian", "advisory_state", "confidence_min", "search", "handling",
    "collection_task",
)


class BulkRecordReviewInputSerializer(serializers.Serializer):
    """One human responsiveness decision over an explicit set, or over every
    record a filter matches after the reviewer has seen the exact count."""

    record_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=BULK_REVIEW_MAX_ITEMS,
        required=False,
    )
    filter = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    expected_count = serializers.IntegerField(min_value=1, required=False)
    selection_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)
    state = serializers.ChoiceField(choices=[
        (value, RecordItem.ReviewState(value).label)
        for value in BULK_REVIEW_STATES
    ])

    def validate(self, attrs):
        has_ids = "record_item_ids" in attrs
        has_filter = "filter" in attrs
        if has_ids == has_filter:
            raise serializers.ValidationError(
                "Send either record_item_ids or a filter, not both."
            )
        if has_filter:
            if "expected_count" not in attrs or "selection_sha256" not in attrs:
                raise serializers.ValidationError(
                    "A filter decision needs the expected_count and "
                    "selection_sha256 from bulk-selection."
                )
            attrs["filter"] = _bulk_filter(attrs["filter"])
        return attrs


def _auditable_filter(filters):
    """The filter as the append-only audit log may keep it.

    Free text a reviewer typed can name a student; the log keeps its digest,
    which still proves which search was decided, never the words.
    """
    detail = dict(filters)
    search = detail.pop("search", "")
    if search:
        detail["search_sha256"] = hashlib.sha256(search.encode()).hexdigest()
    return detail


def _bulk_filter(values):
    """Keep only list filters, and require exactly one request."""
    cleaned = {
        key: str(values[key]).strip()
        for key in BULK_FILTER_KEYS
        if str(values.get(key) or "").strip()
    }
    if not cleaned.get("request"):
        raise serializers.ValidationError(
            {"request": ["Bulk review by filter applies to one request at a time."]}
        )
    return cleaned


#: How many distinct values one open-ended facet returns. A reviewer chooses
#: from the domains and mailboxes that carry volume, not from a thousand.
FACET_VALUE_LIMIT = 50


def _facet_rows(queryset, field: str, *, limit: int | None = FACET_VALUE_LIMIT):
    """Distinct-record counts per value of ``field``, biggest first."""
    rows = (
        queryset.exclude(**{f"{field}__isnull": True})
        .exclude(**{field: ""})
        .values(field)
        .annotate(count=models.Count("pk", distinct=True))
        .order_by("-count", field)
    )
    if limit is not None:
        rows = rows[:limit]
    return [{"value": row[field], "count": row["count"]} for row in rows]


class RedactionProposalDecisionOutputSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=RedactionProposalDecision.Outcome.choices,
    )
    decided_by_username = serializers.CharField()
    reason = serializers.CharField(allow_blank=True)
    redaction = serializers.IntegerField(allow_null=True)
    decided_at = serializers.DateTimeField()


class RedactionProposalOutputSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    advisory_run = serializers.UUIDField()
    record_item = serializers.UUIDField()
    page_number = serializers.IntegerField()
    x0 = serializers.FloatField()
    y0 = serializers.FloatField()
    x1 = serializers.FloatField()
    y1 = serializers.FloatField()
    exemption = serializers.IntegerField()
    citation = serializers.CharField()
    exemption_name = serializers.CharField()
    requires_second_review = serializers.BooleanField()
    confidence = serializers.FloatField()
    rationale = serializers.CharField()
    source_span_ids = serializers.ListField(child=serializers.CharField())
    source_text_sha256 = serializers.CharField()
    created_at = serializers.DateTimeField()
    decision = RedactionProposalDecisionOutputSerializer(allow_null=True)


class RedactionAdvisoryLatestSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    latest = AdvisoryRunSerializer(allow_null=True)
    proposals = RedactionProposalOutputSerializer(many=True)


def _truthy_flag(value) -> bool:
    """Read a query-string boolean the way a URL actually carries one."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


#: Clusters returned at once; the rest are counted in ``cluster_count``.
CLUSTER_LIMIT = 200


# Declared through inline_serializer: a serializer *class* attribute named
# ``label`` collides with Field.label in the type stubs.
RECORD_CLUSTERS_SCHEMA = inline_serializer(
    "RecordClusters",
    fields={
        "group_by": serializers.CharField(),
        "cluster_count": serializers.IntegerField(),
        "clusters": inline_serializer(
            "RecordCluster",
            many=True,
            fields={
                "key": serializers.CharField(allow_blank=True),
                "label": serializers.CharField(),
                "size": serializers.IntegerField(),
                "ai_majority": inline_serializer(
                    "RecordClusterMajority",
                    allow_null=True,
                    fields={
                        "label": serializers.CharField(),
                        "count": serializers.IntegerField(),
                    },
                ),
                "samples": inline_serializer(
                    "RecordClusterSample",
                    many=True,
                    fields={
                        "id": serializers.UUIDField(),
                        "original_filename": serializers.CharField(),
                        "review_state": serializers.CharField(),
                        "advisory_suggested_state": serializers.CharField(allow_null=True),
                        "advisory_confidence": serializers.FloatField(allow_null=True),
                    },
                ),
                "record_item_ids": serializers.ListField(child=serializers.UUIDField()),
            },
        ),
    },
)


class RecordItemViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                         viewsets.GenericViewSet):
    serializer_class = RecordItemSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "preview_metadata": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "preview_page": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "review": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "undo_review": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        # Any review role may bulk-review; the cap and the per-item audit row
        # are what bound the action, not a narrower role.
        "bulk_review": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "bulk_selection": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "facets": {
            StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN,
        },
        "clusters": {
            StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN,
        },
        "second_review": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "render_redactions": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "build_derivative_action": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "render_media_redactions_action": {
            StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER,
        },
        "opaque_disposition": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "stream": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "advisory_responsiveness": {
            StaffRole.RECORDS_OFFICER,
            StaffRole.REVIEWER,
        },
        "advisory_redactions": {
            StaffRole.RECORDS_OFFICER,
            StaffRole.REVIEWER,
        },
    }

    #: Sort keys a caller may ask for. A whitelist rather than passthrough so
    #: a query parameter can never order by an unindexed or private column.
    ORDERINGS = {
        "newest": ("-created_at", "-pk"),
        "oldest": ("created_at", "pk"),
        "filename": ("original_filename", "pk"),
        "-filename": ("-original_filename", "pk"),
        "largest": ("-content_length", "pk"),
        "smallest": ("content_length", "pk"),
        "sender": ("email_sender_domain", "email_from", "pk"),
        "-sender": ("-email_sender_domain", "-email_from", "pk"),
        "custodian": ("custodian", "pk"),
        "-custodian": ("-custodian", "pk"),
        "sent": ("email_sent_at", "pk"),
        "-sent": ("-email_sent_at", "pk"),
        "confidence": ("advisory_confidence", "pk"),
        "-confidence": ("-advisory_confidence", "pk"),
    }

    #: Actions that read the review filters rather than a primary key. Bulk
    #: review by filter resolves exactly the set the list shows.
    FILTERED_ACTIONS = frozenset({
        "list", "facets", "clusters", "bulk_selection", "bulk_review",
    })

    def _filter_params(self):
        # A filter-mode bulk review carries the filters in its JSON body, so
        # the decision is resolved against the same query the list ran.
        return getattr(self, "_body_filter", None) or self.request.query_params

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Read the administrator-managed counsel list once per request rather
        # than once per tag per row.
        context["counsel_domains"] = counsel_domains()
        return context

    def _advisory_annotations(self, qs):
        return annotate_latest_suggestion(qs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            # Schema introspection builds this queryset with no real request
            # and no database. It only needs the model to type the path
            # parameter, so stop before the tag annotations read the
            # administrator-managed counsel list.
            return RecordItem.objects.all()
        counsel = counsel_domains()
        qs = RecordItem.objects.select_related(
            "request",
            "reviewed_by",
            "second_reviewed_by",
            "withholding_exemption",
        ).prefetch_related(
            "withholding_exemptions", "redactions", "custodians",
        )
        qs = annotate_tags(qs, counsel=counsel)
        qs = self._advisory_annotations(qs)
        params = self._filter_params()
        request_number = params.get("request")
        if request_number:
            qs = qs.filter(request__number=request_number)
        review_state = params.get("review_state")
        if review_state:
            qs = qs.filter(review_state=review_state)
        elif self.action in self.FILTERED_ACTIONS:
            # A superseded collection's records left review on purpose. They
            # stay one filter away, and every detail action still finds them.
            qs = qs.exclude(review_state=RecordItem.ReviewState.SUPERSEDED)
        source_system = params.get("source_system")
        if source_system:
            qs = qs.filter(source_system=source_system)
        collection_task = (params.get("collection_task") or "").strip()
        if collection_task:
            if not collection_task.isdigit():
                raise serializers.ValidationError(
                    {"collection_task": ["Supply a collection task id."]}
                )
            qs = qs.filter(collection_task_id=int(collection_task))
        # Derived release filters. "release_ready" is the same condition
        # production enforces when it builds the ZIP; "unreleased" means no
        # live installment has claimed the record yet. Together they are the
        # Releases tab's ready-to-release list, so an officer never has to
        # reason about which records are eligible.
        if _truthy_flag(params.get("release_ready")):
            qs = release_ready_record_items(qs)
        if _truthy_flag(params.get("unreleased")):
            qs = unreleased_record_items(qs)
        tag = (params.get("tag") or "").strip()
        if tag:
            # "privilege" is the counsel-domain/privilege-marker queue rather
            # than a tag of its own, and filter_by_tag resolves it.
            qs = filter_by_tag(qs, tag, counsel=counsel)
        sender_domain = (
            params.get("sender_domain") or ""
        ).strip()
        if sender_domain:
            qs = qs.filter(email_sender_domain__iexact=sender_domain)
        custodian = (params.get("custodian") or "").strip()
        if custodian:
            # Match the recorded custodian set, not just the primary name, so a
            # deduplicated message is found under every mailbox it was in.
            qs = qs.filter(
                models.Q(custodians__custodian__iexact=custodian)
                | models.Q(custodian__iexact=custodian)
            ).distinct()
        advisory_state = (
            params.get("advisory_state") or ""
        ).strip()
        if advisory_state:
            qs = qs.filter(advisory_suggested_state=advisory_state)
        confidence_min = params.get("confidence_min")
        if confidence_min not in (None, ""):
            try:
                threshold = float(confidence_min)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"confidence_min": ["Supply a number between 0 and 1."]}
                ) from None
            if not 0 <= threshold <= 1:
                raise serializers.ValidationError(
                    {"confidence_min": ["Supply a number between 0 and 1."]}
                )
            qs = qs.filter(advisory_confidence__gte=threshold)
        search = (params.get("search") or "").strip()
        if search:
            # Archive expansion puts the container path in the filename, so a
            # reviewer looking for "2018 Resources" needs to match inside it.
            # The extracted text and the correspondents are searched too: with
            # thousands of messages, the only way to find one is by what it
            # says and who sent it.
            matches = (
                models.Q(original_filename__icontains=search)
                | models.Q(ocr_text__icontains=search)
            )
            if has_staff_role(self.request.user, StaffRole.RECORDS_OFFICER):
                # Searching by correspondent is searching by identity, so it
                # belongs to the role that may see the addresses at all.
                matches |= (
                    models.Q(email_from__icontains=search)
                    | models.Q(email_to__icontains=search)
                    | models.Q(email_cc__icontains=search)
                )
            qs = qs.filter(matches)
        ordering = self.ORDERINGS.get(
            params.get("ordering") or "newest",
            self.ORDERINGS["newest"],
        )
        # The correspondent address is blanked in the response for everyone but
        # a Records Officer, so it must not decide row order for them either:
        # an ordering over a hidden value is still a reading of it, one
        # comparison at a time. The sending domain is not identity and stays.
        if not has_staff_role(self.request.user, StaffRole.RECORDS_OFFICER):
            ordering = tuple(
                key for key in ordering
                if key.lstrip("-") not in RecordItemSerializer.IDENTITY_FIELDS
            )
        qs = qs.order_by(*ordering)
        handling = params.get("handling")
        if handling and self.action in self.FILTERED_ACTIONS:
            # Handling is derived from the media type rather than stored, so it
            # is filtered in Python. Restricted to the two actions that list,
            # because every other action filters this queryset further by
            # primary key, which a plain list cannot do. The queryset is
            # already narrowed to one request by the time this matters.
            return [item for item in qs if handling_for_item(item) == handling]
        return qs

    @action(detail=False, methods=["get"], url_path="facets")
    def facets(self, request):
        """Counts for the review filters, over the currently filtered set.

        Every count is taken from the same queryset the list returns, so a
        facet never promises rows a reviewer cannot then see. Long tails are
        capped: a reviewer picks from the values that actually matter.
        """
        queryset = self.get_queryset()
        if isinstance(queryset, list):
            # The handling filter resolves in Python; reduce to ids so the
            # facet aggregates still run in the database.
            queryset = RecordItem.objects.filter(
                pk__in=[item.pk for item in queryset]
            )
            queryset = annotate_tags(queryset, counsel=counsel_domains())
            queryset = self._advisory_annotations(queryset)
        tag_counts = queryset.aggregate(**{
            tag: models.Count(
                models.Case(
                    models.When(
                        **{annotation_name(tag): True},
                        then=models.F("pk"),
                    ),
                    output_field=models.UUIDField(),
                ),
                distinct=True,
            )
            for tag in TAG_NAMES
        })
        privilege_count = (
            queryset.filter(
                models.Q(**{annotation_name(PRIVILEGE_TAGS[0]): True})
                | models.Q(**{annotation_name(PRIVILEGE_TAGS[1]): True})
            )
            .distinct()
            .count()
        )
        return Response({
            "total": queryset.distinct().count(),
            "privilege": privilege_count,
            "tags": [
                {
                    "value": tag,
                    "label": TAG_LABELS[tag],
                    "count": tag_counts.get(tag, 0),
                }
                for tag in TAG_NAMES
            ],
            "sender_domains": _facet_rows(
                queryset, "email_sender_domain",
            ),
            "custodians": _facet_rows(
                queryset, "custodians__custodian",
            ),
        })

    @extend_schema(responses=RECORD_CLUSTERS_SCHEMA)
    @action(detail=False, methods=["get"], url_path="clusters")
    def clusters(self, request):
        """Group the currently filtered records into clusters (#38).

        A way to look at records, not a decision: each cluster reports its
        size, a few samples, and the majority AI suggestion inside it.
        """
        # Every filtered record is read, not just the clusters returned: the
        # review filters bound the set (one case, or a narrower filter), and a
        # cluster's size and majority are only true over all of it.
        grouping = request.query_params.get("group_by", "thread")
        queryset = self.get_queryset()
        try:
            clusters = cluster_records(
                queryset if isinstance(queryset, list) else queryset.order_by(),
                grouping,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"group_by": exc.messages}) from exc
        return Response({
            "group_by": grouping,
            "cluster_count": len(clusters),
            "clusters": clusters[:CLUSTER_LIMIT],
        })

    @action(detail=False, methods=["post"], url_path="bulk-review")
    def bulk_review(self, request):
        """Record one human responsiveness decision over a selected set.

        A human decision about records the reviewer chose and could see. This
        is not an accept-all of AI output: the model's recommendation changes
        nothing about what is written. Each record's standing suggestion is
        only stored beside the decision so agreement can be measured (#38).
        Withholding and duplicate outcomes are deliberately unavailable here
        because each needs its own citation or judgement.
        """
        # A decision batch replaces this for token callers (#43): an agent
        # proposes, the accountable person approves in the staff application.
        handoff = human_only_handoff(
            request,
            capability_id="record_items.bulk_review",
            action="Propose a review decision batch, or review in the staff application.",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        payload = BulkRecordReviewInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        if "filter" in data:
            return self._bulk_review_by_filter(request, data)
        try:
            reviewed = bulk_review_records(
                data["record_item_ids"],
                state=data["state"],
                actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response({
            "reviewed_count": len(reviewed),
            "review_state": payload.validated_data["state"],
            "record_item_ids": [str(item.pk) for item in reviewed],
        })

    def _matching_ids(self, filters):
        self._body_filter = QueryDict(mutable=True)
        self._body_filter.update(filters)
        queryset = self.get_queryset()
        if isinstance(queryset, list):
            return [item.pk for item in queryset]
        return list(queryset.order_by().values_list("pk", flat=True).distinct())

    def _bulk_review_by_filter(self, request, data):
        # A decision over everything a query matches is the accountable
        # reviewer's own, made in the staff application. Tokens may preview
        # the count (bulk-selection) but are handed off here, so the
        # capability's human_confirmation is enforced, not just declared.
        handoff = human_only_handoff(
            request,
            capability_id="record_items.bulk_review",
            action=(
                "Confirm a review outcome for every record a filter matches "
                "in the staff application."
            ),
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        filters = data["filter"]
        try:
            reviewed_count = bulk_review_confirmed_selection(
                self._matching_ids(filters),
                state=data["state"],
                actor=request.user,
                expected_count=data["expected_count"],
                selection_sha256=data["selection_sha256"],
                filter_detail=_auditable_filter(filters),
            )
        except BulkSelectionChanged as exc:
            return Response(
                {"code": "selection_changed", "detail": exc.messages[0]},
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response({
            "reviewed_count": reviewed_count,
            "review_state": data["state"],
        })

    @action(detail=False, methods=["get"], url_path="bulk-selection")
    def bulk_selection(self, request):
        """What one filter decision would change, before anyone confirms it.

        Returns the exact count and a digest of the records a decision of
        ``state`` would write; the confirmation must send both back.
        """
        state = request.query_params.get("state", "")
        filters = _bulk_filter({
            key: request.query_params.get(key, "") for key in BULK_FILTER_KEYS
        })
        try:
            selection = bulk_review_selection(
                self._matching_ids(filters), state=state,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        eligible = set(selection.eligible_ids)
        privileged = filter_by_tag(
            RecordItem.objects.filter(request__number=filters["request"]),
            "privilege",
            counsel=counsel_domains(),
        ).values_list("pk", flat=True)
        return Response({
            "matching": selection.matching,
            "eligible_count": len(selection.eligible_ids),
            "excluded": selection.excluded,
            "selection_sha256": selection.selection_sha256,
            "privilege_count": sum(1 for pk in privileged if pk in eligible),
            "max_items": BULK_REVIEW_FILTER_MAX_ITEMS,
        })

    @action(detail=True, methods=["get"], url_path="preview-metadata")
    def preview_metadata(self, request, pk=None):
        item = self.get_object()
        try:
            return Response(preview_pdf_metadata(item.pk, actor=request.user))
        except DjangoValidationError as exc:
            _raise_api_validation(exc)

    @action(detail=True, methods=["get"], url_path="preview-page")
    def preview_page(self, request, pk=None):
        item = self.get_object()
        try:
            preview = render_pdf_preview_page(
                item.pk,
                page_number=request.query_params.get("page", "1"),
                actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        response = HttpResponse(preview.content, content_type="image/png")
        response["Cache-Control"] = "no-store, private"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["X-Page-Number"] = str(preview.page_number)
        response["X-Page-Count"] = str(preview.page_count)
        response["X-Image-Width"] = str(preview.width)
        response["X-Image-Height"] = str(preview.height)
        return response

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        # A decision batch replaces this for token callers (#43): an agent
        # proposes, the accountable person approves in the staff application.
        handoff = human_only_handoff(
            request,
            capability_id="record_items.review",
            action="Propose a review decision batch, or review in the staff application.",
            required_role="reviewer",
        )
        if handoff is not None:
            return handoff
        item = self.get_object()
        payload = RecordReviewInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            item = review_record(
                item.pk,
                state=payload.validated_data["state"],
                actor=request.user,
                withholding_exemption_ids=payload.validated_data[
                    "withholding_exemption_ids"
                ],
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @extend_schema(request=None, responses=RecordItemSerializer)
    @action(detail=True, methods=["post"], url_path="undo-review")
    def undo_review(self, request, pk=None):
        """Undo the caller's own latest review decision on this record."""
        item = self.get_object()
        try:
            item = undo_review(item.pk, actor=request.user)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"], url_path="second-review")
    def second_review(self, request, pk=None):
        item = self.get_object()
        try:
            item = approve_second_review(item.pk, actor=request.user)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"], url_path="render-redactions")
    def render_redactions(self, request, pk=None):
        item = self.get_object()
        try:
            item = render_redacted_record(item.pk, actor=request.user)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["get"], url_path="stream")
    def stream(self, request, pk=None):
        """Serve one audio/video record to the staff player.

        Streamed through the application rather than by a storage link so the
        role check applies to the bytes themselves, and served with the same
        no-store, sandboxed headers as the page raster. Integrity is verified
        against the stored digest before anything is sent.
        """
        from apps.records.handling import MEDIA, handling_for_item
        from apps.records.storage import load_verified_object

        item = self.get_object()
        if handling_for_item(item) != MEDIA:
            raise serializers.ValidationError(
                {"detail": ["This record is not audio or video."]}
            )
        if item.content_length > runtime.get("PRR_MEDIA_STREAM_MAX_BYTES"):
            raise serializers.ValidationError(
                {"detail": ["This recording is too large to stream for review."]}
            )
        source = load_verified_object(
            item.storage_key,
            expected_size=item.content_length,
            expected_sha256=item.sha256,
        )
        try:
            payload = source.read()
        finally:
            source.close()
        audit(
            "record.media_streamed",
            target=str(item.pk),
            actor=request.user,
            request_number=item.request.number,
        )
        response = HttpResponse(
            payload,
            content_type=item.content_type or "application/octet-stream",
        )
        response["Cache-Control"] = "no-store, private"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response

    @action(detail=True, methods=["post"], url_path="build-derivative")
    def build_derivative_action(self, request, pk=None):
        """Render a non-PDF record into the copy a reviewer can read."""
        from apps.records.derivatives import build_derivative

        item = self.get_object()
        try:
            item = build_derivative(item.pk, actor=request.user)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"], url_path="render-media-redactions")
    def render_media_redactions_action(self, request, pk=None):
        """Burn marked intervals into a released copy of an audio/video record."""
        from apps.records.media_review import (
            MediaToolUnavailable,
            render_media_redactions,
        )

        item = self.get_object()
        try:
            item = render_media_redactions(item.pk, actor=request.user)
        except MediaToolUnavailable as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"], url_path="opaque-disposition")
    def opaque_disposition(self, request, pk=None):
        """Decide a record nothing can render, on stated human evidence."""
        from apps.records.opaque_review import record_opaque_disposition

        item = self.get_object()
        exemption = None
        exemption_id = request.data.get("withholding_exemption")
        if exemption_id:
            exemption = ExemptionCitation.objects.filter(pk=exemption_id).first()
            if exemption is None:
                raise serializers.ValidationError(
                    {"withholding_exemption": ["Unknown exemption citation."]}
                )
        try:
            record_opaque_disposition(
                item.pk,
                basis=request.data.get("basis", ""),
                reasoning=request.data.get("reasoning", ""),
                review_state=request.data.get("review_state", ""),
                actor=request.user,
                withholding_exemption=exemption,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        item.refresh_from_db()
        return Response(self.get_serializer(item).data)

    @extend_schema(
        request=None,
        responses={200: AdvisoryLatestSerializer, 202: AdvisoryRunSerializer},
    )
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="advisory-responsiveness",
    )
    def advisory_responsiveness(self, request, pk=None):
        item = self.get_object()
        if request.method == "GET":
            latest = (
                AdvisoryRun.objects.filter(
                    record_item=item,
                    kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                )
                .select_related("requested_by")
                .first()
            )
            return Response(
                {
                    "enabled": runtime.get("PRR_AI_ADVISORY_ENABLED"),
                    "latest": (
                        AdvisoryRunSerializer(latest).data
                        if latest is not None
                        else None
                    ),
                }
            )
        try:
            run = request_record_responsiveness_advisory(
                item.pk,
                actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            AdvisoryRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=None,
        responses={
            200: RedactionAdvisoryLatestSerializer,
            202: AdvisoryRunSerializer,
        },
    )
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="advisory-redactions",
    )
    def advisory_redactions(self, request, pk=None):
        item = self.get_object()
        if request.method == "GET":
            latest = (
                AdvisoryRun.objects.filter(
                    record_item=item,
                    kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
                )
                .select_related("requested_by")
                .first()
            )
            proposals = (
                RedactionProposal.objects.filter(advisory_run=latest)
                .select_related(
                    "exemption",
                    "decision__decided_by",
                    "decision__redaction",
                )
                if latest is not None
                else RedactionProposal.objects.none()
            )
            return Response(
                {
                    "enabled": runtime.get("PRR_AI_ADVISORY_ENABLED"),
                    "latest": (
                        AdvisoryRunSerializer(latest).data
                        if latest is not None
                        else None
                    ),
                    "proposals": RedactionProposalSerializer(
                        proposals,
                        many=True,
                    ).data,
                }
            )
        try:
            run = request_record_redaction_advisory(
                item.pk,
                actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            AdvisoryRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED,
        )


class RedactionSerializer(serializers.ModelSerializer):
    citation = serializers.CharField(source="exemption.citation", read_only=True)
    exemption_name = serializers.CharField(
        source="exemption.short_name",
        read_only=True,
    )
    requires_second_review = serializers.BooleanField(
        source="exemption.requires_second_review",
        read_only=True,
    )
    created_by_username = serializers.CharField(
        source="created_by.username",
        read_only=True,
        default=None,
    )
    voided_by_username = serializers.CharField(
        source="voided_by.username",
        read_only=True,
        default=None,
    )

    class Meta:
        model = Redaction
        fields = [
            "id",
            "record_item",
            "page_number",
            "x0",
            "y0",
            "x1",
            "y1",
            "exemption",
            "citation",
            "exemption_name",
            "requires_second_review",
            "explanation",
            "created_by_username",
            "created_at",
            "voided_by_username",
            "voided_at",
            "void_reason",
        ]
        read_only_fields = fields


class RedactionCreateSerializer(serializers.Serializer):
    record_item = serializers.UUIDField()
    page_number = serializers.IntegerField(min_value=1)
    x0 = serializers.FloatField(min_value=0.0, max_value=1.0)
    y0 = serializers.FloatField(min_value=0.0, max_value=1.0)
    x1 = serializers.FloatField(min_value=0.0, max_value=1.0)
    y1 = serializers.FloatField(min_value=0.0, max_value=1.0)
    exemption = serializers.IntegerField(min_value=1)
    explanation = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
    )


class RedactionVoidSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=10, max_length=500)


class RedactionProposalDecisionSerializer(serializers.ModelSerializer):
    decided_by_username = serializers.CharField(
        source="decided_by.username",
        read_only=True,
    )

    class Meta:
        model = RedactionProposalDecision
        fields = [
            "outcome",
            "decided_by_username",
            "reason",
            "redaction",
            "decided_at",
        ]
        read_only_fields = fields


class RedactionProposalSerializer(serializers.ModelSerializer):
    citation = serializers.CharField(source="exemption.citation", read_only=True)
    exemption_name = serializers.CharField(
        source="exemption.short_name",
        read_only=True,
    )
    requires_second_review = serializers.BooleanField(
        source="exemption.requires_second_review",
        read_only=True,
    )
    decision = serializers.SerializerMethodField()

    class Meta:
        model = RedactionProposal
        fields = [
            "id",
            "advisory_run",
            "record_item",
            "page_number",
            "x0",
            "y0",
            "x1",
            "y1",
            "exemption",
            "citation",
            "exemption_name",
            "requires_second_review",
            "confidence",
            "rationale",
            "source_span_ids",
            "source_text_sha256",
            "created_at",
            "decision",
        ]
        read_only_fields = fields

    @extend_schema_field(RedactionProposalDecisionSerializer(allow_null=True))
    def get_decision(self, obj):
        try:
            decision = obj.decision
        except RedactionProposalDecision.DoesNotExist:
            return None
        return RedactionProposalDecisionSerializer(decision).data


class RedactionProposalAcceptSerializer(serializers.Serializer):
    page_number = serializers.IntegerField(min_value=1)
    x0 = serializers.FloatField(min_value=0.0, max_value=1.0)
    y0 = serializers.FloatField(min_value=0.0, max_value=1.0)
    x1 = serializers.FloatField(min_value=0.0, max_value=1.0)
    y1 = serializers.FloatField(min_value=0.0, max_value=1.0)
    exemption = serializers.IntegerField(min_value=1)
    explanation = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
    )


class RedactionProposalRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=10, max_length=500)


class RedactionProposalViewSet(
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = RedactionProposalSerializer
    role_permissions = {
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "accept": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "reject": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
    }

    def get_queryset(self):
        return RedactionProposal.objects.select_related(
            "advisory_run",
            "record_item__request",
            "exemption",
            "decision__decided_by",
            "decision__redaction",
        )

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        proposal = self.get_object()
        payload = RedactionProposalAcceptSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            accept_redaction_proposal(
                proposal.pk,
                actor=request.user,
                page_number=data["page_number"],
                x0=data["x0"],
                y0=data["y0"],
                x1=data["x1"],
                y1=data["y1"],
                exemption_id=data["exemption"],
                explanation=data.get("explanation", ""),
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(self.get_object()).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        proposal = self.get_object()
        payload = RedactionProposalRejectSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            reject_redaction_proposal(
                proposal.pk,
                actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(self.get_object()).data)


class RedactionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = RedactionSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "create": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "void": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
    }

    def get_queryset(self):
        queryset = Redaction.objects.select_related(
            "record_item__request",
            "exemption",
            "created_by",
            "voided_by",
        ).order_by("record_item_id", "page_number", "pk")
        record_item = self.request.query_params.get("record_item")
        if record_item:
            queryset = queryset.filter(record_item_id=record_item)
        if self.request.query_params.get("include_voided", "").lower() != "true":
            queryset = queryset.filter(voided_at__isnull=True)
        return queryset

    def create(self, request, *args, **kwargs):
        payload = RedactionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            redaction = create_redaction(
                data["record_item"],
                actor=request.user,
                page_number=data["page_number"],
                x0=data["x0"],
                y0=data["y0"],
                x1=data["x1"],
                y1=data["y1"],
                exemption_id=data["exemption"],
                explanation=data.get("explanation", ""),
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(redaction).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        redaction = self.get_object()
        payload = RedactionVoidSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            redaction = void_redaction(
                redaction.pk,
                actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(redaction).data)


class FeeScheduleSerializer(serializers.ModelSerializer):
    activated_by_username = serializers.CharField(
        source="activated_by.username",
        read_only=True,
    )
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = FeeSchedule
        fields = [
            "id",
            "name",
            "basis",
            "effective_on",
            "counsel_approval_reference",
            "adoption_reference",
            "actual_cost_statement_reference",
            "photocopy_page_rate",
            "scanned_page_rate",
            "electronic_file_bundle_rate",
            "electronic_gb_rate",
            "flat_fee_amount",
            "supersedes",
            "activated_by_username",
            "activated_at",
            "is_current",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_is_current(self, obj):
        current = current_fee_schedule()
        return current is not None and current.pk == obj.pk


class FeeScheduleCreateSerializer(serializers.Serializer):
    name = serializers.CharField(min_length=4, max_length=200)
    basis = serializers.ChoiceField(choices=FeeSchedule.Basis.choices)
    effective_on = serializers.DateField()
    counsel_approval_reference = serializers.CharField(max_length=500)
    adoption_reference = serializers.CharField(max_length=500)
    actual_cost_statement_reference = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
    )
    photocopy_page_rate = serializers.DecimalField(
        max_digits=8, decimal_places=4, required=False, default=Decimal("0.15")
    )
    scanned_page_rate = serializers.DecimalField(
        max_digits=8, decimal_places=4, required=False, default=Decimal("0.10")
    )
    electronic_file_bundle_rate = serializers.DecimalField(
        max_digits=8, decimal_places=4, required=False, default=Decimal("0.05")
    )
    electronic_gb_rate = serializers.DecimalField(
        max_digits=8, decimal_places=4, required=False, default=Decimal("0.10")
    )
    flat_fee_amount = serializers.DecimalField(
        max_digits=4,
        decimal_places=2,
        required=False,
        allow_null=True,
        default=Decimal("2.00"),
    )


class FeeScheduleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = FeeSchedule.objects.select_related(
        "activated_by",
        "supersedes",
    ).order_by("-effective_on", "-activated_at")
    serializer_class = FeeScheduleSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "create": {StaffRole.RECORDS_OFFICER},
    }

    def create(self, request, *args, **kwargs):
        payload = FeeScheduleCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            schedule = activate_fee_schedule(
                actor=request.user,
                **payload.validated_data,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(schedule).data,
            status=status.HTTP_201_CREATED,
        )


#: Sentinel for "the adopted waiver floor has not been looked up yet", so a
#: cached ``None`` is not mistaken for a cache miss.
_NO_FLOOR = object()
#: Where the per-request adopted-floor lookup is memoized in serializer context.
_FLOOR_CACHE_KEY = "_current_fee_waiver_floor"


class FeeWaiverFloorSerializer(serializers.ModelSerializer):
    activated_by_username = serializers.CharField(
        source="activated_by.username",
        read_only=True,
    )
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = FeeWaiverFloor
        fields = [
            "id",
            "amount",
            "counsel_reference",
            "effective_on",
            "supersedes",
            "activated_by_username",
            "activated_at",
            "is_current",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_is_current(self, obj):
        current = current_fee_waiver_floor()
        return current is not None and current.pk == obj.pk


class FeeWaiverFloorCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    counsel_reference = serializers.CharField(min_length=8, max_length=500)
    effective_on = serializers.DateField()


class FeeWaiverFloorViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """The adopted amount under which an officer is prompted to waive.

    Append-only like the fee schedule it sits beside: a floor is superseded,
    never edited, so the counsel reference that adopted each one survives.
    """

    queryset = FeeWaiverFloor.objects.select_related(
        "activated_by",
        "supersedes",
    ).order_by("-effective_on", "-activated_at")
    serializer_class = FeeWaiverFloorSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {
            StaffRole.RECORDS_OFFICER,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        },
        "create": {StaffRole.RECORDS_OFFICER},
    }

    def create(self, request, *args, **kwargs):
        payload = FeeWaiverFloorCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            floor = activate_fee_waiver_floor(
                actor=request.user,
                **payload.validated_data,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(floor).data,
            status=status.HTTP_201_CREATED,
        )


class FeeDepositGateSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(
        source="request.number",
        read_only=True,
    )
    applied_by_username = serializers.CharField(
        source="applied_by.username",
        read_only=True,
    )
    satisfied_by_username = serializers.CharField(
        source="satisfied_by.username",
        read_only=True,
        default=None,
    )
    lifted_by_username = serializers.CharField(
        source="lifted_by.username",
        read_only=True,
        default=None,
    )
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = FeeDepositGate
        fields = [
            "id",
            "request_number",
            "scope",
            "amount",
            "estimated_total",
            "reason",
            "requested_on",
            "applied_by_username",
            "applied_at",
            "satisfied_at",
            "satisfied_by_username",
            "payment_amount",
            "payment_method",
            "payment_reference",
            "payment_received_on",
            "lifted_at",
            "lifted_by_username",
            "lift_reason",
            "is_open",
        ]
        read_only_fields = fields


class FeeDepositGateCreateSerializer(serializers.Serializer):
    request_number = serializers.CharField(max_length=20)
    scope = serializers.ChoiceField(choices=FeeDepositGate.Scope.choices)
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    estimated_total = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    reason = serializers.CharField(min_length=10, max_length=1000)
    requested_on = serializers.DateField(required=False)


class FeeDepositPaymentInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    method = serializers.ChoiceField(choices=FeePayment.Method.choices)
    reference = serializers.CharField(min_length=5, max_length=500)
    received_on = serializers.DateField()


class FeeDepositGateLiftSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=10, max_length=1000)


class FeeDepositGateViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Deposits the District requires before it collects or produces.

    A gate never closes a case and never expires. It ends when the deposit is
    recorded or when an officer lifts it with a written reason.
    """

    queryset = FeeDepositGate.objects.select_related(
        "request",
        "applied_by",
        "satisfied_by",
        "lifted_by",
    ).order_by("-applied_at")
    serializer_class = FeeDepositGateSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {
            StaffRole.RECORDS_OFFICER,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
        },
        "create": {StaffRole.RECORDS_OFFICER},
        "record_payment": {StaffRole.RECORDS_OFFICER},
        "lift": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        number = self.request.query_params.get("request")
        if number:
            queryset = queryset.filter(request__number=number)
        if self.request.query_params.get("open") == "true":
            queryset = queryset.filter(
                satisfied_at__isnull=True,
                lifted_at__isnull=True,
            )
        return queryset

    def create(self, request, *args, **kwargs):
        payload = FeeDepositGateCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        number = data.pop("request_number")
        try:
            gate = apply_deposit_gate(number, actor=request.user, **data)
        except PublicRecordsRequest.DoesNotExist:
            raise serializers.ValidationError(
                {"request_number": "That public records request was not found."}
            ) from None
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(gate).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="record-payment")
    @extend_schema(
        request=FeeDepositPaymentInputSerializer,
        responses={200: FeeDepositGateSerializer},
    )
    def record_payment(self, request, pk=None):
        payload = FeeDepositPaymentInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        gate = self.get_object()
        try:
            gate = record_deposit_payment(
                gate.pk,
                actor=request.user,
                **payload.validated_data,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(gate).data)

    @action(detail=True, methods=["post"], url_path="lift")
    @extend_schema(
        request=FeeDepositGateLiftSerializer,
        responses={200: FeeDepositGateSerializer},
    )
    def lift(self, request, pk=None):
        payload = FeeDepositGateLiftSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        gate = self.get_object()
        try:
            gate = lift_deposit_gate(
                gate.pk,
                actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(gate).data)


class FeeAssessmentSerializer(serializers.ModelSerializer):
    schedule_name = serializers.CharField(source="schedule.name", read_only=True)
    created_by_username = serializers.CharField(
        source="created_by.username",
        read_only=True,
    )
    waiver_floor_amount = serializers.SerializerMethodField()
    below_waiver_floor = serializers.SerializerMethodField()

    def _floor(self):
        """Read the adopted floor once per request, not once per row.

        The effective floor is a property of today, not of the assessment. A
        fresh ``FeeAssessmentSerializer`` is built for each installment in a
        list, so the cache lives in the shared serializer context rather than
        on the instance. ``_NO_FLOOR`` distinguishes "not looked up yet" from
        "looked up and there is none".
        """
        cache = self.context if isinstance(self.context, dict) else {}
        cached = cache.get(_FLOOR_CACHE_KEY, _NO_FLOOR)
        if cached is _NO_FLOOR:
            cached = current_fee_waiver_floor()
            cache[_FLOOR_CACHE_KEY] = cached
        return cached

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_waiver_floor_amount(self, obj):
        floor = self._floor()
        return None if floor is None else f"{floor.amount:.2f}"

    @extend_schema_field(serializers.BooleanField())
    def get_below_waiver_floor(self, obj):
        floor = self._floor()
        if floor is None:
            return False
        return assessment_is_below_waiver_floor(obj, floor=floor)

    class Meta:
        model = FeeAssessment
        fields = [
            "id",
            "waiver_floor_amount",
            "below_waiver_floor",
            "revision",
            "schedule",
            "schedule_name",
            "photocopy_pages",
            "scanned_pages",
            "electronic_files",
            "electronic_gb",
            "media_cost",
            "postage_cost",
            "customized_service_cost",
            "customized_service_explanation",
            "customized_service_expertise",
            "requester_notice_reference",
            "requester_approval_reference",
            "itemized_amount",
            "use_flat_fee",
            "covered_by_request_flat_fee",
            "waiver_amount",
            "waiver_reason",
            "amount",
            "deposit_percent",
            "deposit_amount",
            "reason",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = fields


class FeeAssessmentInputSerializer(serializers.Serializer):
    schedule_id = serializers.UUIDField()
    photocopy_pages = serializers.IntegerField(min_value=0, required=False, default=0)
    scanned_pages = serializers.IntegerField(min_value=0, required=False, default=0)
    electronic_files = serializers.IntegerField(min_value=0, required=False, default=0)
    electronic_gb = serializers.DecimalField(
        max_digits=16, decimal_places=6, required=False, default=0
    )
    media_cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    postage_cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    customized_service_cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    customized_service_explanation = serializers.CharField(
        required=False, allow_blank=True, max_length=2000
    )
    customized_service_expertise = serializers.CharField(
        required=False, allow_blank=True, max_length=500
    )
    requester_notice_reference = serializers.CharField(
        required=False, allow_blank=True, max_length=500
    )
    requester_approval_reference = serializers.CharField(
        required=False, allow_blank=True, max_length=500
    )
    use_flat_fee = serializers.BooleanField(required=False, default=False)
    waiver_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    waiver_reason = serializers.CharField(
        required=False, allow_blank=True, max_length=1000
    )
    deposit_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=0
    )
    reason = serializers.CharField(min_length=10, max_length=1000)


class FeePaymentInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=FeePayment.Method.choices)
    reference = serializers.CharField(min_length=5, max_length=500)
    received_on = serializers.DateField()


class InstallmentSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    item_count = serializers.IntegerField(source="items.count", read_only=True)
    package_prepared_by_username = serializers.CharField(
        source="package_prepared_by.username",
        read_only=True,
        default=None,
    )
    exemption_log_prepared_by_username = serializers.CharField(
        source="exemption_log_prepared_by.username",
        read_only=True,
        default=None,
    )
    revoked_by_username = serializers.CharField(
        source="revoked_by.username",
        read_only=True,
        default=None,
    )
    #: Whether a code is required, never the code or its hash. The clear-text
    #: code exists only in the separate message the officer sent.
    requires_access_code = serializers.SerializerMethodField()
    record_item_ids = serializers.SerializerMethodField()
    contents_locked = serializers.SerializerMethodField()
    exemption_log_ready = serializers.SerializerMethodField()
    has_exemptions = serializers.SerializerMethodField()
    fee_assessment = serializers.SerializerMethodField()
    fee_paid_amount = serializers.SerializerMethodField()
    fee_balance = serializers.SerializerMethodField()

    class Meta:
        model = Installment
        fields = [
            "id", "request_number", "sequence", "fee_amount", "fee_paid_at",
            "released_at", "download_expires_at", "item_count",
            "package_prepared_at", "package_prepared_by_username",
            "package_size",
            "download_count",
            "last_downloaded_at",
            "requires_access_code",
            "access_code_set_at",
            "revoked_at",
            "revoked_by_username",
            "revocation_reason",
            "record_item_ids",
            "contents_locked",
            "exemption_log_prepared_at",
            "exemption_log_prepared_by_username",
            "exemption_log_ready",
            "has_exemptions",
            "fee_assessment",
            "fee_paid_amount",
            "fee_balance",
        ]

    @extend_schema_field(serializers.BooleanField())
    def get_requires_access_code(self, obj):
        return bool(obj.access_code_hash)

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_record_item_ids(self, obj):
        return [str(item.pk) for item in obj.items.all()]

    @extend_schema_field(serializers.BooleanField())
    def get_contents_locked(self, obj):
        return obj.package_prepared_at is not None or obj.released_at is not None

    @extend_schema_field(serializers.BooleanField())
    def get_exemption_log_ready(self, obj):
        return bool(
            obj.exemption_log_storage_key
            and obj.exemption_log_sha256
            and obj.exemption_log_size is not None
            and obj.exemption_log_manifest_sha256
            and obj.exemption_log_prepared_at is not None
        )

    @extend_schema_field(serializers.BooleanField())
    def get_has_exemptions(self, obj):
        return obj.items.filter(
            models.Q(review_state=RecordItem.ReviewState.WITHHELD)
            | models.Q(
                redactions__isnull=False,
                redactions__voided_at__isnull=True,
            )
            | models.Q(withholding_exemptions__isnull=False)
        ).exists()

    def _assessment(self, obj):
        cached = getattr(obj, "_current_fee_assessment", None)
        if cached is not None:
            return cached
        assessment = (
            FeeAssessment.objects.select_related("schedule", "created_by")
            .filter(installment=obj, superseded_by__isnull=True)
            .order_by("-revision")
            .first()
        )
        obj._current_fee_assessment = assessment
        return assessment

    @extend_schema_field(FeeAssessmentSerializer(allow_null=True))
    def get_fee_assessment(self, obj):
        assessment = self._assessment(obj)
        return (
            FeeAssessmentSerializer(assessment, context=self.context).data
            if assessment is not None
            else None
        )

    def _paid_amount(self, obj):
        cached = getattr(obj, "_fee_paid_amount", None)
        if cached is not None:
            return cached
        value = obj.fee_payments.aggregate(total=models.Sum("amount"))["total"]
        obj._fee_paid_amount = value or Decimal("0.00")
        return obj._fee_paid_amount

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_fee_paid_amount(self, obj):
        return f"{self._paid_amount(obj):.2f}"

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_fee_balance(self, obj):
        assessed = Decimal(str(obj.fee_amount))
        return f"{max(Decimal('0.00'), assessed - self._paid_amount(obj)):.2f}"


class InstallmentReleaseSerializer(serializers.Serializer):
    is_final = serializers.BooleanField()


class InstallmentCreateSerializer(serializers.Serializer):
    request_number = serializers.CharField(max_length=40)
    record_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
    )


class InstallmentItemsSerializer(serializers.Serializer):
    record_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
    )


class InstallmentRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=10, max_length=1000)


class InstallmentAccessCodeSerializer(serializers.Serializer):
    # Length and strength rules live in the service, which reads the
    # configured minimum; a second floor here would only ever disagree.
    access_code = serializers.CharField(max_length=128)


class InstallmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    queryset = Installment.objects.select_related(
        "request",
        "package_prepared_by",
        "exemption_log_prepared_by",
        "revoked_by",
    ).prefetch_related("items").order_by("request__number", "sequence")
    serializer_class = InstallmentSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER, StaffRole.ADMIN},
        "create": {StaffRole.RECORDS_OFFICER},
        "set_items": {StaffRole.RECORDS_OFFICER},
        "prepare": {StaffRole.RECORDS_OFFICER},
        "generate_exemption_log": {StaffRole.RECORDS_OFFICER},
        "assess_fee": {StaffRole.RECORDS_OFFICER},
        "record_payment": {StaffRole.RECORDS_OFFICER},
        "release": {StaffRole.RECORDS_OFFICER},
        "release_and_send": {StaffRole.RECORDS_OFFICER},
        "revoke": {StaffRole.RECORDS_OFFICER},
        "set_access_code": {StaffRole.RECORDS_OFFICER},
        "clear_access_code": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        request_number = self.request.query_params.get("request", "").strip()
        if request_number:
            queryset = queryset.filter(request__number=request_number)
        return queryset

    @extend_schema(
        request=InstallmentCreateSerializer,
        responses={201: InstallmentSerializer},
    )
    def create(self, request):
        """Open the next installment for a request from selected records."""
        payload = InstallmentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            installment = create_installment(
                payload.validated_data["request_number"],
                actor=request.user,
                item_ids=payload.validated_data["record_item_ids"],
            )
        except PublicRecordsRequest.DoesNotExist:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(installment).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="set-items")
    @extend_schema(
        request=InstallmentItemsSerializer,
        responses={200: InstallmentSerializer},
    )
    def set_items(self, request, pk=None):
        """Replace this installment's contents while it is still open."""
        return self._apply(
            request,
            InstallmentItemsSerializer,
            lambda pk, data: set_installment_items(
                pk, actor=request.user, item_ids=data["record_item_ids"],
            ),
        )

    def _apply(self, request, serializer_class, call):
        """Validate, apply one service call, and answer with the fresh row.

        Every installment action that mutates and then shows the result has
        this shape; ``call`` receives the installment id and the validated
        payload (an empty dict when the action takes no body).
        """
        installment_id = self.get_object().pk
        data = {}
        if serializer_class is not None:
            payload = serializer_class(data=request.data)
            payload.is_valid(raise_exception=True)
            data = payload.validated_data
        try:
            call(installment_id, data)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        installment = self.get_queryset().get(pk=installment_id)
        return Response(self.get_serializer(installment).data)

    @action(detail=True, methods=["post"], url_path="release-and-send")
    @extend_schema(
        request=InstallmentReleaseSerializer,
        responses={200: InstallmentSerializer},
    )
    def release_and_send(self, request, pk=None):
        """Release the package and send its notice in one officer action."""
        handoff = human_only_handoff(
            request,
            capability_id="installments.release_and_send",
            action=(
                "Release the verified package to the requester and send the "
                "installment notice."
            ),
        )
        if handoff:
            return handoff
        installment_id = self.get_object().pk
        payload = InstallmentReleaseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            installment, download_url, notice = (
                release_installment_and_send_notice(
                    installment_id,
                    actor=request.user,
                    is_final=payload.validated_data["is_final"],
                )
            )
        except InstallmentNoticeUnsent as exc:
            # Not a 400: the release committed. The row is returned so the
            # screen can show it released with its notice still a draft.
            installment = self.get_queryset().get(pk=installment_id)
            return Response(
                {
                    **self.get_serializer(installment).data,
                    "code": "installment_notice_unsent",
                    "detail": " ".join(exc.messages),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        installment = self.get_queryset().get(pk=installment.pk)
        return Response({
            **self.get_serializer(installment).data,
            "download_url": download_url,
            "correspondence_id": str(notice.pk),
            "correspondence_status": notice.status,
        })

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=InstallmentRevokeSerializer,
        responses={200: InstallmentSerializer},
    )
    def revoke(self, request, pk=None):
        """End requester access to a released package, with a reason."""
        handoff = human_only_handoff(
            request,
            capability_id="installments.revoke",
            action="Withdraw the requester's access to a released package.",
        )
        if handoff:
            return handoff
        return self._apply(
            request,
            InstallmentRevokeSerializer,
            lambda pk, data: revoke_installment(
                pk, actor=request.user, reason=data["reason"],
            ),
        )

    @action(detail=True, methods=["post"], url_path="set-access-code")
    @extend_schema(
        request=InstallmentAccessCodeSerializer,
        responses={200: InstallmentSerializer},
    )
    def set_access_code(self, request, pk=None):
        """Require a separately communicated code on this download."""
        return self._apply(
            request,
            InstallmentAccessCodeSerializer,
            lambda pk, data: set_installment_access_code(
                pk, actor=request.user, access_code=data["access_code"],
            ),
        )

    @action(detail=True, methods=["post"], url_path="clear-access-code")
    @extend_schema(request=None, responses={200: InstallmentSerializer})
    def clear_access_code(self, request, pk=None):
        """Drop the access code, for instance when a requester loses it."""
        return self._apply(
            request,
            None,
            lambda pk, data: clear_installment_access_code(
                pk, actor=request.user,
            ),
        )

    @action(detail=True, methods=["post"])
    def prepare(self, request, pk=None):
        installment_id = self.get_object().pk
        try:
            installment = prepare_installment(installment_id, actor=request.user)
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(installment).data)

    @action(detail=True, methods=["post"], url_path="generate-exemption-log")
    def generate_exemption_log(self, request, pk=None):
        installment_id = self.get_object().pk
        try:
            installment = generate_exemption_log(
                installment_id,
                actor=request.user,
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(installment).data)

    @action(detail=True, methods=["post"], url_path="assess-fee")
    def assess_fee(self, request, pk=None):
        return self._apply(
            request,
            FeeAssessmentInputSerializer,
            lambda pk, data: assess_installment_fee(
                pk, actor=request.user, **data,
            ),
        )

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        return self._apply(
            request,
            FeePaymentInputSerializer,
            lambda pk, data: record_fee_payment(
                pk, actor=request.user, **data,
            ),
        )

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        installment_id = self.get_object().pk
        payload = InstallmentReleaseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            installment, download_url, notice = release_installment_with_notice(
                installment_id,
                actor=request.user,
                is_final=payload.validated_data["is_final"],
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response({
            **self.get_serializer(installment).data,
            "download_url": download_url,
            "correspondence_id": str(notice.pk),
        })


class TimeEntrySerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    staff_username = serializers.CharField(source="staff.username", read_only=True)

    class Meta:
        model = TimeEntry
        fields = ["id", "request", "request_number", "staff_username",
                  "date", "minutes", "activity"]


class TimeEntryViewSet(viewsets.ModelViewSet):
    queryset = TimeEntry.objects.select_related("request", "staff").order_by("-date")
    serializer_class = TimeEntrySerializer
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "create": WORKFLOW_STAFF_ROLES,
        "update": {StaffRole.RECORDS_OFFICER},
        "partial_update": {StaffRole.RECORDS_OFFICER},
        "destroy": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        qs = super().get_queryset()
        if not has_staff_role(self.request.user, StaffRole.RECORDS_OFFICER, StaffRole.ADMIN):
            qs = qs.filter(staff=self.request.user)
        request_number = self.request.query_params.get("request")
        if request_number:
            qs = qs.filter(request__number=request_number)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        with transaction.atomic():
            prr = PublicRecordsRequest.objects.select_for_update().get(
                pk=serializer.validated_data["request"].pk
            )
            if (
                has_staff_role(user, StaffRole.LIAISON)
                and not has_staff_role(user, StaffRole.RECORDS_OFFICER)
                and not CollectionTask.objects.select_for_update().filter(
                    request=prr,
                    assigned_to=user,
                ).exists()
            ):
                raise serializers.ValidationError(
                    "Liaisons may log time only to requests assigned to them."
                )
            serializer.save(staff=user, request=prr)


class MediaRedactionSerializer(serializers.ModelSerializer):
    citation = serializers.CharField(source="exemption.citation", read_only=True)
    exemption_name = serializers.CharField(
        source="exemption.short_name", read_only=True
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )

    class Meta:
        model = MediaRedaction
        fields = [
            "id", "record_item", "starts_at", "ends_at", "treatment",
            "x0", "y0", "x1", "y1", "exemption", "citation", "exemption_name",
            "explanation", "created_by_username", "created_at",
            "voided_at", "void_reason",
        ]
        read_only_fields = fields


class MediaRedactionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Timed withholdings over an audio or video record."""

    serializer_class = MediaRedactionSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "create": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
        "void": {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER},
    }

    def get_queryset(self):
        queryset = MediaRedaction.objects.select_related(
            "record_item__request", "exemption", "created_by", "voided_by",
        ).order_by("record_item_id", "starts_at", "pk")
        record_item = self.request.query_params.get("record_item")
        if record_item:
            queryset = queryset.filter(record_item_id=record_item)
        if self.request.query_params.get("include_voided", "").lower() != "true":
            queryset = queryset.filter(voided_at__isnull=True)
        return queryset

    def create(self, request, *args, **kwargs):
        from apps.records.media_review import create_media_redaction

        try:
            entry = create_media_redaction(
                request.data.get("record_item"),
                actor=request.user,
                starts_at=request.data.get("starts_at"),
                ends_at=request.data.get("ends_at"),
                treatment=request.data.get("treatment", ""),
                exemption_id=request.data.get("exemption"),
                explanation=request.data.get("explanation", ""),
                x0=request.data.get("x0"),
                y0=request.data.get("y0"),
                x1=request.data.get("x1"),
                y1=request.data.get("y1"),
            )
        except RecordItem.DoesNotExist as exc:
            raise serializers.ValidationError(
                {"record_item": ["Unknown record item."]}
            ) from exc
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(
            self.get_serializer(entry).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        from apps.records.media_review import void_media_redaction

        try:
            entry = void_media_redaction(
                pk, actor=request.user, reason=request.data.get("reason", "")
            )
        except DjangoValidationError as exc:
            _raise_api_validation(exc)
        return Response(self.get_serializer(entry).data)


def register(router):
    router.register("requesters", RequesterViewSet, basename="requester")
    router.register("record-items", RecordItemViewSet, basename="record-item")
    router.register("redactions", RedactionViewSet, basename="redaction")
    router.register(
        "media-redactions", MediaRedactionViewSet, basename="media-redaction",
    )
    router.register(
        "redaction-proposals",
        RedactionProposalViewSet,
        basename="redaction-proposal",
    )
    router.register("fee-schedules", FeeScheduleViewSet, basename="fee-schedule")
    router.register(
        "fee-waiver-floors",
        FeeWaiverFloorViewSet,
        basename="fee-waiver-floor",
    )
    router.register(
        "fee-deposit-gates",
        FeeDepositGateViewSet,
        basename="fee-deposit-gate",
    )
    router.register("installments", InstallmentViewSet, basename="installment")
    router.register("time-entries", TimeEntryViewSet, basename="time-entry")
