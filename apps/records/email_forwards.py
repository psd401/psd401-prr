"""Find who originally sent a request that reached the mailbox as a forward.

Staff (and anyone else) forward requests to the intake mailbox. The case must
open under the person who wrote the request, with the date they sent it, not
under whoever forwarded it. The forwarded header blocks are read from the
plain-text body exactly as mail clients write them; the deepest block is the
original sender. Nothing here edits the body: the whole forward stays the
request description (rule 9).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

_DISTRICT_TIMEZONE = ZoneInfo("America/Los_Angeles")
_FORWARD_SUBJECT = re.compile(r"^\s*(?:(?:re|aw)\s*:\s*)*(?:fwd?|fw)\s*:", re.IGNORECASE)
_FORWARD_MARKER = re.compile(
    r"^\s*(?:-+\s*Forwarded message\s*-+|Begin forwarded message:)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADER_LINE = re.compile(r"^[\s>]*\**\s*([A-Za-z-]+)\s*:\**\s*(.*?)\s*$")
_ANGLE_ADDRESS = re.compile(r"[<\[](?:mailto:)?([^<>\[\]\s]+@[^<>\[\]\s]+)[>\]]", re.IGNORECASE)
_BARE_ADDRESS = re.compile(r"([^<>\s\"'\[\]]+@[^<>\s\"'\[\]]+)")
_TRAILING_ZONE = re.compile(r"\s+(?:(?!AM\b|PM\b)[A-Z]{2,5}|GMT[+-]\d{1,2}(?::\d{2})?)$")
_DATE_FORMATS = (
    "%a, %b %d, %Y at %I:%M %p",
    "%a, %b %d, %Y, %I:%M %p",
    "%a, %b %d, %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M:%S %p",
    "%A, %B %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M:%S %p",
    "%B %d, %Y at %I:%M %p",
    "%b %d, %Y at %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
)
_BLOCK_WINDOW = 8


@dataclass(frozen=True)
class ForwardedOrigin:
    name: str
    email: str
    sent_at: dt.datetime | None


def _address(value: str) -> tuple[str, str] | None:
    match = _ANGLE_ADDRESS.search(value)
    if match:
        email = match.group(1)
        name = value[: match.start()]
    else:
        match = _BARE_ADDRESS.search(value)
        if match is None:
            return None
        email = match.group(1)
        name = ""
    email = email.strip().strip(".,;")
    try:
        validate_email(email)
    except ValidationError:
        return None
    name = name.replace("*", "").strip().strip("\"'").strip()
    return name, email


def _sent_at(value: str) -> dt.datetime | None:
    text = (
        value.replace("*", "")
        .replace("\N{NARROW NO-BREAK SPACE}", " ")
        .replace("\xa0", " ")
        .strip()
    )
    text = re.sub(r"\s+", " ", text)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        bare = _TRAILING_ZONE.sub("", text)
        for date_format in _DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(bare, date_format)
            except ValueError:
                continue
            break
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_DISTRICT_TIMEZONE)
    return parsed


def _header_blocks(body: str):
    lines = body.splitlines()
    for index, line in enumerate(lines):
        match = _HEADER_LINE.match(line)
        if match is None or match.group(1).casefold() != "from":
            continue
        fields = {"from": match.group(2)}
        for following in lines[index + 1 : index + _BLOCK_WINDOW]:
            field = _HEADER_LINE.match(following)
            if field is None:
                if following.strip():
                    break
                continue
            key = field.group(1).casefold()
            if key == "from":
                break
            fields.setdefault(key, field.group(2))
        if "date" in fields or "sent" in fields:
            yield fields


def original_sender(subject: str, body: str) -> ForwardedOrigin | None:
    """Return the deepest forwarded sender, or None if this is not a forward."""
    if not (_FORWARD_SUBJECT.match(subject or "") or _FORWARD_MARKER.search(body or "")):
        return None
    origin = None
    for fields in _header_blocks(body or ""):
        address = _address(fields["from"])
        if address is None:
            continue
        origin = ForwardedOrigin(
            name=address[0],
            email=address[1],
            sent_at=_sent_at(fields.get("date") or fields.get("sent") or ""),
        )
    return origin
