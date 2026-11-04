# ABOUTME: Staff CRUD for CollectionTask — search assignments (manual or
# ABOUTME: Vault-backed) attached to a PRA request. No destroy: working record.
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.identity import StaffRole, has_staff_role
from apps.records.collection_supersession import supersede_collection
from apps.records.fees import assert_deposit_gate_clear
from apps.records.models import CollectionTask, FeeDepositGate, VaultIngestRun

User = get_user_model()


class VaultIngestProgressSerializer(serializers.ModelSerializer):
    """Staff-safe progress only; external object and storage evidence stay private."""

    class Meta:
        model = VaultIngestRun
        fields = [
            "state",
            "attempt_count",
            "files_total",
            "files_processed",
            "messages_seen",
            "items_created",
            "duplicates_skipped",
            "last_error_code",
            "started_at",
            "updated_at",
            "completed_at",
        ]
        read_only_fields = fields


class CollectionTaskSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    system_name = serializers.SerializerMethodField()
    system_slug = serializers.SerializerMethodField()
    system_urgent_hold = serializers.SerializerMethodField()
    kind = serializers.ChoiceField(choices=CollectionTask.Kind.choices, required=False)
    instructions = serializers.CharField(required=True)
    assigned_to_id = serializers.PrimaryKeyRelatedField(
        source="assigned_to",
        queryset=User.objects.filter(is_active=True, is_staff=True),
        required=False, allow_null=True)
    assigned_to_username = serializers.SerializerMethodField()
    source_vault_count_id = serializers.UUIDField(read_only=True)
    vault_ingest = serializers.SerializerMethodField()

    class Meta:
        model = CollectionTask
        fields = [
            "id", "request", "request_number",
            "system_name", "system_slug", "system_urgent_hold",
            "kind", "state", "instructions", "custodian_emails", "search_terms",
            "date_range_start", "date_range_end",
            "source_vault_count_id",
            "assigned_to_id", "assigned_to_username", "due_date",
            "vault_matter_id", "vault_hold_id", "vault_export_id",
            "vault_ingest", "created_at", "completed_at",
        ]
        read_only_fields = [
            "id", "source_vault_count_id",
            "vault_matter_id", "vault_hold_id", "vault_export_id",
            "created_at", "completed_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_system_name(self, obj):
        return obj.system.name if obj.system_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_system_slug(self, obj):
        return obj.system.slug if obj.system_id else None

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_system_urgent_hold(self, obj):
        return obj.system.urgent_hold if obj.system_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_assigned_to_username(self, obj):
        return obj.assigned_to.username if obj.assigned_to_id else None

    @extend_schema_field(VaultIngestProgressSerializer(allow_null=True))
    def get_vault_ingest(self, obj):
        try:
            run = obj.vault_ingest_run
        except VaultIngestRun.DoesNotExist:
            return None
        return VaultIngestProgressSerializer(run).data

    def create(self, validated_data):
        validated_data.setdefault("kind", CollectionTask.Kind.MANUAL)
        task = super().create(validated_data)
        from apps.work_queue.integration import (
            schedule_source_work_item_reconciliation,
        )

        schedule_source_work_item_reconciliation("collections", task.pk)
        return task

    def validate(self, attrs):
        if attrs.get("state") == CollectionTask.State.INGESTING:
            raise serializers.ValidationError(
                {"state": "Ingesting is managed by the Vault export poller."}
            )
        instance = self.instance
        vault_kinds = {
            CollectionTask.Kind.VAULT_MAIL,
            CollectionTask.Kind.VAULT_DRIVE,
        }
        kind = attrs.get("kind", instance.kind if instance else CollectionTask.Kind.MANUAL)
        if instance is None and kind in vault_kinds:
            raise serializers.ValidationError(
                {
                    "kind": (
                        "Vault tasks are created only from a human-confirmed "
                        "completed Vault count."
                    )
                }
            )
        new_state = attrs.get("state")
        # A deposit gate on collection holds the search itself, so advancing a
        # task past Pending is the moment it bites. The domain owns the rule;
        # this only surfaces it as a field error.
        if (
            instance is not None
            and new_state is not None
            and new_state != instance.state
            and new_state
            in {
                CollectionTask.State.RUNNING,
                CollectionTask.State.COMPLETE,
            }
        ):
            try:
                assert_deposit_gate_clear(
                    instance.request,
                    scope=FeeDepositGate.Scope.COLLECTION,
                )
            except DjangoValidationError as exc:
                # Fee terms are between the Records Officer and the
                # requester; a liaison learns only that the task is held.
                if has_staff_role(
                    self.context["request"].user,
                    StaffRole.RECORDS_OFFICER,
                ):
                    messages = exc.messages
                else:
                    messages = [
                        (
                            "This collection is on hold, waiting on payment. The "
                            "Records Officer will release it."
                        )
                    ]
                raise serializers.ValidationError(
                    {"state": messages}
                ) from exc
        if kind in vault_kinds and new_state == CollectionTask.State.COMPLETE:
            try:
                ingest_is_complete = (
                    instance is not None
                    and instance.vault_ingest_run.state == VaultIngestRun.State.COMPLETE
                )
            except VaultIngestRun.DoesNotExist:
                ingest_is_complete = False
            if not ingest_is_complete:
                raise serializers.ValidationError(
                    {"state": "Vault tasks complete only after verified export ingest."}
                )
        if (
            instance is not None
            and instance.kind in vault_kinds
            and instance.state == CollectionTask.State.INGESTING
            and new_state is not None
            and new_state != CollectionTask.State.INGESTING
        ):
            raise serializers.ValidationError(
                {"state": "Vault ingest state is managed by the ingestion worker."}
            )
        user = self.context["request"].user
        if has_staff_role(user, StaffRole.LIAISON) and not has_staff_role(
            user, StaffRole.RECORDS_OFFICER
        ):
            unexpected = set(attrs).difference({"state"})
            if unexpected:
                raise serializers.ValidationError(
                    "Liaisons may update only the state of their assigned task."
                )
        assignee = attrs.get("assigned_to", serializers.empty)
        if assignee is not serializers.empty and assignee is not None and not (
            has_staff_role(assignee, StaffRole.RECORDS_OFFICER)
            or has_staff_role(assignee, StaffRole.LIAISON)
        ):
            raise serializers.ValidationError(
                {
                    "assigned_to_id": (
                        "Collection work may be assigned only to a Records "
                        "Officer or Department Liaison."
                    )
                }
            )
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        assignment_changed = (
            "assigned_to" in validated_data
            and validated_data["assigned_to"] != instance.assigned_to
        )
        new_state = validated_data.get("state")
        if new_state is not None:
            if (
                new_state == CollectionTask.State.COMPLETE
                and instance.state != CollectionTask.State.COMPLETE
            ):
                validated_data["completed_at"] = timezone.now()
            elif new_state != CollectionTask.State.COMPLETE:
                validated_data["completed_at"] = None
        task = super().update(instance, validated_data)
        if assignment_changed:
            self._sync_current_work_assignment(task)
        from apps.work_queue.integration import (
            schedule_source_work_item_reconciliation,
        )

        schedule_source_work_item_reconciliation("collections", task.pk)
        return task

    def _sync_current_work_assignment(self, task):
        """Treat an explicit task reassignment as the same audited work handoff."""
        from apps.work_queue import services
        from apps.work_queue.models import WorkItem

        work_item = (
            WorkItem.objects.select_for_update()
            .filter(
                source_type=WorkItem.SourceType.COLLECTION_TASK,
                source_id=str(task.pk),
                work_kind=WorkItem.WorkKind.COLLECTION,
            )
            .first()
        )
        if work_item is None:
            return
        if work_item.assignee_id == task.assigned_to_id:
            return
        key = (
            f"collection-task-assignment:{task.pk}:{work_item.version}:"
            f"{task.assigned_to_id or 'unassigned'}"
        )
        actor = self.context["request"].user
        if task.assigned_to is None:
            services.unassign_work_item(
                work_item,
                actor=actor,
                expected_version=work_item.version,
                idempotency_key=key,
                allow_terminal=True,
            )
            return
        services.assign_work_item(
            work_item,
            assignee=task.assigned_to,
            actor=actor,
            expected_version=work_item.version,
            idempotency_key=key,
            allow_terminal=True,
        )


class SupersedeCollectionInputSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class CollectionTaskViewSet(mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             mixins.CreateModelMixin,
                             mixins.UpdateModelMixin,
                             viewsets.GenericViewSet):
    """list/retrieve/create/partial_update only — no destroy (working
    record) and no full update (PUT); edits are always partial via PATCH."""
    serializer_class = CollectionTaskSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON, StaffRole.ADMIN},
        "create": {StaffRole.RECORDS_OFFICER},
        "partial_update": {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON},
        "supersede": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        qs = CollectionTask.objects.select_related(
            "request", "system", "assigned_to", "vault_ingest_run"
        )
        if has_staff_role(self.request.user, StaffRole.LIAISON) and not has_staff_role(
            self.request.user, StaffRole.RECORDS_OFFICER
        ):
            qs = qs.filter(assigned_to=self.request.user)
        params = self.request.query_params

        state = params.get("state")
        if state:
            qs = qs.filter(state=state)

        request_number = params.get("request")
        if request_number:
            qs = qs.filter(request__number=request_number)

        if params.get("urgent", "").lower() == "true":
            qs = qs.filter(system__urgent_hold=True)

        return qs.order_by(F("due_date").asc(nulls_last=True), "id")

    @action(detail=True, methods=["post"], url_path="supersede")
    def supersede(self, request, pk=None):
        """Take this collection's unreviewed records out of review.

        Nothing is deleted. Records a reviewer already decided are untouched;
        the rest leave the review and release queues with a recorded reason.
        """
        payload = SupersedeCollectionInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        task = self.get_object()
        try:
            result = supersede_collection(
                task.pk,
                actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            ) from exc
        return Response({
            "collection_task_id": result.collection_task_id,
            "record_count": result.record_count,
        })


def register(router):
    router.register("collection-tasks", CollectionTaskViewSet, basename="collection-task")
