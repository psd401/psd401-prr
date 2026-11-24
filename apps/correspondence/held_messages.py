# ABOUTME: Lets a Records Officer read, and release, a held (unparseable) inbound email.
# ABOUTME: Plain text only: headers and text parts; attachments are named, never opened.
"""Reading a held email.

An email the MIME parser refused is kept whole in quarantine storage, but the
app stored nothing readable about it -- no sender, no subject, no body -- so
"Held for review" listed messages nobody could review. This renders the
retained bytes as plain text for an officer: a lenient parse for headers and
text parts, falling back to the raw source itself. Nothing here executes,
renders HTML, or opens an attachment, and the read is audited.

An officer who has read it can release it (``release_held_message``): the
current parser is tried first, so a message it now accepts keeps its
attachments; one it still refuses is released as the same plain text the
officer read, its attachments left in the held original. Either way the
released email waits, unmatched, for the officer to add it to a case or open
a new request from it.
"""

from __future__ import annotations

import email
import email.policy
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.api.models import StaffRole
from apps.audit.models import audit

from . import receiving
from .authorization import require_current_human_staff
from .mime import ParsedInboundMessage
from .models import InboundCorrespondenceReceipt
from .receiving import verified_raw_message

MAX_BODY_CHARS = 100_000
SHOWN_HEADERS = ("From", "To", "Cc", "Date", "Subject")
# Kept on a text release: what intake and the case page read later.
_KEPT_HEADERS = (
    *SHOWN_HEADERS,
    "Reply-To",
    "Auto-Submitted",
    "Precedence",
    "List-Id",
)
_MIN_REASON = 10
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _CONTROL.sub("", text)


def _header(value) -> str:
    try:
        return _clean(str(make_header(decode_header(str(value)))))
    except Exception:  # noqa: BLE001 - a broken encoded-word is shown as-is
        return _clean(str(value))


def _decoded(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _require_officer(actor, message):
    # The officer check locks the staff row with select_for_update, which
    # Postgres refuses outside a transaction (SQLite silently allows it).
    with transaction.atomic():
        return require_current_human_staff(
            actor,
            roles=(StaffRole.RECORDS_OFFICER,),
            message=message,
        )


def _verified_held_raw(receipt) -> bytes:
    raw = verified_raw_message(receipt)
    if raw is None:
        raise ValidationError(
            "The stored copy of this email no longer matches the fingerprint "
            "taken when it arrived, so it is not shown."
        )
    return raw


def _lenient_read(raw: bytes):
    """Return (message or None, headers, texts, attachment names)."""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.compat32)
        headers: list[tuple[str, str]] = []
        texts: list[str] = []
        attachments: list[str] = []
        for name in SHOWN_HEADERS:
            value = message.get(name)
            if value:
                headers.append((name, _header(value)))
        for part in message.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            if filename or part.get_content_disposition() == "attachment":
                attachments.append(_header(filename or "unnamed attachment"))
                continue
            if part.get_content_type() == "text/plain":
                texts.append(_decoded(part))
            elif part.get_content_type() == "text/html" and not texts:
                texts.append(_TAGS.sub(" ", _decoded(part)))
    except Exception:  # noqa: BLE001 - fall back to the raw source
        return None, [], [], []
    return message, headers, texts, attachments


def _body(raw: bytes, texts: list[str]) -> tuple[str, bool]:
    raw_source = not texts
    body = (
        raw.decode("utf-8", errors="replace")
        if raw_source
        else "\n\n".join(texts)
    )
    return _clean(body), raw_source


def readable_held_message(receipt_id, *, actor) -> dict:
    """Return a held email's headers, text, and attachment names."""
    actor = _require_officer(
        actor, "Only a Records Officer may read a held email."
    )
    receipt = InboundCorrespondenceReceipt.objects.get(pk=receipt_id)
    if receipt.parsing_status not in {
        InboundCorrespondenceReceipt.ParsingStatus.FAILED,
        InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
    }:
        raise ValidationError(
            "This email is not held; open it from its case or the email list."
        )
    raw = _verified_held_raw(receipt)
    _message, headers, texts, attachments = _lenient_read(raw)
    body, raw_source = _body(raw, texts)
    truncated = len(body) > MAX_BODY_CHARS
    audit(
        "inbound.held_message_read",
        target=str(receipt.pk),
        actor=actor,
        raw_source=raw_source,
    )
    return {
        "headers": headers,
        "body": body[:MAX_BODY_CHARS],
        "attachments": attachments,
        "raw_source": raw_source,
        "truncated": truncated,
    }


def _addresses(message, name) -> tuple[str, ...]:
    values = [_header(value) for value in message.get_all(name, [])]
    return tuple(
        address.strip()
        for _display, address in getaddresses(values)
        if address.strip()
    )


def _as_plain_text(raw: bytes) -> tuple[ParsedInboundMessage, int]:
    """The text the officer read, shaped as a parsed message with no files."""
    message, _shown, texts, attachments = _lenient_read(raw)
    body, _raw_source = _body(raw, texts)
    headers: dict[str, str] = {}
    sender = subject = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    if message is not None:
        for name in _KEPT_HEADERS:
            value = message.get(name)
            if value:
                headers[name] = _header(value)
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        to = _addresses(message, "To")
        cc = _addresses(message, "Cc")
    parsed = ParsedInboundMessage(
        subject=subject,
        body=body[:MAX_BODY_CHARS],
        from_address=parseaddr(sender)[1],
        to_addresses=to,
        cc_addresses=cc,
        # Threading headers are left empty: an officer files this email by
        # hand, so nothing automatic reads them.
        rfc_message_id="",
        in_reply_to=(),
        references=(),
        headers=headers,
        attachments=(),
        delivery_status_report=None,
    )
    return parsed, len(attachments)


def release_held_message(receipt_id, *, actor, reason, expected_version):
    """Release a held email an officer has read; it waits for them to file it.

    The current parser is tried first. When it still refuses the message, the
    plain text the officer read is released instead and its attachments stay
    in the held original. Never matched automatically.
    """
    actor = _require_officer(
        actor, "Only a Records Officer may release a held email."
    )
    reason = (reason or "").strip()
    if len(reason) < _MIN_REASON:
        raise ValidationError("Say why this email is being released.")
    receipt = InboundCorrespondenceReceipt.objects.get(pk=receipt_id)
    if not receiving.is_whole_message_quarantine(receipt):
        raise ValidationError("This email is not held, so there is nothing to release.")
    if receipt.version != expected_version:
        from .services import VersionConflict

        raise VersionConflict(
            "This email changed since it was opened. Reload it and try again."
        )
    raw = _verified_held_raw(receipt)
    try:
        parsed = receiving.parse_inbound_message(
            raw, limits=receiving._mime_limits()
        )
        read_as, left_held = "parser", 0
    except Exception:  # noqa: BLE001 - the officer read it; release the text
        parsed, left_held = _as_plain_text(raw)
        read_as = "text"

    def record(released, correspondence, previous_reason, attachments):
        audit(
            "inbound.held_message_released",
            target=str(released.pk),
            actor=actor,
            receipt_id=str(released.pk),
            correspondence_id=str(correspondence.pk),
            previous_reason=previous_reason,
            read_as=read_as,
            attachment_count=len(attachments),
            attachments_left_held=left_held,
            reason=reason,
        )

    released = receiving.release_parsed_quarantine(
        receipt, parsed, actor=actor, match=False, on_released=record
    )
    if released is None:
        raise ValidationError("This email is not held, so there is nothing to release.")
    return released
