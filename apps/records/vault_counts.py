"""Human-scoped, count-only Google Vault searches.

One count covers every Google Workspace source the district collects from:
Gmail, Drive, and Chat, each with its own count method (``COUNT_METHODS``).
Gmail uses ``matters.count``, and its result fills the run's own columns, as
it always has. Vault's ``matters.count`` rejects Drive and Chat outright
("Corpus type DRIVE is not supported", production probe 2026-11-08), so those
two sources are counted with a count-only export: its
``stats.totalArtifactCount`` is read once the export is COMPLETED, and the
export is then deleted. A count-only export is never downloaded and never
becomes a record (AGENTS.md rule 11; pinned by ``test_compliance``).

Each source ends ``counted`` with a number or ``unavailable`` with a plain
reason, never silently skipped. The run completes once every source has, and
its ``total_count`` is the sum of the counted sources.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.audit.models import audit
from apps.configuration import runtime
from apps.integrations.vault import client as vault

from .models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordSystem,
    VaultCountRun,
)

logger = logging.getLogger(__name__)

COUNTING = "counting"
COUNTED = "counted"
UNAVAILABLE = "unavailable"
_SETTLED = {COUNTED, UNAVAILABLE}

#: How each source is counted. ``matters.count`` returns HTTP 400 "not
#: supported" for DRIVE and HANGOUTS_CHAT, so those use a count-only export.
COUNT_METHODS = {"mail": "count", "drive": "export", "chat": "export"}
#: How long a count-only export may run before its source is reported
#: unavailable. The request accepts no new count while one is running, so
#: this is also the longest an officer waits to count again.
COUNT_EXPORT_TIMEOUT = dt.timedelta(hours=3)

_ITEM_NOUNS = {
    "mail": "Gmail messages",
    "drive": "Drive files",
    "chat": "Chat messages",
}


class VaultCountAlreadyRunning(RuntimeError):
    pass


def vault_count_enabled() -> bool:
    """Whether the delegated Vault identity has been configured."""
    admin_user = runtime.get("PRR_VAULT_ADMIN_USER")
    if not admin_user:
        return False
    if settings.DEBUG:
        return bool(
            runtime.get("PRR_VAULT_CREDENTIALS_FILE")
            or runtime.get("PRR_VAULT_IMPERSONATION_TARGET")
        )
    return bool(runtime.get("PRR_VAULT_IMPERSONATION_TARGET"))


def _normalized_date(value, field_name: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        raise ValidationError({field_name: "Enter a calendar date."})
    if isinstance(value, dt.date):
        return value
    parsed = parse_date(str(value))
    if parsed is None:
        raise ValidationError({field_name: "Enter a valid calendar date."})
    return parsed


def _normalize_scope(
    *,
    terms,
    search_scope,
    custodian_emails,
    date_range_start=None,
    date_range_end=None,
):
    normalized_terms = str(terms or "").strip()
    if not normalized_terms:
        raise ValidationError({"terms": "Enter the human-reviewed Vault query."})
    if len(normalized_terms) > 1000:
        raise ValidationError({"terms": "Vault queries are limited to 1,000 characters."})

    if search_scope not in VaultCountRun.SearchScope.values:
        raise ValidationError({"search_scope": "Choose a valid Vault search scope."})
    if not isinstance(custodian_emails, (list, tuple)):
        raise ValidationError({"custodian_emails": "Enter a list of custodian accounts."})
    normalized_accounts = []
    seen = set()
    for raw_email in custodian_emails:
        email = str(raw_email or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError({"custodian_emails": "Enter valid custodian email addresses."})
        if email not in seen:
            seen.add(email)
            normalized_accounts.append(email)
    if search_scope == VaultCountRun.SearchScope.ACCOUNTS and not normalized_accounts:
        raise ValidationError({"custodian_emails": "Enter at least one custodian account."})
    if search_scope == VaultCountRun.SearchScope.ENTIRE_ORG and normalized_accounts:
        raise ValidationError(
            {"custodian_emails": "Leave custodian accounts empty when searching all district accounts."}
        )
    if len(normalized_accounts) > 100:
        raise ValidationError({"custodian_emails": "A count may include at most 100 custodians."})

    hosted_domain = runtime.get("PRR_GOOGLE_HOSTED_DOMAIN")
    if not hosted_domain or any(
        email.rsplit("@", 1)[-1] != hosted_domain for email in normalized_accounts
    ):
        raise ValidationError(
            {
                "custodian_emails": (
                    "Every custodian must be an account in the district Google Workspace."
                )
            }
        )

    start = _normalized_date(date_range_start, "date_range_start")
    end = _normalized_date(date_range_end, "date_range_end")
    if start and end and start > end:
        raise ValidationError(
            {"date_range_end": "The end date must be on or after the start date."}
        )
    return search_scope, normalized_terms, normalized_accounts, start, end


def _input_sha256(
    request_id,
    search_scope,
    terms,
    accounts,
    start,
    end,
    *,
    sources=("mail",),
    org_unit_id="",
) -> str:
    payload = {
        "version": 3,
        "request_id": str(request_id),
        "sources": list(sources),
        "org_unit_id": org_unit_id,
        "search_scope": search_scope,
        "terms": terms,
        "custodian_emails": accounts,
        "date_range_start": start.isoformat() if start else None,
        "date_range_end": end.isoformat() if end else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def vault_query_from_intent(
    required_terms,
    alternative_terms,
    *,
    sender_domains=(),
    exclude_senders=(),
) -> str:
    """Assemble the proposed Vault query the staff UI shows for a proposal.

    Required terms are mandatory AND anchors; alternatives become one
    parenthesized OR group. This mirrors the staff workspace exactly, so an
    automatic count and the officer's own Count button scope the same search
    from the same proposal.
    """

    def quoted(values) -> list[str]:
        out: list[str] = []
        seen = set()
        for value in values or ():
            term = str(value or "").strip().strip('"')
            if not term:
                continue
            normalized = f'"{term.replace(chr(34), "")}"'
            if normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out

    required = quoted(required_terms)
    required_keys = {term.casefold() for term in required}
    alternatives = [
        term for term in quoted(alternative_terms)
        if term.casefold() not in required_keys
    ]
    if not required and not alternatives:
        # Senders alone never make a query: there is nothing to search for.
        return ""
    parts = list(required)
    if len(alternatives) == 1:
        parts.append(alternatives[0])
    elif alternatives:
        parts.append(f"({' OR '.join(alternatives)})")
    # Senders (#36): mail from these domains only, and never from the
    # district's known noise senders. Values are validated addresses and
    # domains; anything else is dropped rather than spliced into the query.
    senders = [f"from:{value}" for value in _sender_values(sender_domains)]
    if len(senders) == 1:
        parts.append(senders[0])
    elif senders:
        parts.append(f"({' OR '.join(senders)})")
    parts.extend(f"-from:{value}" for value in _sender_values(exclude_senders))
    return " AND ".join(parts)


_SENDER = re.compile(r"^[A-Za-z0-9._%+'@-]+\.[A-Za-z]{2,}$")


def _sender_values(values) -> list[str]:
    out = []
    for value in values or ():
        text = str(value or "").strip().lower()
        if _SENDER.match(text) and text not in out:
            out.append(text)
    return out


def intent_vault_system_slugs(proposal) -> list[str]:
    """The active Vault-backed systems this proposal mapped the request to."""
    if not isinstance(proposal, dict):
        return []
    slugs = [
        str(slug)
        for slug in proposal.get("record_system_slugs") or ()
        if isinstance(slug, str)
    ]
    if not slugs:
        return []
    return sorted(
        RecordSystem.objects.filter(
            active=True,
            slug__in=slugs,
            integration=RecordSystem.Integration.VAULT,
        ).values_list("slug", flat=True)
    )


def request_vault_count(
    request_id,
    *,
    actor,
    terms,
    custodian_emails,
    search_scope=VaultCountRun.SearchScope.ACCOUNTS,
    date_range_start=None,
    date_range_end=None,
    trigger=VaultCountRun.Trigger.MANUAL,
) -> VaultCountRun:
    """Persist scope and queue a count without applying any outcome."""
    if not vault_count_enabled():
        raise ValidationError("Google Vault counting is not configured.")
    if trigger not in VaultCountRun.Trigger.values:
        raise ValidationError({"trigger": "Choose a valid count trigger."})
    if actor is None and trigger != VaultCountRun.Trigger.AUTOMATIC:
        raise ValidationError(
            "An officer-scoped Vault count requires an accountable actor."
        )
    search_scope, terms, accounts, start, end = _normalize_scope(
        terms=terms,
        search_scope=search_scope,
        custodian_emails=custodian_emails,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )

    sources = list(vault.SOURCES)
    org_unit_id = (
        str(runtime.get("PRR_VAULT_STAFF_ORG_UNIT_ID") or "").strip()
        if search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
        else ""
    )

    with transaction.atomic():
        prr = PublicRecordsRequest.objects.select_for_update().get(pk=request_id)
        if prr.vault_count_runs.filter(
            state__in={VaultCountRun.State.PENDING, VaultCountRun.State.RUNNING}
        ).exists():
            raise VaultCountAlreadyRunning(
                "A Vault count is already in progress for this request."
            )
        run = VaultCountRun.objects.create(
            request=prr,
            trigger=trigger,
            requested_by=actor,
            search_scope=search_scope,
            terms=terms,
            custodian_emails=accounts,
            date_range_start=start,
            date_range_end=end,
            sources=sources,
            org_unit_id=org_unit_id,
            input_sha256=_input_sha256(
                prr.pk,
                search_scope,
                terms,
                accounts,
                start,
                end,
                sources=sources,
                org_unit_id=org_unit_id,
            ),
        )

    from .tasks import execute_vault_count

    try:
        execute_vault_count.delay(str(run.pk))
    except Exception as exc:
        fail_vault_count(
            run.pk,
            code="queue_unavailable",
            detail="The Vault count worker could not be queued.",
        )
        raise ValidationError("The Vault count worker could not be queued.") from exc

    audit(
        "vault.count.queued",
        target=prr.number,
        actor=actor,
        run_id=str(run.pk),
        input_sha256=run.input_sha256,
        search_scope=run.search_scope,
        custodian_count=len(accounts),
        trigger=run.trigger,
    )
    return run


def request_automatic_vault_count_from_intent(request_id, proposal):
    """Count the proposed search universe as soon as intent is proposed.

    A count estimates a search universe. It is not a search, a hold, an
    export, or anything the requester ever sees -- so producing it before the
    officer opens the case costs nothing, and it is the difference between a
    case that answers "how big is this?" on arrival and one that answers it a
    day later. It runs only when the proposal actually maps the request to a
    Vault-backed system; the officer re-runs it with their own edits from the
    Collection tab whenever the proposal was wrong.

    Returns the queued run, or ``None`` when no automatic count applies.
    """
    if not runtime.get("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED"):
        return None
    if not vault_count_enabled():
        return None
    if not intent_vault_system_slugs(proposal):
        return None
    if not vault_query_from_intent(
        proposal.get("required_search_terms"),
        proposal.get("search_terms"),
    ):
        return None
    from .models import ScopeIteration, ScopingSession
    from .scoping import start_scoping

    prr = PublicRecordsRequest.objects.get(pk=request_id)
    session = start_scoping(prr, proposal, trigger=ScopingSession.Trigger.AUTOMATIC)
    return ScopeIteration.objects.get(session=session, number=1).count_run


def proposal_date_range(prr, proposal) -> tuple[dt.date | None, dt.date]:
    """The proposal's date range, never reaching past receipt."""
    received_date = timezone.localtime(prr.received_at).date()
    date_range = proposal.get("date_range") if isinstance(proposal, dict) else None
    if not isinstance(date_range, dict):
        date_range = {}
    end = _normalized_date(date_range.get("end"), "date_range_end")
    if end is None or end > received_date:
        # Only records in existence at receipt can be responsive.
        end = received_date
    start = _normalized_date(date_range.get("start"), "date_range_start")
    if start is not None and start > end:
        start = None
    return start, end


def defer_automatic_vault_count_from_intent(request_id, proposal) -> bool:
    """Queue the automatic count once the advisory result is durable."""
    if not runtime.get("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED"):
        return False

    def count_after_commit():
        try:
            request_automatic_vault_count_from_intent(request_id, proposal)
        except Exception:
            # The proposal is already durable and the officer's own Count
            # button remains. This includes VaultCountAlreadyRunning: an
            # officer who clicked Count first keeps their run. Never log
            # request or model content.
            logger.exception(
                "Automatic Vault count could not be queued",
                extra={"request": str(request_id)},
            )

    transaction.on_commit(count_after_commit)
    return True


def _rfc3339_boundary(value: dt.date | None, *, following_day=False) -> str | None:
    if value is None:
        return None
    if following_day:
        value += dt.timedelta(days=1)
    local_midnight = dt.datetime.combine(
        value,
        dt.time.min,
        tzinfo=ZoneInfo(settings.TIME_ZONE),
    )
    utc = local_midnight.astimezone(dt.UTC)
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _existing_matter_id(run: VaultCountRun) -> str:
    prior_count = (
        VaultCountRun.objects.filter(request=run.request)
        .exclude(pk=run.pk)
        .exclude(vault_matter_id="")
        .values_list("vault_matter_id", flat=True)
        .first()
    )
    if prior_count:
        return prior_count
    return (
        CollectionTask.objects.filter(request=run.request)
        .exclude(vault_matter_id="")
        .values_list("vault_matter_id", flat=True)
        .first()
        or ""
    )


def start_vault_count(run_id) -> bool:
    """Start a provider count. Return True when it completed immediately."""
    run = VaultCountRun.objects.select_related("request", "requested_by").get(pk=run_id)
    if run.state in {VaultCountRun.State.COMPLETE, VaultCountRun.State.FAILED}:
        return True
    if run.operation_name:
        return advance_vault_count(run.pk)

    matter_id = _existing_matter_id(run) or vault.ensure_matter(
        run.request.number,
        run.request.description,
        actor=run.requested_by,
    )
    count_options = {
        "start_time": _rfc3339_boundary(run.date_range_start),
        "end_time": _rfc3339_boundary(run.date_range_end, following_day=True),
    }
    if run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG:
        count_options["entire_org"] = True
    operation = vault.start_mail_count(
        matter_id,
        run.custodian_emails,
        run.terms,
        **count_options,
    )
    operation_name = str(operation.get("name", "")).strip()
    if not operation_name:
        fail_vault_count(
            run.pk,
            code="invalid_provider_result",
            detail="Google Vault returned an invalid count operation.",
        )
        return True

    run.state = VaultCountRun.State.RUNNING
    run.vault_matter_id = matter_id
    run.operation_name = operation_name
    run.started_at = timezone.now()
    run.save(
        update_fields=[
            "state",
            "vault_matter_id",
            "operation_name",
            "started_at",
        ]
    )
    audit(
        "vault.count.started",
        target=run.request.number,
        actor=run.requested_by,
        run_id=str(run.pk),
        input_sha256=run.input_sha256,
        search_scope=run.search_scope,
        sources=run_sources(run),
    )
    _start_other_sources(run)
    if operation.get("done"):
        return _apply_operation(run, operation)
    return False


# ------------------------------------------------------------ Drive and Chat


def run_sources(run: VaultCountRun) -> list[str]:
    """The sources a run searched, in display order. Empty means Gmail only."""
    chosen = set(run.sources or ["mail"])
    return [source for source in vault.SOURCES if source in chosen]


def _search_kwargs(run: VaultCountRun) -> dict:
    return {
        "start_time": _rfc3339_boundary(run.date_range_start),
        "end_time": _rfc3339_boundary(run.date_range_end, following_day=True),
        "entire_org": run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG,
        "org_unit_id": run.org_unit_id,
    }


def _unavailable(reason: str) -> dict:
    return {"state": UNAVAILABLE, "count": None, "reason": reason}


def _could_not_count(source: str) -> dict:
    return _unavailable(f"Google Vault could not count {_ITEM_NOUNS[source]}.")


def _rate_limited(exc: Exception) -> bool:
    """Vault refused because exports were created faster than its quota.

    Production, 2026-11-08: the third and fourth count-only exports created
    within a minute came back HTTP 429 "Quota exceeded ... Export Writes".
    That is a reason to wait, not evidence the source cannot be counted.
    """
    return getattr(getattr(exc, "resp", None), "status", None) == 429


def _start_other_sources(run: VaultCountRun) -> None:
    counts = dict(run.source_counts or {})
    for source in run_sources(run):
        # Gmail's matters.count is started by start_vault_count itself.
        if COUNT_METHODS[source] != "export" or source in counts:
            continue
        counts[source] = _start_source_count(run, source)
    run.source_counts = counts
    run.save(update_fields=["source_counts"])


def _start_source_count(run: VaultCountRun, source: str) -> dict:
    if (
        run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
        and not run.org_unit_id
    ):
        # ENTIRE_ORG is Gmail-only in Vault; say so rather than skip.
        return _unavailable("Staff organizational unit not set.")
    return _start_count_export(run, source)


def _count_export_name(run: VaultCountRun, source: str) -> str:
    return f"{run.request.number} — count-only {run.pk} {vault.SOURCE_LABELS[source]}"


def _start_count_export(run: VaultCountRun, source: str, waiting: dict | None = None) -> dict:
    """Start one count-only export, or wait if Vault is rate limiting.

    ``waiting`` is the source's earlier rate-limited entry, if any: its first
    attempt time bounds how long the source may wait (COUNT_EXPORT_TIMEOUT).
    """
    try:
        export_id = vault.ensure_count_export(
            source,
            run.vault_matter_id,
            run.custodian_emails,
            run.terms,
            name=_count_export_name(run, source),
            **_search_kwargs(run),
        )
    except Exception as exc:  # noqa: BLE001 - a source that cannot count is reported unavailable
        if _rate_limited(exc):
            # No export exists yet, so there is nothing to delete; the next
            # check (the export poller) tries again.
            return {
                "state": COUNTING,
                "method": "export",
                "export_id": None,
                "waiting": "rate_limited",
                "attempts": int((waiting or {}).get("attempts") or 0) + 1,
                "started_at": (waiting or {}).get("started_at") or timezone.now().isoformat(),
            }
        # Includes a permission refusal: the identity may lack Drive or Chat
        # export rights. Provider text can echo the query: never log it.
        logger.warning(
            "Vault count-only export did not start; provider detail suppressed",
            extra={"vault_count_run": str(run.pk), "source": source},
        )
        return _could_not_count(source)
    audit(
        "vault.count.export_started",
        target=run.request.number,
        actor=run.requested_by,
        run_id=str(run.pk),
        source=source,
    )
    return {
        "state": COUNTING,
        "method": "export",
        "export_id": export_id,
        "export_deleted": False,
        "started_at": timezone.now().isoformat(),
    }


def _delete_count_export(run: VaultCountRun, entry: dict) -> dict:
    """Delete a count-only export once its count is read. Never download it."""
    if not entry.get("export_id") or entry.get("export_deleted"):
        return entry
    try:
        vault.delete_export(run.vault_matter_id, entry["export_id"])
    except Exception:  # noqa: BLE001 - a failed delete is retried on the next poll
        logger.warning(
            "Vault count-only export could not be deleted yet; it is retried",
            extra={"vault_count_run": str(run.pk)},
        )
        return {**entry, "export_deleted": False}
    return {**entry, "export_deleted": True}


def _timed_out(entry: dict, limit: dt.timedelta) -> bool:
    started = parse_datetime(str(entry.get("started_at") or ""))
    return started is not None and timezone.now() - started > limit


def _advance_source(run: VaultCountRun, source: str, entry: dict) -> dict:
    if entry.get("state") != COUNTING:
        # Settled; only a delete that failed earlier is left to do.
        return _delete_count_export(run, entry)
    if entry.get("waiting") == "rate_limited":
        if _timed_out(entry, COUNT_EXPORT_TIMEOUT):
            return {**entry, **_unavailable(
                f"Google Vault was too busy to count {_ITEM_NOUNS[source]}; count again later."
            )}
        return _start_count_export(run, source, waiting=entry)
    late = _unavailable(
        f"Google Vault did not finish counting {_ITEM_NOUNS[source]} in time."
    )
    try:
        export = vault.get_export(run.vault_matter_id, entry["export_id"])
    except Exception:  # noqa: BLE001 - a read hiccup is retried until the timeout
        logger.warning(
            "Vault count-only export could not be read; retried",
            extra={"vault_count_run": str(run.pk), "source": source},
        )
        if _timed_out(entry, COUNT_EXPORT_TIMEOUT):
            return _delete_count_export(run, {**entry, **late})
        return entry
    status = export.get("status")
    if status == "COMPLETED":
        # Only the export's own statistics are read; its files never are.
        total = vault.export_artifact_total(export)
        result = (
            {"state": COUNTED, "count": total}
            if total is not None
            else _could_not_count(source)
        )
        return _delete_count_export(run, {**entry, **result})
    if status == "FAILED":
        return _delete_count_export(run, {**entry, **_could_not_count(source)})
    if _timed_out(entry, COUNT_EXPORT_TIMEOUT):
        return _delete_count_export(run, {**entry, **late})
    return entry


def _advance_other_sources(run: VaultCountRun) -> None:
    counts = dict(run.source_counts or {})
    changed = False
    for source in run_sources(run):
        if source == "mail" or source not in counts:
            continue
        updated = _advance_source(run, source, counts[source])
        if updated != counts[source]:
            counts[source] = updated
            changed = True
    if changed:
        run.source_counts = counts
        run.save(update_fields=["source_counts"])


def _complete_if_settled(run: VaultCountRun) -> bool:
    """Complete the run once every source is counted or unavailable."""
    counts = run.source_counts or {}
    sources = run_sources(run)
    if any(counts.get(source, {}).get("state") not in _SETTLED for source in sources):
        return False
    per_source = {
        source: counts[source].get("count")
        if counts[source].get("state") == COUNTED
        else None
        for source in sources
    }
    total = sum(value or 0 for value in per_source.values())
    now = timezone.now()
    updated = (
        VaultCountRun.objects.filter(pk=run.pk)
        .exclude(state__in={VaultCountRun.State.COMPLETE, VaultCountRun.State.FAILED})
        .update(state=VaultCountRun.State.COMPLETE, total_count=total, completed_at=now)
    )
    if not updated:
        return True
    run.state = VaultCountRun.State.COMPLETE
    run.total_count = total
    run.completed_at = now
    audit(
        "vault.count.completed",
        target=run.request.number,
        actor=run.requested_by,
        run_id=str(run.pk),
        input_sha256=run.input_sha256,
        total_count=total,
        queried_accounts_count=run.queried_accounts_count,
        matching_accounts_count=run.matching_accounts_count,
        source_counts=per_source,
    )
    _scoping_count_finished(run)
    return True


def source_count_summary(run: VaultCountRun) -> list[dict]:
    """Per-source count state for staff: never provider identifiers."""
    counts = run.source_counts or {}
    rows = []
    for source in run_sources(run):
        entry = counts.get(source)
        if source == "mail" and run.state == VaultCountRun.State.FAILED:
            entry = _unavailable(run.failure_detail or "The count failed.")
        elif source == "mail" and entry is None and run.state == VaultCountRun.State.COMPLETE:
            entry = {"state": COUNTED, "count": run.total_count}
        elif (
            entry is not None
            and entry.get("state") == COUNTING
            and run.state == VaultCountRun.State.FAILED
        ):
            entry = _unavailable(f"The count stopped before {_ITEM_NOUNS[source]} were counted.")
        state = (entry or {}).get("state") or COUNTING
        rows.append({
            "source": source,
            "label": vault.SOURCE_LABELS[source],
            "state": state,
            "count": entry.get("count") if entry and state == COUNTED else None,
            "reason": str(entry.get("reason") or "") if entry and state == UNAVAILABLE else "",
        })
    return rows


def collectable_sources(run: VaultCountRun) -> list[str]:
    """The sources a collection from this count exports.

    Every source except one that counted exactly zero, and except Drive or
    Chat on a district-wide count with no staff organizational unit, which
    Vault cannot search. Unavailable or still-counting sources are collected:
    a missing count is not evidence that nothing is there.
    """
    entire_org = run.search_scope == VaultCountRun.SearchScope.ENTIRE_ORG
    return [
        row["source"]
        for row in source_count_summary(run)
        if not (row["state"] == COUNTED and row["count"] == 0)
        and not (row["source"] != "mail" and entire_org and not run.org_unit_id)
    ]


def _nonnegative_integer(value, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}") from exc
    if parsed < 0:
        raise ValueError(f"Invalid {field_name}")
    return parsed


def _apply_operation(run: VaultCountRun, operation: dict) -> bool:
    if operation.get("error"):
        fail_vault_count(
            run.pk,
            code="provider_rejected_count",
            detail="Google Vault could not complete this count. Review access and scope.",
        )
        return True
    response = operation.get("response")
    if not isinstance(response, dict):
        fail_vault_count(
            run.pk,
            code="invalid_provider_result",
            detail="Google Vault returned an invalid count result.",
        )
        return True
    mail_result = response.get("mailCountResult") or response.get("mail_count_result") or {}
    try:
        total_count = _nonnegative_integer(
            response.get("totalCount", response.get("total_count", 0)),
            "total count",
        )
        queried = _nonnegative_integer(
            mail_result.get("queriedAccountsCount", mail_result.get("queried_accounts_count", 0)),
            "queried account count",
        )
        matching = _nonnegative_integer(
            mail_result.get("matchingAccountsCount", mail_result.get("matching_accounts_count", 0)),
            "matching account count",
        )
    except ValueError:
        fail_vault_count(
            run.pk,
            code="invalid_provider_result",
            detail="Google Vault returned an invalid count result.",
        )
        return True

    run.queried_accounts_count = queried
    run.matching_accounts_count = matching
    run.non_queryable_accounts_count = len(
        mail_result.get("nonQueryableAccounts")
        or mail_result.get("non_queryable_accounts")
        or []
    )
    run.accounts_with_errors_count = len(
        mail_result.get("accountCountErrors")
        or mail_result.get("account_count_errors")
        or []
    )
    run.account_counts = _account_counts(
        mail_result.get("accountCounts") or mail_result.get("account_counts") or []
    )
    run.failure_code = ""
    run.failure_detail = ""
    run.source_counts = {
        **(run.source_counts or {}),
        "mail": {"state": COUNTED, "count": total_count},
    }
    run.save(
        update_fields=[
            "queried_accounts_count",
            "matching_accounts_count",
            "non_queryable_accounts_count",
            "accounts_with_errors_count",
            "account_counts",
            "failure_code",
            "failure_detail",
            "source_counts",
        ]
    )
    return _complete_if_settled(run)


def _scoping_count_finished(run: VaultCountRun) -> None:
    """Hand a finished count that belongs to a scoping session onward (#36).

    Queued after commit, so the next step reads the stored result.
    """
    from .models import ScopeIteration

    session_id = (
        ScopeIteration.objects.filter(count_run=run).values_list("session_id", flat=True).first()
    )
    if session_id is None:
        return

    def continue_after_commit():
        from typing import cast

        from celery import Task

        from .tasks import continue_scoping as continue_task

        try:
            # delay(), not send_task(): it honours eager mode in tests.
            cast(Task, continue_task).delay(session_id)
        except Exception:
            logger.exception("Scoping could not be continued", extra={"session": session_id})

    transaction.on_commit(continue_after_commit)


def _account_counts(entries) -> list[dict]:
    """The busiest custodian accounts, largest first, at most 20."""
    counts = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        account = entry.get("account")
        email = account.get("email") if isinstance(account, dict) else account
        try:
            count = _nonnegative_integer(entry.get("count", 0), "account count")
        except ValueError:
            continue
        if isinstance(email, str) and "@" in email:
            counts.append({"email": email.lower(), "count": count})
    counts.sort(key=lambda row: (-row["count"], row["email"]))
    return counts[:20]


def advance_vault_count(run_id) -> bool:
    """Poll every source's provider work. Return True on a terminal result.

    A settled run is still visited for one thing: a count-only export whose
    delete failed earlier is deleted again.
    """
    with transaction.atomic():
        run = VaultCountRun.objects.select_for_update().get(pk=run_id)
        if run.state in {VaultCountRun.State.COMPLETE, VaultCountRun.State.FAILED}:
            _advance_other_sources(run)
            return True
        if "mail" not in (run.source_counts or {}):
            if not run.operation_name:
                raise ValueError("The Vault count has no provider operation.")
            operation = vault.get_operation(run.operation_name)
            _advance_other_sources(run)
            if not operation.get("done"):
                return False
            return _apply_operation(run, operation)
        _advance_other_sources(run)
        return _complete_if_settled(run)


def runs_waiting_on_sources():
    """Runs the export poller advances: Gmail done, Drive or Chat not yet,
    or a count-only export still to delete."""
    pending_delete = Q(source_counts__drive__export_deleted=False) | Q(
        source_counts__chat__export_deleted=False
    )
    return VaultCountRun.objects.filter(
        Q(state=VaultCountRun.State.RUNNING, source_counts__has_key="mail")
        | pending_delete
    ).values_list("pk", flat=True)


def fail_vault_count(run_id, *, code: str, detail: str) -> None:
    run = VaultCountRun.objects.select_related("request", "requested_by").get(pk=run_id)
    if run.state == VaultCountRun.State.COMPLETE:
        return
    run.state = VaultCountRun.State.FAILED
    run.failure_code = str(code)[:100]
    run.failure_detail = str(detail)[:255]
    run.completed_at = timezone.now()
    run.save(
        update_fields=["state", "failure_code", "failure_detail", "completed_at"]
    )
    audit(
        "vault.count.failed",
        target=run.request.number,
        actor=run.requested_by,
        run_id=str(run.pk),
        input_sha256=run.input_sha256,
        failure_code=run.failure_code,
    )
    counts = run.source_counts or {}
    if any(entry.get("export_id") for entry in counts.values()):
        run.source_counts = {
            source: _delete_count_export(run, entry) for source, entry in counts.items()
        }
        run.save(update_fields=["source_counts"])
    _scoping_count_finished(run)
