"""
Google Vault client — the automated collection engine.

Flow per CollectionTask:
  1. ensure_matter()      -> one Vault matter per PRR (preservation container)
  2. create_hold()        -> immediately preserve custodian data (do this FIRST;
                             spoliation risk starts the moment a request lands)
  3. ensure_source_export() -> one export per source: Gmail, Drive, Chat
  4. poll_export()        -> Celery beat polls until COMPLETED
  5. download_export()    -> export files land in a Google-managed Cloud Storage
                             bucket; fetch via the Cloud Storage API and stream
                             into our object store, then dissolve mbox/zip into
                             RecordItems (see tasks.py).

Auth: service account with domain-wide delegation, impersonating a dedicated
Vault-admin user. Scopes:
    https://www.googleapis.com/auth/ediscovery
    https://www.googleapis.com/auth/devstorage.read_only
Grant the impersonated user only the Vault privileges needed (Manage Matters,
Manage Holds, Manage Searches, Manage Exports). Log every call to AuditLog.

NOTE: MBOX exports process faster than PST — use MBOX.

Counting: Gmail uses ``matters.count`` (``start_mail_count()``). Vault
rejects ``matters.count`` for DRIVE and HANGOUTS_CHAT with HTTP 400 "Corpus
type ... is not supported" (production probe, 2026-11-08), so those sources
are counted with ``ensure_count_export()``: a count-only export whose
``stats.totalArtifactCount`` is read once it is COMPLETED, then
``delete_export()``. A count-only export is never downloaded.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Final

import google.auth
from django.conf import settings
from google.auth import impersonated_credentials
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from googleapiclient.discovery import build

from apps.audit.models import audit
from apps.configuration import runtime

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/ediscovery",
    "https://www.googleapis.com/auth/devstorage.read_only",
]
IAM_SCOPE: Final = "https://www.googleapis.com/auth/cloud-platform"
IMPERSONATION_LIFETIME_SECONDS: Final = 3600


class VaultUnavailable(RuntimeError):
    """Sanitized Vault authentication or provider configuration failure."""


def vault_credentials():
    """Return the same narrowly scoped delegated identity for Vault and GCS."""
    admin_user = runtime.get("PRR_VAULT_ADMIN_USER")
    target = runtime.get("PRR_VAULT_IMPERSONATION_TARGET")
    credentials_file = runtime.get("PRR_VAULT_CREDENTIALS_FILE")
    try:
        if target:
            source_credentials, _ = google.auth.default(scopes=[IAM_SCOPE])
            return impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=target,
                target_scopes=SCOPES,
                subject=admin_user,
                lifetime=IMPERSONATION_LIFETIME_SECONDS,
            )
        if not settings.DEBUG and credentials_file:
            raise VaultUnavailable(
                "A local Google Vault private key is forbidden outside debug mode."
            )
        if not settings.DEBUG:
            raise VaultUnavailable(
                "PRR_VAULT_IMPERSONATION_TARGET is required outside debug mode."
            )
        if not credentials_file:
            raise VaultUnavailable("Google Vault credentials are not configured.")
        return service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=SCOPES,
        ).with_subject(admin_user)
    except VaultUnavailable:
        raise
    except (GoogleAuthError, OSError, ValueError) as exc:
        raise VaultUnavailable(
            "Google Vault credentials could not be resolved."
        ) from exc


def _vault_service():
    return build(
        "vault",
        "v1",
        credentials=vault_credentials(),
        cache_discovery=False,
    )


def _e2e_stand_in() -> bool:
    """A deterministic stand-in for Vault in browser acceptance runs only.

    Only ``config.e2e_settings`` sets ``PRR_E2E_FAKE_VAULT``, and it is
    honoured only while the test-fixture switch is on as well, so no deployed
    environment reaches it. It answers the calls the count and collection
    paths make with synthetic identifiers and totals, and never anything else.
    """
    return bool(
        getattr(settings, "PRR_E2E_FAKE_VAULT", False)
        and getattr(settings, "PRR_TEST_FIXTURE_SEED_ENABLED", False)
    )


def _e2e_id(prefix: str, *parts) -> str:
    return f"e2e-{prefix}-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]


def ensure_matter(request_number: str, description: str, *, actor=None) -> str:
    """Create (or reuse) the Vault matter for a PRR. Returns matterId."""
    if _e2e_stand_in():
        return _e2e_id("matter", request_number)
    svc = _vault_service()
    matter_name = f"{request_number} — Public Records Request"
    matching_matters = []
    page_token = None
    while True:
        list_options = {
            "pageSize": 100,
            "state": "OPEN",
            "view": "BASIC",
        }
        if page_token:
            list_options["pageToken"] = page_token
        response = svc.matters().list(**list_options).execute()
        matching_matters.extend(
            matter
            for matter in response.get("matters", [])
            if matter.get("name") == matter_name and matter.get("matterId")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if matching_matters:
        matter_id = min(
            matching_matters,
            key=lambda matter: str(matter["matterId"]),
        )["matterId"]
        audit(
            "vault.matter.reused",
            target=request_number,
            actor=actor,
            matter_id=matter_id,
        )
        logger.info("Vault matter reused", extra={"matter_id": matter_id})
        return matter_id

    matter = svc.matters().create(body={
        "name": matter_name,
        "description": description[:1000],
    }).execute()
    matter_id = matter["matterId"]
    audit(
        "vault.matter.created",
        target=request_number,
        actor=actor,
        matter_id=matter_id,
    )
    logger.info("Vault matter created", extra={"matter_id": matter_id})
    return matter_id


def create_mail_hold(matter_id: str, custodian_emails: list[str], name: str) -> str:
    """Place a hold on custodians' Gmail so nothing responsive is purged."""
    svc = _vault_service()
    hold = svc.matters().holds().create(matterId=matter_id, body={
        "name": name,
        "corpus": "MAIL",
        "accounts": [{"email": e} for e in custodian_emails],
        "query": {"mailQuery": {}},   # hold everything; searches narrow later
    }).execute()
    hold_id = hold["holdId"]
    audit(
        "vault.hold.created",
        target=matter_id,
        hold_id=hold_id,
        custodian_count=len(custodian_emails),
    )
    return hold_id


def ensure_mail_hold(
    matter_id: str,
    custodian_emails: list[str],
    *,
    name: str,
) -> str:
    """Create or reuse the deterministic targeted hold for one collection.

    A provider call can succeed before the worker records its ID. Reusing the
    stable name keeps a retry from multiplying legal holds.
    """
    if _e2e_stand_in():
        return _e2e_id("hold", matter_id, name, *sorted(custodian_emails))
    svc = _vault_service()
    matching_holds = []
    page_token = None
    while True:
        options = {"matterId": matter_id, "pageSize": 100}
        if page_token:
            options["pageToken"] = page_token
        response = svc.matters().holds().list(**options).execute()
        matching_holds.extend(
            hold
            for hold in response.get("holds", [])
            if hold.get("name") == name and hold.get("holdId")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if matching_holds:
        hold_id = min(
            matching_holds,
            key=lambda hold: str(hold["holdId"]),
        )["holdId"]
        audit(
            "vault.hold.reused",
            target=matter_id,
            hold_id=hold_id,
            custodian_count=len(custodian_emails),
        )
        return hold_id
    return create_mail_hold(matter_id, custodian_emails, name)


def create_mail_export(
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    start_time: str | None = None,   # RFC3339, e.g. "2024-01-01T00:00:00Z"
    end_time: str | None = None,
    name: str = "PRR mail export",
    entire_org: bool = False,
) -> str:
    """Search + export Gmail for the explicit reviewed scope. Returns exportId."""
    svc = _vault_service()
    query = _mail_query(
        custodian_emails,
        terms,
        start_time=start_time,
        end_time=end_time,
        entire_org=entire_org,
    )

    export = svc.matters().exports().create(matterId=matter_id, body={
        "name": name,
        "query": query,
        "exportOptions": {"mailOptions": {"exportFormat": "MBOX"}},
    }).execute()
    export_id = export["id"]
    audit(
        "vault.export.created",
        target=matter_id,
        export_id=export_id,
        corpus="MAIL",
        search_scope="entire_org" if entire_org else "accounts",
        custodian_count=len(custodian_emails),
    )
    return export_id


def ensure_mail_export(
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    name: str = "PRR mail export",
    entire_org: bool = False,
) -> str:
    """Create or reuse the deterministic export for a reviewed count.

    The Vault create call can succeed even if the worker loses its database
    connection before retaining the export ID. A retry therefore resolves the
    stable per-count name first instead of creating duplicate exports.
    """
    if _e2e_stand_in():
        return _e2e_id("export", matter_id, name, terms)
    svc = _vault_service()
    matching_exports = []
    page_token = None
    while True:
        options = {"matterId": matter_id, "pageSize": 100}
        if page_token:
            options["pageToken"] = page_token
        response = svc.matters().exports().list(**options).execute()
        matching_exports.extend(
            export
            for export in response.get("exports", [])
            if (
                export.get("name") == name
                and export.get("id")
                and export.get("status") != "FAILED"
            )
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if matching_exports:
        export_id = min(
            matching_exports,
            key=lambda export: str(export["id"]),
        )["id"]
        audit(
            "vault.export.reused",
            target=matter_id,
            export_id=export_id,
            corpus="MAIL",
            search_scope="entire_org" if entire_org else "accounts",
            custodian_count=len(custodian_emails),
        )
        return export_id
    return create_mail_export(
        matter_id,
        custodian_emails,
        terms,
        start_time=start_time,
        end_time=end_time,
        name=name,
        entire_org=entire_org,
    )


def _mail_query(
    custodian_emails: list[str],
    terms: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
) -> dict:
    query = {
        "corpus": "MAIL",
        "dataScope": "ALL_DATA",
        "method": "ENTIRE_ORG" if entire_org else "ACCOUNT",
        "terms": terms,
        "mailOptions": {"excludeDrafts": False},  # drafts can be public records
    }
    if not entire_org:
        query["accountInfo"] = {"emails": custodian_emails}
    if start_time:
        query["startTime"] = start_time
    if end_time:
        query["endTime"] = end_time
    return query



def start_mail_count(
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
) -> dict:
    """Start a count-only Gmail search; no hold, export, or saved query."""
    if _e2e_stand_in():
        accounts = custodian_emails or ["records@psd401.net"]
        total = 128 if entire_org else 37 * len(accounts)
        return {
            "name": _e2e_id("operation", matter_id, terms),
            "done": True,
            "response": {
                "totalCount": str(total),
                "mailCountResult": {
                    "queriedAccountsCount": str(len(accounts)),
                    "matchingAccountsCount": str(len(accounts)),
                    "accountCounts": [
                        {"account": {"email": email}, "count": str(total // len(accounts))}
                        for email in accounts
                    ],
                },
            },
        }
    svc = _vault_service()
    return svc.matters().count(
        matterId=matter_id,
        body={
            "query": _mail_query(
                custodian_emails,
                terms,
                start_time=start_time,
                end_time=end_time,
                entire_org=entire_org,
            ),
            "view": "TOTAL_COUNT",
        },
    ).execute()


def get_operation(operation_name: str) -> dict:
    """Read the current state of one Google long-running operation."""
    return _vault_service().operations().get(name=operation_name).execute()


def get_export(matter_id: str, export_id: str) -> dict:
    """Poll export status. status becomes COMPLETED; cloudStorageSink.files
    then lists bucket/object names to download via Cloud Storage."""
    if _e2e_stand_in():
        if export_id.startswith("e2e-count-export-"):
            return {"id": export_id, "status": "COMPLETED", "stats": {"totalArtifactCount": "11"}}
        return {"id": export_id, "status": "IN_PROGRESS"}
    svc = _vault_service()
    return svc.matters().exports().get(matterId=matter_id, exportId=export_id).execute()


# --------------------------------------------------------------------------
# Every Google Workspace source a count and a collection search.
#
# Gmail keeps its long-standing functions above. Drive and Chat go through
# the same interface below, so a caller names a source and never a corpus.
# --------------------------------------------------------------------------

#: The sources, in the order staff see them.
SOURCES: Final = ("mail", "drive", "chat")
CORPUS: Final = {"mail": "MAIL", "drive": "DRIVE", "chat": "HANGOUTS_CHAT"}
SOURCE_LABELS: Final = {"mail": "Gmail", "drive": "Drive", "chat": "Chat"}
_EXPORT_OPTIONS: Final = {
    "drive": {"driveOptions": {"includeAccessInfo": False}},
    # Vault exports Chat as ZIP archives of MBOX files, which the existing
    # message ingest already reads.
    "chat": {"hangoutsChatOptions": {"exportFormat": "MBOX"}},
}
_HOLD_QUERIES: Final = {
    "drive": {"driveQuery": {"includeSharedDriveFiles": True}},
    "chat": {"hangoutsChatQuery": {"includeRooms": True}},
}


class StaffOrgUnitMissing(ValueError):
    """A district-wide Drive or Chat search needs the staff org unit.

    Google Vault's ENTIRE_ORG method is supported only for Gmail, so Drive
    and Chat search everyone in the staff organizational unit instead.
    """


def _require_source(source: str) -> None:
    if source not in CORPUS:
        raise ValueError(f"Unknown Vault source {source!r}.")


def source_query(
    source: str,
    custodian_emails: list[str],
    terms: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
    org_unit_id: str = "",
) -> dict:
    """The Vault v1 ``Query`` for one source of a reviewed scope."""
    _require_source(source)
    if source == "mail":
        return _mail_query(
            custodian_emails,
            terms,
            start_time=start_time,
            end_time=end_time,
            entire_org=entire_org,
        )
    query: dict = {
        "corpus": CORPUS[source],
        "dataScope": "ALL_DATA",
    }
    if entire_org:
        if not org_unit_id:
            raise StaffOrgUnitMissing(
                "The staff organizational unit is not set."
            )
        query["method"] = "ORG_UNIT"
        query["orgUnitInfo"] = {"orgUnitId": org_unit_id}
    else:
        query["method"] = "ACCOUNT"
        query["accountInfo"] = {"emails": list(custodian_emails)}
    query["terms"] = terms
    if source == "drive":
        query["driveOptions"] = {"sharedDrivesOption": "INCLUDED"}
    else:
        query["hangoutsChatOptions"] = {"includeRooms": True}
    if start_time:
        query["startTime"] = start_time
    if end_time:
        query["endTime"] = end_time
    return query


def _named_export_id(svc, matter_id: str, name: str) -> str:
    """The lowest-id live export of this exact name, or ``""``."""
    matching = []
    page_token = None
    while True:
        options = {"matterId": matter_id, "pageSize": 100}
        if page_token:
            options["pageToken"] = page_token
        response = svc.matters().exports().list(**options).execute()
        matching.extend(
            export
            for export in response.get("exports", [])
            if (
                export.get("name") == name
                and export.get("id")
                and export.get("status") != "FAILED"
            )
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if not matching:
        return ""
    return min(matching, key=lambda export: str(export["id"]))["id"]


def _ensure_export(
    source: str,
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    name: str,
    start_time: str | None,
    end_time: str | None,
    entire_org: bool,
    org_unit_id: str,
    created_action: str,
) -> str:
    query = source_query(
        source,
        custodian_emails,
        terms,
        start_time=start_time,
        end_time=end_time,
        entire_org=entire_org,
        org_unit_id=org_unit_id,
    )
    if _e2e_stand_in():
        prefix = "count-export" if created_action.startswith("vault.count_export") else "export"
        return _e2e_id(prefix, matter_id, source, name, terms)
    svc = _vault_service()
    existing = _named_export_id(svc, matter_id, name)
    if existing:
        audit(
            created_action.replace(".created", ".reused"),
            target=matter_id,
            export_id=existing,
            corpus=CORPUS[source],
            search_scope="entire_org" if entire_org else "accounts",
            custodian_count=len(custodian_emails),
        )
        return existing
    export = svc.matters().exports().create(matterId=matter_id, body={
        "name": name,
        "query": query,
        "exportOptions": _EXPORT_OPTIONS[source],
    }).execute()
    export_id = export["id"]
    audit(
        created_action,
        target=matter_id,
        export_id=export_id,
        corpus=CORPUS[source],
        search_scope="entire_org" if entire_org else "accounts",
        custodian_count=len(custodian_emails),
    )
    return export_id


def ensure_count_export(
    source: str,
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    name: str,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
    org_unit_id: str = "",
) -> str:
    """Create or reuse a count-only Drive or Chat export.

    Vault's ``matters.count`` does not support Drive or Chat. The count
    reads ``Export.stats.totalArtifactCount`` once the export is
    COMPLETED and then deletes it with :func:`delete_export`. A count-only
    export is never downloaded and never becomes a record.
    """
    if source not in _EXPORT_OPTIONS:
        raise ValueError("Count-only exports are for Drive and Chat.")
    return _ensure_export(
        source,
        matter_id,
        custodian_emails,
        terms,
        name=name,
        start_time=start_time,
        end_time=end_time,
        entire_org=entire_org,
        org_unit_id=org_unit_id,
        created_action="vault.count_export.created",
    )


def create_source_export(
    source: str,
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    name: str,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
    org_unit_id: str = "",
) -> str:
    """Create one Drive or Chat collection export. Returns exportId."""
    if source not in _EXPORT_OPTIONS:
        raise ValueError("Use create_mail_export for Gmail.")
    query = source_query(
        source,
        custodian_emails,
        terms,
        start_time=start_time,
        end_time=end_time,
        entire_org=entire_org,
        org_unit_id=org_unit_id,
    )
    export = _vault_service().matters().exports().create(matterId=matter_id, body={
        "name": name,
        "query": query,
        "exportOptions": _EXPORT_OPTIONS[source],
    }).execute()
    export_id = export["id"]
    audit(
        "vault.export.created",
        target=matter_id,
        export_id=export_id,
        corpus=CORPUS[source],
        search_scope="entire_org" if entire_org else "accounts",
        custodian_count=len(custodian_emails),
    )
    return export_id


def ensure_source_export(
    source: str,
    matter_id: str,
    custodian_emails: list[str],
    terms: str,
    *,
    name: str,
    start_time: str | None = None,
    end_time: str | None = None,
    entire_org: bool = False,
    org_unit_id: str = "",
) -> str:
    """Create or reuse one source's collection export for a reviewed count."""
    _require_source(source)
    if source == "mail":
        return ensure_mail_export(
            matter_id,
            custodian_emails,
            terms,
            start_time=start_time,
            end_time=end_time,
            name=name,
            entire_org=entire_org,
        )
    return _ensure_export(
        source,
        matter_id,
        custodian_emails,
        terms,
        name=name,
        start_time=start_time,
        end_time=end_time,
        entire_org=entire_org,
        org_unit_id=org_unit_id,
        created_action="vault.export.created",
    )


def ensure_source_hold(
    source: str,
    matter_id: str,
    custodian_emails: list[str],
    *,
    name: str,
) -> str:
    """Create or reuse one source's targeted hold on named accounts."""
    _require_source(source)
    if source == "mail":
        return ensure_mail_hold(matter_id, custodian_emails, name=name)
    if _e2e_stand_in():
        return _e2e_id("hold", matter_id, source, name, *sorted(custodian_emails))
    svc = _vault_service()
    matching = []
    page_token = None
    while True:
        options = {"matterId": matter_id, "pageSize": 100}
        if page_token:
            options["pageToken"] = page_token
        response = svc.matters().holds().list(**options).execute()
        matching.extend(
            hold
            for hold in response.get("holds", [])
            if hold.get("name") == name and hold.get("holdId")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if matching:
        hold_id = min(matching, key=lambda hold: str(hold["holdId"]))["holdId"]
        audit(
            "vault.hold.reused",
            target=matter_id,
            hold_id=hold_id,
            corpus=CORPUS[source],
            custodian_count=len(custodian_emails),
        )
        return hold_id
    hold = svc.matters().holds().create(matterId=matter_id, body={
        "name": name,
        "corpus": CORPUS[source],
        "accounts": [{"email": email} for email in custodian_emails],
        "query": _HOLD_QUERIES[source],
    }).execute()
    hold_id = hold["holdId"]
    audit(
        "vault.hold.created",
        target=matter_id,
        hold_id=hold_id,
        corpus=CORPUS[source],
        custodian_count=len(custodian_emails),
    )
    return hold_id


def delete_export(matter_id: str, export_id: str) -> None:
    """Delete a count-only export once its count is read.

    An export that is already gone counts as deleted.
    """
    if _e2e_stand_in():
        return
    from googleapiclient.errors import HttpError

    try:
        _vault_service().matters().exports().delete(
            matterId=matter_id,
            exportId=export_id,
        ).execute()
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 404:
            raise
    audit(
        "vault.count_export.deleted",
        target=matter_id,
        export_id=export_id,
    )


def export_artifact_total(export: dict) -> int | None:
    """``Export.stats.totalArtifactCount``, or None when it is not a count."""
    stats = export.get("stats") if isinstance(export, dict) else None
    if not isinstance(stats, dict):
        return None
    raw = stats.get("totalArtifactCount")
    if raw is None:
        return None
    try:
        total = int(str(raw))
    except ValueError:
        return None
    return total if total >= 0 else None
