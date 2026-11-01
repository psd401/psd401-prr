"""Keyless, mailbox-pinned Gmail API transport.

Production exchanges its AWS runtime identity through Google Workload Identity
Federation and impersonates the dedicated ``prr-gmail`` service account.
Workspace domain-wide delegation then acts only as
``runtime.get("PRR_GMAIL_MAILBOX")``. Local developers may use a service-account key
file only while Django ``DEBUG`` is true.

The delegated scopes are deliberately limited to Gmail send and modify. The
modify scope is required by the inbound poller to label processed messages.
Each exported message and label operation writes a content-free audit row.
Queries, RFC822 bytes, requester or recipient addresses, and label names are
never copied there; the configured staff mailbox may identify the audit target.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Final

import google.auth
import google_auth_httplib2
import httplib2
from django.conf import settings
from google.auth import impersonated_credentials
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from apps.audit.models import audit
from apps.configuration import runtime
from apps.correspondence import services as correspondence_services

logger = logging.getLogger(__name__)

GMAIL_SEND_SCOPE: Final = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.modify"
SCOPES: Final = [GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE]
IAM_SCOPE: Final = "https://www.googleapis.com/auth/cloud-platform"
IMPERSONATION_LIFETIME_SECONDS: Final = 3600

EXPORTED_OPERATIONS: Final = (
    "send_raw",
    "find_by_rfc822_message_id",
    "list_history",
    "list_messages",
    "get_raw",
    "get_message",
    "current_history_id",
    "add_label",
    "ensure_label",
)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str


@dataclass(frozen=True)
class FetchedMessage:
    message_id: str
    thread_id: str
    history_id: str
    received_at: dt.datetime
    raw_bytes: bytes
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    credentials_ok: bool
    subject: str
    granted_scopes: tuple[str, ...]
    profile_email: str


class GmailUnavailable(RuntimeError):
    """A sanitized Gmail failure with an existing delivery category."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        remediation: str = "Retry after checking Gmail transport configuration.",
    ):
        retryable = category in correspondence_services.RETRYABLE_DELIVERY_FAILURES
        permanent = category in correspondence_services.PERMANENT_DELIVERY_FAILURES
        if not (retryable or permanent):
            raise ValueError("Gmail error category must be a delivery failure category.")
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.remediation = remediation


def _credentials():
    """Resolve one credential delegated only to the configured mailbox."""
    mailbox = runtime.get("PRR_GMAIL_MAILBOX")
    target = runtime.get("PRR_GMAIL_IMPERSONATION_TARGET")
    credentials_file = runtime.get("PRR_GMAIL_CREDENTIALS_FILE")
    try:
        if target:
            source_credentials, _ = google.auth.default(scopes=[IAM_SCOPE])
            return impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=target,
                target_scopes=SCOPES,
                subject=mailbox,
                lifetime=IMPERSONATION_LIFETIME_SECONDS,
            )

        if not settings.DEBUG and credentials_file:
            raise GmailUnavailable(
                "A local Gmail key file is forbidden outside debug mode.",
                category="authentication_failed",
                remediation=(
                    "Configure PRR_GMAIL_IMPERSONATION_TARGET and workload "
                    "identity federation; do not deploy a private key."
                ),
            )
        if not settings.DEBUG:
            raise GmailUnavailable(
                "PRR_GMAIL_IMPERSONATION_TARGET is required outside debug mode.",
                category="authentication_failed",
                remediation=(
                    "Set PRR_GMAIL_IMPERSONATION_TARGET to the delegated service "
                    "account and configure workload identity federation."
                ),
            )
        if not credentials_file:
            raise GmailUnavailable(
                "Gmail credentials could not be resolved.",
                category="authentication_failed",
                remediation=(
                    "Set PRR_GMAIL_CREDENTIALS_FILE for a local debug run or "
                    "configure workload identity federation."
                ),
            )
        return service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=SCOPES,
        ).with_subject(runtime.get("PRR_GMAIL_MAILBOX"))
    except GmailUnavailable:
        raise
    except (GoogleAuthError, OSError, ValueError) as exc:
        raise GmailUnavailable(
            "Gmail credentials could not be resolved.",
            category="authentication_failed",
            remediation=(
                "Check the workload identity binding or the local debug key path."
            ),
        ) from exc


def _gmail_service(*, credentials=None):
    try:
        resolved_credentials = credentials or _credentials()
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            resolved_credentials,
            http=httplib2.Http(timeout=runtime.get("PRR_GMAIL_TIMEOUT_SECONDS")),
        )
        return build(
            "gmail",
            "v1",
            http=authorized_http,
            cache_discovery=False,
        )
    except GmailUnavailable:
        raise
    except (GoogleAuthError, httplib2.HttpLib2Error, OSError, ValueError) as exc:
        raise GmailUnavailable(
            "The Gmail client could not be initialized.",
            category="provider_unavailable",
            remediation=(
                "Check workload identity availability, IAM Credentials API access, "
                "and PRR_GMAIL_TIMEOUT_SECONDS."
            ),
        ) from exc


def _http_failure(status_code: int) -> GmailUnavailable:
    if status_code == 429:
        category = "rate_limited"
    elif status_code in {502, 503, 504}:
        category = "provider_unavailable"
    elif 500 <= status_code <= 599:
        category = "temporary_provider_failure"
    elif status_code in {401, 403}:
        category = "authentication_failed"
    elif status_code == 400:
        category = "message_rejected"
    else:
        category = "permanent_provider_failure"

    if category in correspondence_services.RETRYABLE_DELIVERY_FAILURES:
        remediation = "Retry with the same durable delivery identity after backoff."
    elif category == "authentication_failed":
        remediation = (
            "Verify the service-account impersonation binding, domain-wide "
            "delegation scopes, and configured mailbox."
        )
    else:
        remediation = "Verify the Gmail request and mailbox configuration."
    return GmailUnavailable(
        "The Gmail API rejected the operation.",
        category=category,
        status_code=status_code,
        remediation=remediation,
    )


def _execute(request):
    try:
        return request.execute()
    except HttpError as exc:
        status_code = int(getattr(exc.resp, "status", 0) or 0)
        failure = _http_failure(status_code)
        logger.warning(
            "Gmail API operation failed",
            extra={
                "gmail_status_code": status_code,
                "gmail_failure_category": failure.category,
            },
        )
        raise failure from exc
    except (GoogleAuthError, httplib2.HttpLib2Error, OSError, TimeoutError) as exc:
        logger.warning(
            "Gmail API transport unavailable",
            extra={"gmail_failure_category": "provider_unavailable"},
        )
        raise GmailUnavailable(
            "The Gmail API is temporarily unavailable.",
            category="provider_unavailable",
            remediation="Retry after checking network and Google API availability.",
        ) from exc


def _credentials_have_scopes(credentials, required_scopes: list[str]) -> bool:
    """Check scopes on both ordinary and impersonated Google credentials.

    google-auth keeps impersonated credentials' effective scopes in
    ``_target_scopes`` while its inherited ``has_scopes`` implementation reads
    ``scopes`` instead. Prefer the target list when it is present so the
    verifier tests the same scopes that will be requested from IAM Credentials.
    """
    target_scopes = getattr(credentials, "_target_scopes", None)
    if isinstance(target_scopes, (list, tuple, set, frozenset)):
        return set(required_scopes).issubset(set(target_scopes))
    return credentials.has_scopes(required_scopes)


def _sent_message(response) -> SentMessage:
    if not isinstance(response, dict):
        raise GmailUnavailable(
            "The Gmail API returned an invalid message response.",
            category="permanent_provider_failure",
        )
    message_id = str(response.get("id", "")).strip()
    thread_id = str(response.get("threadId", "")).strip()
    if not message_id or not thread_id:
        raise GmailUnavailable(
            "The Gmail API returned an incomplete message response.",
            category="permanent_provider_failure",
        )
    return SentMessage(message_id=message_id, thread_id=thread_id)


def send_raw(raw_bytes: bytes, *, thread_id: str | None = None) -> SentMessage:
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ValueError("raw_bytes must be non-empty bytes.")
    body = {"raw": base64.urlsafe_b64encode(raw_bytes).decode("ascii")}
    if thread_id:
        body["threadId"] = str(thread_id)
    response = _execute(
        _gmail_service().users().messages().send(userId="me", body=body)
    )
    sent = _sent_message(response)
    audit(
        "gmail.message.sent",
        target=sent.message_id,
        thread_id=sent.thread_id,
    )
    logger.info(
        "Gmail message sent",
        extra={"gmail_message_id": sent.message_id, "gmail_thread_id": sent.thread_id},
    )
    return sent


def find_by_rfc822_message_id(message_id: str) -> SentMessage | None:
    normalized = str(message_id).strip()
    if (
        not normalized
        or len(normalized) > 998
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise ValueError("message_id must be one valid RFC822 Message-ID value.")
    response = _execute(
        _gmail_service()
        .users()
        .messages()
        .list(
            userId="me",
            q=f"rfc822msgid:{normalized}",
            maxResults=1,
        )
    )
    if not isinstance(response, dict):
        raise GmailUnavailable(
            "The Gmail API returned an invalid message-list response.",
            category="permanent_provider_failure",
        )
    messages = response.get("messages", [])
    if not isinstance(messages, list):
        raise GmailUnavailable(
            "The Gmail API returned an invalid message-list response.",
            category="permanent_provider_failure",
        )
    if not messages:
        audit(
            "gmail.message.looked_up",
            target=runtime.get("PRR_GMAIL_MAILBOX"),
            found=False,
        )
        logger.info("Gmail message lookup completed", extra={"gmail_found": False})
        return None
    found = _sent_message(messages[0])
    audit("gmail.message.looked_up", target=found.message_id, found=True)
    logger.info(
        "Gmail message lookup completed",
        extra={"gmail_found": True, "gmail_message_id": found.message_id},
    )
    return found


def list_history(start_history_id: str) -> dict:
    normalized = str(start_history_id).strip()
    if not normalized:
        raise ValueError("start_history_id is required.")
    resource = _gmail_service().users().history()
    history = []
    latest_history_id = normalized
    page_token = None
    seen_tokens = set()
    while True:
        options = {
            "userId": "me",
            "startHistoryId": normalized,
            "historyTypes": ["messageAdded"],
            "labelId": "INBOX",
        }
        if page_token:
            options["pageToken"] = page_token
        response = _execute(resource.list(**options))
        if not isinstance(response, dict):
            raise GmailUnavailable(
                "The Gmail API returned an invalid history response.",
                category="permanent_provider_failure",
            )
        page_history = response.get("history", [])
        if not isinstance(page_history, list):
            raise GmailUnavailable(
                "The Gmail API returned an invalid history response.",
                category="permanent_provider_failure",
            )
        history.extend(page_history)
        latest_history_id = str(response.get("historyId") or latest_history_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        if page_token in seen_tokens:
            raise GmailUnavailable(
                "The Gmail API repeated a history page token.",
                category="permanent_provider_failure",
            )
        seen_tokens.add(page_token)
    result = {"history": history, "historyId": latest_history_id}
    audit(
        "gmail.history.listed",
        target=runtime.get("PRR_GMAIL_MAILBOX"),
        history_count=len(history),
        history_id=latest_history_id,
    )
    logger.info(
        "Gmail history listed",
        extra={
            "gmail_history_count": len(history),
            "gmail_history_id": latest_history_id,
        },
    )
    return result


def list_messages(query: str) -> list[SentMessage]:
    normalized = str(query).strip()
    if not normalized:
        raise ValueError("query is required.")
    resource = _gmail_service().users().messages()
    found = []
    page_token = None
    seen_tokens = set()
    while True:
        options = {"userId": "me", "q": normalized}
        if page_token:
            options["pageToken"] = page_token
        response = _execute(resource.list(**options))
        if not isinstance(response, dict):
            raise GmailUnavailable(
                "The Gmail API returned an invalid message-list response.",
                category="permanent_provider_failure",
            )
        page_messages = response.get("messages", [])
        if not isinstance(page_messages, list):
            raise GmailUnavailable(
                "The Gmail API returned an invalid message-list response.",
                category="permanent_provider_failure",
            )
        found.extend(_sent_message(message) for message in page_messages)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        if page_token in seen_tokens:
            raise GmailUnavailable(
                "The Gmail API repeated a message-list page token.",
                category="permanent_provider_failure",
            )
        seen_tokens.add(page_token)
    audit(
        "gmail.messages.listed",
        target=runtime.get("PRR_GMAIL_MAILBOX"),
        message_count=len(found),
    )
    logger.info(
        "Gmail messages listed",
        extra={"gmail_message_count": len(found)},
    )
    return found


def get_raw(message_id: str) -> bytes:
    normalized = str(message_id).strip()
    if not normalized:
        raise ValueError("message_id is required.")
    response = _execute(
        _gmail_service()
        .users()
        .messages()
        .get(userId="me", id=normalized, format="raw")
    )
    encoded = response.get("raw") if isinstance(response, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise GmailUnavailable(
            "The Gmail API returned an invalid raw-message response.",
            category="permanent_provider_failure",
        )
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise GmailUnavailable(
            "The Gmail API returned invalid base64url message data.",
            category="permanent_provider_failure",
        ) from exc
    audit("gmail.message.read", target=normalized, byte_count=len(raw))
    logger.info(
        "Gmail raw message read",
        extra={"gmail_message_id": normalized, "gmail_byte_count": len(raw)},
    )
    return raw


def get_message(message_id: str) -> FetchedMessage:
    """Fetch original RFC822 bytes with authoritative Gmail transport metadata."""
    normalized = str(message_id).strip()
    if not normalized:
        raise ValueError("message_id is required.")
    response = _execute(
        _gmail_service()
        .users()
        .messages()
        .get(userId="me", id=normalized, format="raw")
    )
    if not isinstance(response, dict):
        raise GmailUnavailable(
            "The Gmail API returned an invalid raw-message response.",
            category="permanent_provider_failure",
        )
    response_id = str(response.get("id", "")).strip()
    thread_id = str(response.get("threadId", "")).strip()
    history_id = str(response.get("historyId", "")).strip()
    internal_date = str(response.get("internalDate", "")).strip()
    encoded = response.get("raw")
    encoded_limit = 4 * (
        (runtime.get("PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES") + 2) // 3
    )
    if (
        response_id != normalized
        or not thread_id
        or not history_id.isdigit()
        or not internal_date.isdigit()
        or not isinstance(encoded, str)
        or not encoded
    ):
        raise GmailUnavailable(
            "The Gmail API returned an incomplete raw-message response.",
            category="permanent_provider_failure",
        )
    if len(encoded) > encoded_limit:
        raise GmailUnavailable(
            "The Gmail message exceeds the configured inbound size limit.",
            category="message_rejected",
        )
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        received_at = dt.datetime.fromtimestamp(
            int(internal_date) / 1000,
            tz=dt.UTC,
        )
    except (
        UnicodeEncodeError,
        binascii.Error,
        OverflowError,
        OSError,
        ValueError,
    ) as exc:
        raise GmailUnavailable(
            "The Gmail API returned invalid raw-message metadata.",
            category="permanent_provider_failure",
        ) from exc
    audit(
        "gmail.message.read",
        target=normalized,
        byte_count=len(raw),
        history_id=history_id,
    )
    logger.info(
        "Gmail raw message and transport metadata read",
        extra={
            "gmail_message_id": normalized,
            "gmail_byte_count": len(raw),
            "gmail_history_id": history_id,
        },
    )
    return FetchedMessage(
        message_id=response_id,
        thread_id=thread_id,
        history_id=history_id,
        received_at=received_at,
        raw_bytes=raw,
        label_ids=_label_ids(response.get("labelIds")),
    )


def _label_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(label for label in value if isinstance(label, str) and label)


def current_history_id() -> str:
    """Read the mailbox's current history boundary without exposing messages."""
    response = _execute(
        _gmail_service().users().getProfile(userId="me")
    )
    history_id = (
        str(response.get("historyId", "")).strip()
        if isinstance(response, dict)
        else ""
    )
    if not history_id.isdigit():
        raise GmailUnavailable(
            "The Gmail API returned an invalid mailbox history identifier.",
            category="permanent_provider_failure",
        )
    audit(
        "gmail.history.current",
        target=runtime.get("PRR_GMAIL_MAILBOX"),
        history_id=history_id,
    )
    logger.info(
        "Gmail current history identifier read",
        extra={"gmail_history_id": history_id},
    )
    return history_id


def ensure_label(label: str) -> str:
    normalized = str(label).strip()
    if (
        not normalized
        or len(normalized) > 225
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError("label must be a non-empty Gmail label name.")
    resource = _gmail_service().users().labels()
    response = _execute(resource.list(userId="me"))
    if not isinstance(response, dict) or not isinstance(
        response.get("labels", []),
        list,
    ):
        raise GmailUnavailable(
            "The Gmail API returned an invalid label response.",
            category="permanent_provider_failure",
        )
    labels = response.get("labels", [])
    for existing in labels:
        if isinstance(existing, dict) and existing.get("name") == normalized:
            label_id = str(existing.get("id", "")).strip()
            if label_id:
                audit("gmail.label.ensured", target=label_id, created=False)
                logger.info(
                    "Gmail label ensured",
                    extra={"gmail_label_id": label_id, "gmail_label_created": False},
                )
                return label_id
    created = _execute(
        resource.create(
            userId="me",
            body={
                "name": normalized,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
    )
    label_id = str(created.get("id", "")).strip() if isinstance(created, dict) else ""
    if not label_id:
        raise GmailUnavailable(
            "The Gmail API returned an incomplete label response.",
            category="permanent_provider_failure",
        )
    audit("gmail.label.ensured", target=label_id, created=True)
    logger.info(
        "Gmail label ensured",
        extra={"gmail_label_id": label_id, "gmail_label_created": True},
    )
    return label_id


def add_label(message_id: str, label: str, *, archive: bool = False) -> None:
    """Apply one label, optionally removing INBOX in the same modify call.

    Archiving is mailbox hygiene, not a processing state: the caller applies
    the label only after the message is durably received, so removing INBOX
    here can never drop a message that was not already recorded.
    """
    normalized = str(message_id).strip()
    if not normalized:
        raise ValueError("message_id is required.")
    label_id = ensure_label(label)
    remove_label_ids = ["INBOX"] if archive else []
    _execute(
        _gmail_service()
        .users()
        .messages()
        .modify(
            userId="me",
            id=normalized,
            body={"addLabelIds": [label_id], "removeLabelIds": remove_label_ids},
        )
    )
    audit(
        "gmail.label.added",
        target=normalized,
        label_id=label_id,
        archived=bool(archive),
    )
    logger.info(
        "Gmail label added",
        extra={
            "gmail_message_id": normalized,
            "gmail_label_id": label_id,
            "gmail_archived": bool(archive),
        },
    )


def verify_configuration() -> VerificationReport:
    """Verify credentials, pinned subject, requested scopes, and mailbox profile.

    This deliberately never calls ``messages.send``.
    """
    credentials = _credentials()
    subject = str(
        getattr(credentials, "subject", None)
        or getattr(credentials, "_subject", "")
        or ""
    ).strip()
    if subject.casefold() != runtime.get("PRR_GMAIL_MAILBOX").casefold():
        raise GmailUnavailable(
            "The delegated Gmail subject does not match PRR_GMAIL_MAILBOX.",
            category="authentication_failed",
            remediation="Pin domain-wide delegation to PRR_GMAIL_MAILBOX.",
        )
    if not _credentials_have_scopes(credentials, SCOPES):
        raise GmailUnavailable(
            "The Gmail credential is missing a required scope.",
            category="authentication_failed",
            remediation=(
                "Grant exactly gmail.send and gmail.modify in Workspace domain-wide "
                "delegation, then retry."
            ),
        )
    profile = _execute(
        _gmail_service(credentials=credentials).users().getProfile(userId="me")
    )
    profile_email = (
        str(profile.get("emailAddress", "")).strip()
        if isinstance(profile, dict)
        else ""
    )
    if profile_email.casefold() != runtime.get("PRR_GMAIL_MAILBOX").casefold():
        raise GmailUnavailable(
            "The Gmail profile does not match PRR_GMAIL_MAILBOX.",
            category="authentication_failed",
            remediation=(
                "Confirm PRR_GMAIL_MAILBOX is a real Workspace mailbox and the "
                "delegated subject uses that exact address."
            ),
        )
    return VerificationReport(
        credentials_ok=True,
        subject=subject,
        granted_scopes=tuple(SCOPES),
        profile_email=profile_email,
    )
