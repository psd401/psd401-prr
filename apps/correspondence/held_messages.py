# ABOUTME: Lets a Records Officer read a held (unparseable) inbound email.
# ABOUTME: Plain text only: headers and text parts; attachments are named, never opened.
"""Reading a held email.

An email the MIME parser refused is kept whole in quarantine storage, but the
app stored nothing readable about it -- no sender, no subject, no body -- so
"Held for review" listed messages nobody could review. This renders the
retained bytes as plain text for an officer: a lenient parse for headers and
text parts, falling back to the raw source itself. Nothing here executes,
renders HTML, or opens an attachment, and the read is audited.
"""

from __future__ import annotations

import email
import email.policy
import re
from email.header import decode_header, make_header

from django.core.exceptions import ValidationError

from apps.api.models import StaffRole
from apps.audit.models import audit

from .authorization import require_current_human_staff
from .models import InboundCorrespondenceReceipt
from .receiving import verified_raw_message

MAX_BODY_CHARS = 100_000
SHOWN_HEADERS = ("From", "To", "Cc", "Date", "Subject")
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


def readable_held_message(receipt_id, *, actor) -> dict:
    """Return a held email's headers, text, and attachment names."""
    actor = require_current_human_staff(
        actor,
        roles=(StaffRole.RECORDS_OFFICER,),
        message="Only a Records Officer may read a held email.",
    )
    receipt = InboundCorrespondenceReceipt.objects.get(pk=receipt_id)
    if receipt.parsing_status not in {
        InboundCorrespondenceReceipt.ParsingStatus.FAILED,
        InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
    }:
        raise ValidationError(
            "This email is not held; open it from its case or the email list."
        )
    raw = verified_raw_message(receipt)
    if raw is None:
        raise ValidationError(
            "The stored copy of this email no longer matches the fingerprint "
            "taken when it arrived, so it is not shown."
        )

    headers: list[tuple[str, str]] = []
    texts: list[str] = []
    attachments: list[str] = []
    try:
        message = email.message_from_bytes(raw, policy=email.policy.compat32)
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
    except Exception:  # noqa: BLE001 - fall back to the raw source below
        headers, texts, attachments = [], [], []

    raw_source = not texts
    body = (
        raw.decode("utf-8", errors="replace")
        if raw_source
        else "\n\n".join(texts)
    )
    body = _clean(body)
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
