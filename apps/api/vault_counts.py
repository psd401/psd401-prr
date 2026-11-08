"""Staff-safe serializers for count-only Google Vault searches."""

from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.records.models import CollectionTask, VaultCountRun
from apps.records.vault_collections import KIND_SOURCES
from apps.records.vault_counts import (
    collectable_sources,
    run_sources,
    source_count_summary,
)

#: One source's count: ``counting``, ``counted`` with a number, or
#: ``unavailable`` with a plain reason. Declared inline because ``source`` and
#: ``label`` are attribute names on a DRF field.
VAULT_SOURCE_COUNT_SCHEMA = inline_serializer(
    name="VaultSourceCount",
    fields={
        "source": serializers.ChoiceField(choices=["mail", "drive", "chat"]),
        "label": serializers.CharField(),
        "state": serializers.ChoiceField(choices=["counting", "counted", "unavailable"]),
        "count": serializers.IntegerField(allow_null=True),
        "reason": serializers.CharField(allow_blank=True),
    },
    many=True,
)
VAULT_COLLECTION_TASK_SCHEMA = inline_serializer(
    name="VaultCollectionTaskSummary",
    fields={
        "id": serializers.IntegerField(),
        "kind": serializers.CharField(),
        "source": serializers.CharField(),
        "state": serializers.CharField(),
    },
    many=True,
)


class VaultCountRunSerializer(serializers.ModelSerializer):
    requested_by_username = serializers.CharField(
        source="requested_by.username",
        read_only=True,
        default=None,
        allow_null=True,
    )
    collection_task_id = serializers.SerializerMethodField()
    collection_task_state = serializers.SerializerMethodField()
    collection_task_retryable = serializers.SerializerMethodField()
    collection_tasks = serializers.SerializerMethodField()
    source_counts = serializers.SerializerMethodField()
    collectable = serializers.SerializerMethodField()
    sources = serializers.SerializerMethodField()

    def get_sources(self, obj) -> list[str]:
        """The sources searched; a count from before Drive and Chat is Gmail."""
        return run_sources(obj)

    def _collection_tasks(self, obj) -> list:
        """One task per source, Gmail then Drive then Chat."""
        order = {kind: index for index, kind in enumerate(CollectionTask.VAULT_KINDS)}
        return sorted(
            obj.collection_tasks.all(),
            key=lambda task: (order.get(task.kind, len(order)), task.pk),
        )

    def get_collection_task_id(self, obj) -> int | None:
        tasks = self._collection_tasks(obj)
        return tasks[0].pk if tasks else None

    def get_collection_task_state(self, obj) -> str | None:
        """The collection's state across its sources.

        Failed if any source failed; complete once every source is; otherwise
        the furthest-behind source still working.
        """
        states = [task.state for task in self._collection_tasks(obj)]
        if not states:
            return None
        if CollectionTask.State.FAILED in states:
            return CollectionTask.State.FAILED
        for state in (
            CollectionTask.State.PENDING,
            CollectionTask.State.RUNNING,
            CollectionTask.State.INGESTING,
        ):
            if state in states:
                return state
        return CollectionTask.State.COMPLETE

    def get_collection_task_retryable(self, obj) -> bool:
        return any(
            task.state == CollectionTask.State.FAILED and not task.vault_export_id
            for task in self._collection_tasks(obj)
        )

    @extend_schema_field(VAULT_COLLECTION_TASK_SCHEMA)
    def get_collection_tasks(self, obj) -> list[dict]:
        return [
            {
                "id": task.pk,
                "kind": task.kind,
                "source": KIND_SOURCES[task.kind],
                "state": task.state,
            }
            for task in self._collection_tasks(obj)
            if task.kind in KIND_SOURCES
        ]

    @extend_schema_field(VAULT_SOURCE_COUNT_SCHEMA)
    def get_source_counts(self, obj) -> list[dict]:
        return source_count_summary(obj)

    def get_collectable(self, obj) -> bool:
        return obj.state == VaultCountRun.State.COMPLETE and bool(collectable_sources(obj))

    class Meta:
        model = VaultCountRun
        fields = [
            "id",
            "state",
            "corpus",
            "search_scope",
            "terms",
            "custodian_emails",
            "date_range_start",
            "date_range_end",
            "input_sha256",
            "trigger",
            "requested_by_username",
            "collection_task_id",
            "collection_task_state",
            "collection_task_retryable",
            "collection_tasks",
            "sources",
            "source_counts",
            "collectable",
            "total_count",
            "queried_accounts_count",
            "matching_accounts_count",
            "non_queryable_accounts_count",
            "accounts_with_errors_count",
            "failure_code",
            "failure_detail",
            "created_at",
            "started_at",
            "completed_at",
        ]
        read_only_fields = fields


class VaultCountRequestSerializer(serializers.Serializer):
    terms = serializers.CharField(max_length=1000, trim_whitespace=True)
    search_scope = serializers.ChoiceField(
        choices=VaultCountRun.SearchScope.choices,
        default=VaultCountRun.SearchScope.ACCOUNTS,
    )
    custodian_emails = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        default=list,
        max_length=100,
    )
    date_range_start = serializers.DateField(required=False, allow_null=True)
    date_range_end = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        search_scope = attrs["search_scope"]
        custodians = attrs["custodian_emails"]
        if search_scope == VaultCountRun.SearchScope.ACCOUNTS and not custodians:
            raise serializers.ValidationError(
                {"custodian_emails": "Enter at least one custodian account."}
            )
        if search_scope == VaultCountRun.SearchScope.ENTIRE_ORG and custodians:
            raise serializers.ValidationError(
                {
                    "custodian_emails": (
                        "Leave custodian accounts empty when searching all district accounts."
                    )
                }
            )
        start = attrs.get("date_range_start")
        end = attrs.get("date_range_end")
        if start and end and start > end:
            raise serializers.ValidationError(
                {"date_range_end": "The end date must be on or after the start date."}
            )
        return attrs


class VaultCountLatestSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    corpus = serializers.CharField()
    sources = serializers.ListField(child=serializers.CharField())
    latest = VaultCountRunSerializer(allow_null=True)


class VaultCollectionRequestSerializer(serializers.Serializer):
    count_run_id = serializers.UUIDField()
    expected_input_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    confirmed = serializers.BooleanField()

    def validate_confirmed(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "Confirm the reviewed count, scope, dates, and query before exporting."
            )
        return value
