"""Human-confirmed Vault export work sourced from a completed count.

One reviewed count starts one collection task per Google Workspace source it
collects -- Gmail, Drive, and Chat -- each with its own export, poll, and
ingest, so a failure in one source never holds back the others.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.integrations.vault import client as vault

from .deadlines import add_business_days
from .fees import assert_deposit_gate_clear, open_deposit_gate
from .models import (
    CollectionSupersession,
    CollectionTask,
    FeeDepositGate,
    StatusEvent,
    VaultCountRun,
)
from .services import _require_current_records_officer
from .vault_counts import _rfc3339_boundary, collectable_sources, vault_count_enabled

#: Which collection task kind exports which source.
SOURCE_KINDS = {
    "mail": CollectionTask.Kind.VAULT_MAIL,
    "drive": CollectionTask.Kind.VAULT_DRIVE,
    "chat": CollectionTask.Kind.VAULT_CHAT,
}
KIND_SOURCES = {kind: source for source, kind in SOURCE_KINDS.items()}


@dataclass(frozen=True)
class VaultCollectionQueueResult:
    #: The first task, in Gmail, Drive, Chat order: what a caller that shows
    #: one collection shows.
    task: CollectionTask
    created: bool
    #: Every task this count's collection has, one per source.
    tasks: tuple = ()


def _instructions(run: VaultCountRun, source: str = "mail") -> str:
    if run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG:
        scope = (
            "All district accounts. This export does not claim an "
            "organization-wide preservation hold; refine named custodians "
            "for a targeted hold when they are identified."
        )
    else:
        scope = f"{len(run.custodian_emails)} named district account(s)."
    label = vault.SOURCE_LABELS[source]
    return (
        f"Create the confirmed Google Vault {label} export from the reviewed "
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
    return _queue_vault_collection(
        count_run_id,
        actor=actor,
        expected_input_sha256=expected_input_sha256,
        trigger=CollectionTask.Trigger.MANUAL,
    )


def queue_automatic_vault_collection(count_run_id) -> VaultCollectionQueueResult:
    """Start the export from an automatic scoping session's final count (#37).

    The rule 11 amendment: only with ``PRR_TRUST_SCOPE_MODE`` set to ``auto``,
    only from the final completed count of a finished *automatic* scoping
    session, and only on a request with no Vault collection yet. No actor;
    the task is recorded ``trigger=automatic``. Holds are unchanged.
    """
    from apps.configuration import runtime

    from .models import ScopeIteration, ScopingSession

    if runtime.get("PRR_TRUST_SCOPE_MODE") != "auto":
        raise ValidationError("The trust policy does not start collections automatically.")
    iteration = (
        ScopeIteration.objects.select_related("session", "count_run")
        .filter(count_run_id=count_run_id)
        .first()
    )
    if (
        iteration is None
        or iteration.session.trigger != ScopingSession.Trigger.AUTOMATIC
        or iteration.session.state != ScopingSession.State.COMPLETE
    ):
        raise ValidationError(
            "Only a finished automatic scoping session's final count is collected automatically."
        )
    final = (
        ScopeIteration.objects.filter(
            session=iteration.session, count_run__state=VaultCountRun.State.COMPLETE,
        )
        .order_by("-number")
        .first()
    )
    if final is None or final.pk != iteration.pk:
        raise ValidationError("Only the session's final count is collected automatically.")
    if CollectionTask.objects.filter(
        request=iteration.count_run.request, kind__in=CollectionTask.VAULT_KINDS,
    ).exists():
        raise ValidationError("This request already has a Vault collection.")
    return _queue_vault_collection(
        count_run_id,
        actor=None,
        expected_input_sha256=iteration.count_run.input_sha256,
        trigger=CollectionTask.Trigger.AUTOMATIC,
    )


def _queue_vault_collection(
    count_run_id,
    *,
    actor,
    expected_input_sha256: str,
    trigger: str,
) -> VaultCollectionQueueResult:
    if not vault_count_enabled():
        raise ValidationError("Google Vault collection is not configured.")
    expected_hash = str(expected_input_sha256 or "").strip().lower()
    created = False
    with transaction.atomic():
        if trigger != CollectionTask.Trigger.AUTOMATIC:
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
        sources = collectable_sources(run)
        if not sources:
            entire_org = run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
            if entire_org and not run.org_unit_id and len(run.sources or ()) > 1:
                raise ValidationError(
                    "Gmail counted zero, and Drive and Chat cannot be searched "
                    "district-wide until the staff organizational unit is set; "
                    "there is nothing to export from this scope."
                )
            raise ValidationError(
                "Every source counted zero possible messages; there is "
                "nothing to export from this scope."
            )
        existing = {
            task.kind: task
            for task in CollectionTask.objects.filter(source_vault_count=run)
        }
        # Handing back a replaced collection, or one Vault failed after its
        # export started, would be a silent no-op (#37): its records stay
        # superseded, or nothing runs. A new count is a new collection.
        if any(
            CollectionSupersession.objects.filter(collection_task=task).exists()
            or (task.state == CollectionTask.State.FAILED and task.vault_export_id)
            for task in existing.values()
        ):
            raise ValidationError(
                "This count's collection was replaced or failed in Vault; "
                "run scoping again for a new count to collect."
            )
        tasks = []
        to_start = []
        for source in sources:
            kind = SOURCE_KINDS[source]
            task = existing.get(kind)
            if task is not None:
                if task.state == CollectionTask.State.FAILED:
                    task.state = CollectionTask.State.PENDING
                    task.completed_at = None
                    task.save(update_fields=["state", "completed_at", "updated_at"])
                    to_start.append(task)
                    audit(
                        "vault.collection.requeued",
                        target=run.request.number,
                        actor=actor,
                        collection_task_id=task.pk,
                        count_run_id=str(run.pk),
                        source=source,
                    )
                tasks.append(task)
                continue
            task = CollectionTask.objects.create(
                request=run.request,
                kind=kind,
                state=CollectionTask.State.PENDING,
                source_vault_count=run,
                trigger=trigger,
                instructions=_instructions(run, source),
                custodian_emails=run.custodian_emails,
                search_terms=run.terms,
                date_range_start=run.date_range_start,
                date_range_end=run.date_range_end,
                due_date=add_business_days(timezone.localdate(), 2),
            )
            created = True
            tasks.append(task)
            to_start.append(task)
            audit(
                "vault.collection.queued",
                target=run.request.number,
                actor=actor,
                collection_task_id=task.pk,
                count_run_id=str(run.pk),
                input_sha256=run.input_sha256,
                search_scope=run.search_scope,
                possible_message_count=run.total_count,
                source=source,
                trigger=trigger,
            )

    from apps.work_queue.integration import (
        schedule_source_work_item_reconciliation,
    )

    from .tasks import execute_vault_collection

    for task in tasks:
        schedule_source_work_item_reconciliation("collections", task.pk)
    for task in to_start:
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
    return VaultCollectionQueueResult(task=tasks[0], created=created, tasks=tuple(tasks))


@transaction.atomic
def start_vault_collection(collection_task_id: int) -> bool:
    """Create/reuse provider artifacts and arm the existing export poller."""
    task = (
        CollectionTask.objects.select_for_update()
        .select_related("request")
        .get(pk=collection_task_id)
    )
    run = task.source_vault_count
    if run is None or task.kind not in KIND_SOURCES:
        raise ValidationError(
            "This task is not backed by a reviewed Vault count."
        )
    source = KIND_SOURCES[task.kind]
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
    if source != "mail":
        artifact_name = f"{artifact_name} — {vault.SOURCE_LABELS[source]}"
    matter_id = run.vault_matter_id
    hold_id = task.vault_hold_id
    # Gmail's targeted hold is unchanged for every collection. Drive and
    # Chat holds are placed only when a person started the collection; an
    # automatic collection (PRR_TRUST_SCOPE_MODE=auto) adds none (rule 11).
    wants_hold = (
        run.search_scope == VaultCountRun.SearchScope.ACCOUNTS
        and not hold_id
        and (source == "mail" or task.trigger == CollectionTask.Trigger.MANUAL)
    )
    if wants_hold:
        if source == "mail":
            hold_id = vault.ensure_mail_hold(
                matter_id,
                run.custodian_emails,
                name=artifact_name,
            )
        else:
            hold_id = vault.ensure_source_hold(
                source,
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

    entire_org = run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
    if source == "mail":
        export_id = vault.ensure_mail_export(
            matter_id,
            run.custodian_emails,
            run.terms,
            start_time=_rfc3339_boundary(run.date_range_start),
            end_time=_rfc3339_boundary(run.date_range_end, following_day=True),
            name=artifact_name,
            entire_org=entire_org,
        )
    else:
        export_id = vault.ensure_source_export(
            source,
            matter_id,
            run.custodian_emails,
            run.terms,
            name=artifact_name,
            start_time=_rfc3339_boundary(run.date_range_start),
            end_time=_rfc3339_boundary(run.date_range_end, following_day=True),
            entire_org=entire_org,
            org_unit_id=run.org_unit_id,
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
            f"Vault {vault.SOURCE_LABELS[source]} collection started from the "
            "reviewed count; the export will enter Record Items after "
            "verified ingest."
        ),
    )
    audit(
        "vault.collection.started",
        target=task.request.number,
        collection_task_id=task.pk,
        count_run_id=str(run.pk),
        search_scope=run.search_scope,
        source=source,
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
