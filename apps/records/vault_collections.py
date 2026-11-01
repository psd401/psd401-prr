"""Human-confirmed Vault export work sourced from a completed count."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.integrations.vault import client as vault

from .deadlines import add_business_days
from .fees import assert_deposit_gate_clear, open_deposit_gate
from .models import CollectionTask, FeeDepositGate, StatusEvent, VaultCountRun
from .services import _require_current_records_officer
from .vault_counts import _rfc3339_boundary, vault_count_enabled


@dataclass(frozen=True)
class VaultCollectionQueueResult:
    task: CollectionTask
    created: bool


def _instructions(run: VaultCountRun) -> str:
    if run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG:
        scope = (
            "All district accounts. This export does not claim an "
            "organization-wide preservation hold; refine named custodians "
            "for a targeted hold when they are identified."
        )
    else:
        scope = f"{len(run.custodian_emails)} named district account(s)."
    return (
        "Create the confirmed Google Vault Gmail export from the reviewed "
        f"count. Scope: {scope}\n\n"
        "After verified ingest, review every item in Records → Record items "
        "and classify it as responsive, non-responsive, or uncertain before "
        "production."
    )


def queue_vault_collection(
    count_run_id,
    *,
    actor,
    expected_input_sha256: str,
) -> VaultCollectionQueueResult:
    """Create one durable task and enqueue provider work after human review."""
    if not vault_count_enabled():
        raise ValidationError("Google Vault collection is not configured.")
    expected_hash = str(expected_input_sha256 or "").strip().lower()
    created = False
    with transaction.atomic():
        actor = _require_current_records_officer(
            actor=actor,
            message=(
                "Current Records Officer authority is required to start a "
                "Vault collection."
            ),
        )
        run = (
            VaultCountRun.objects.select_for_update()
            .select_related("request")
            .get(pk=count_run_id)
        )
        assert_deposit_gate_clear(
            run.request,
            scope=FeeDepositGate.Scope.COLLECTION,
        )
        if run.state != VaultCountRun.State.COMPLETE:
            raise ValidationError(
                "A completed Vault count is required before collection."
            )
        if not run.vault_matter_id:
            raise ValidationError(
                "The completed Vault count is missing its matter evidence."
            )
        if expected_hash != run.input_sha256:
            raise ValidationError(
                "The Vault scope changed since review; review the latest "
                "count before collecting."
            )
        if not run.total_count:
            raise ValidationError(
                "The completed count found zero possible messages; there is "
                "nothing to export from this scope."
            )
        existing = CollectionTask.objects.filter(
            source_vault_count=run,
        ).first()
        if existing is not None:
            if (
                existing.state == CollectionTask.State.FAILED
                and not existing.vault_export_id
            ):
                task = existing
                task.state = CollectionTask.State.PENDING
                task.completed_at = None
                task.save(
                    update_fields=["state", "completed_at", "updated_at"]
                )
                audit(
                    "vault.collection.requeued",
                    target=run.request.number,
                    actor=actor,
                    collection_task_id=task.pk,
                    count_run_id=str(run.pk),
                )
            else:
                return VaultCollectionQueueResult(task=existing, created=False)
        else:
            task = CollectionTask.objects.create(
                request=run.request,
                kind=CollectionTask.Kind.VAULT_MAIL,
                state=CollectionTask.State.PENDING,
                source_vault_count=run,
                instructions=_instructions(run),
                custodian_emails=run.custodian_emails,
                search_terms=run.terms,
                date_range_start=run.date_range_start,
                date_range_end=run.date_range_end,
                due_date=add_business_days(timezone.localdate(), 2),
            )
            created = True
            audit(
                "vault.collection.queued",
                target=run.request.number,
                actor=actor,
                collection_task_id=task.pk,
                count_run_id=str(run.pk),
                input_sha256=run.input_sha256,
                search_scope=run.search_scope,
                possible_message_count=run.total_count,
            )

    from apps.work_queue.integration import (
        schedule_source_work_item_reconciliation,
    )

    schedule_source_work_item_reconciliation("collections", task.pk)

    from .tasks import execute_vault_collection

    try:
        execute_vault_collection.delay(task.pk)
    except Exception as exc:
        fail_vault_collection(
            task.pk,
            code="queue_unavailable",
            detail="The Vault collection worker could not be queued.",
        )
        raise ValidationError(
            "The Vault collection worker could not be queued."
        ) from exc
    return VaultCollectionQueueResult(task=task, created=created)


@transaction.atomic
def start_vault_collection(collection_task_id: int) -> bool:
    """Create/reuse provider artifacts and arm the existing export poller."""
    task = (
        CollectionTask.objects.select_for_update()
        .select_related("request")
        .get(pk=collection_task_id)
    )
    run = task.source_vault_count
    if run is None or task.kind != CollectionTask.Kind.VAULT_MAIL:
        raise ValidationError(
            "This task is not backed by a reviewed Vault Gmail count."
        )
    if task.vault_export_id and task.state in {
        CollectionTask.State.RUNNING,
        CollectionTask.State.INGESTING,
        CollectionTask.State.COMPLETE,
    }:
        return False
    if task.state not in {
        CollectionTask.State.PENDING,
        CollectionTask.State.FAILED,
    }:
        raise ValidationError("This Vault collection cannot be started again.")

    artifact_name = f"{task.request.number} — Vault count {run.pk}"
    matter_id = run.vault_matter_id
    hold_id = task.vault_hold_id
    if run.search_scope == VaultCountRun.SearchScope.ACCOUNTS and not hold_id:
        hold_id = vault.ensure_mail_hold(
            matter_id,
            run.custodian_emails,
            name=artifact_name,
        )
        task.vault_hold_id = hold_id
        task.vault_matter_id = matter_id
        task.save(
            update_fields=["vault_hold_id", "vault_matter_id", "updated_at"]
        )

    if open_deposit_gate(
        task.request,
        scope=FeeDepositGate.Scope.COLLECTION,
    ) is not None:
        # A deposit requested after queueing holds the export, never the
        # preservation hold placed above. The task fails with no export so
        # an officer can requeue it once the deposit is recorded or lifted.
        fail_vault_collection(
            task.pk,
            code="deposit_gate_open",
            detail=(
                "Vault collection is waiting on a collection deposit "
                "requested after it was queued; the preservation hold is in "
                "place. Requeue it once the deposit is recorded or lifted."
            ),
        )
        return False

    export_id = vault.ensure_mail_export(
        matter_id,
        run.custodian_emails,
        run.terms,
        start_time=_rfc3339_boundary(run.date_range_start),
        end_time=_rfc3339_boundary(run.date_range_end, following_day=True),
        name=artifact_name,
        entire_org=(
            run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
        ),
    )
    task.vault_matter_id = matter_id
    task.vault_export_id = export_id
    task.state = CollectionTask.State.RUNNING
    task.completed_at = None
    task.save(
        update_fields=[
            "vault_matter_id",
            "vault_export_id",
            "state",
            "completed_at",
            "updated_at",
        ]
    )
    StatusEvent.objects.create(
        request=task.request,
        note=(
            "Vault collection started from the reviewed count; the export "
            "will enter Record Items after verified ingest."
        ),
    )
    audit(
        "vault.collection.started",
        target=task.request.number,
        collection_task_id=task.pk,
        count_run_id=str(run.pk),
        search_scope=run.search_scope,
        targeted_hold=bool(hold_id),
    )
    return True


def fail_vault_collection(
    collection_task_id: int,
    *,
    code: str,
    detail: str,
) -> None:
    """Persist only fixed-code, content-free provider failure evidence."""
    with transaction.atomic():
        task = (
            CollectionTask.objects.select_for_update()
            .select_related("request")
            .get(pk=collection_task_id)
        )
        if task.state in {
            CollectionTask.State.INGESTING,
            CollectionTask.State.COMPLETE,
        }:
            return
        task.state = CollectionTask.State.FAILED
        task.completed_at = None
        task.save(update_fields=["state", "completed_at", "updated_at"])
        StatusEvent.objects.create(request=task.request, note=detail)
        audit(
            "vault.collection.failed",
            target=task.request.number,
            collection_task_id=task.pk,
            failure_code=code,
        )
