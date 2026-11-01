"""Staff-safe serializers for count-only Google Vault searches."""

from rest_framework import serializers

from apps.records.models import CollectionTask, VaultCountRun


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

    def _collection_task(self, obj):
        try:
            return obj.collection_task
        except obj._meta.model.collection_task.RelatedObjectDoesNotExist:
            return None

    def get_collection_task_id(self, obj) -> int | None:
        task = self._collection_task(obj)
        return task.pk if task is not None else None

    def get_collection_task_state(self, obj) -> str | None:
        task = self._collection_task(obj)
        return task.state if task is not None else None

    def get_collection_task_retryable(self, obj) -> bool:
        task = self._collection_task(obj)
        return bool(
            task is not None
            and task.state == CollectionTask.State.FAILED
            and not task.vault_export_id
        )

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
