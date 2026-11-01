# ABOUTME: Parse the RFC 5322 headers a reviewer needs from a retained message
# ABOUTME: so review throughput features read stored facts, not raw bytes.
"""Email metadata extraction for retained record messages.

One module, used by both Vault ingest and the backfill command, so a record
created today and a record migrated from a retained raw message describe
themselves identically. Everything here is a *fact about the message* — who
sent it, who received it, when, and which structural headers it carried. No
judgement about responsiveness, exemption, or privilege is made here; the
deterministic tags in :mod:`apps.records.record_tags` are derived from these
facts, and every legal decision stays with a named human.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

#: Control characters never belong in a stored header value.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Field widths mirror the model columns so a hostile header cannot overflow
#: one. RFC 5322 caps a Message-ID line at 998 octets.
MAX_MESSAGE_ID = 998
MAX_ADDRESS_LIST = 2000
MAX_HEADER = 500
MAX_DOMAIN = 253
#: The custodian column's width. ``Delivered-To`` is read to name a mailbox,
#: so it is bounded by where a mailbox is written, not by a header width.
MAX_ACCOUNT = 255

#: Tags and markup stripped before the body is fingerprinted, so an HTML and a
#: plain-text alternative of the same words hash the same way.
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class EmailMetadata:
    """The stored shape of one message's reviewer-relevant headers."""

    message_id: str = ""
    in_reply_to: str = ""
    from_address: str = ""
    to_addresses: str = ""
    cc_addresses: str = ""
    sent_at: datetime | None = None
    sender_domain: str = ""
    auto_submitted: str = ""
    list_id: str = ""
    precedence: str = ""
    has_calendar_part: bool = False
    #: SHA-256 of the normalized body text. Two copies of one message
    #: delivered to different mailboxes share it; two different messages that
    #: happen to carry the same Message-ID do not.
    body_sha256: str = ""

    def as_record_item_fields(self) -> dict[str, str | bool | datetime | None]:
        """The exact ``RecordItem`` column assignment for this message."""
        return {
            "email_message_id": self.message_id,
            "email_in_reply_to": self.in_reply_to,
            "email_from": self.from_address,
            "email_to": self.to_addresses,
            "email_cc": self.cc_addresses,
            "email_sent_at": self.sent_at,
            "email_sender_domain": self.sender_domain,
            "email_auto_submitted": self.auto_submitted,
            "email_list_id": self.list_id,
            "email_precedence": self.precedence,
            "email_has_calendar_part": self.has_calendar_part,
            "email_body_sha256": self.body_sha256,
        }


#: The column names :meth:`EmailMetadata.as_record_item_fields` writes, for
#: ``update_fields`` on a save that touches only the metadata. Derived from the
#: method itself so the two can never drift apart.
RECORD_ITEM_METADATA_FIELDS = tuple(EmailMetadata().as_record_item_fields())


def _clean(value: object, limit: int) -> str:
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


def _address_list(message: EmailMessage, header: str) -> tuple[str, list[str]]:
    """Normalized ``header`` addresses and their lowercased domains."""
    raw_values = [str(value) for value in message.get_all(header, [])]
    addresses = [
        address.strip()
        for _display, address in getaddresses(raw_values)
        if address and address.strip()
    ]
    # dict.fromkeys keeps header order while dropping the repeats a long
    # thread accumulates.
    addresses = list(dict.fromkeys(addresses))
    domains = [
        domain
        for domain in dict.fromkeys(
            address.rpartition("@")[2].lower() for address in addresses
        )
        if domain
    ]
    return _clean(", ".join(addresses), MAX_ADDRESS_LIST), domains


def single_address(value: object) -> str:
    """One lowercased mailbox address, or empty when the value is not one.

    Stricter than :func:`_address_list` on purpose. An address list is stored
    as text for a reviewer to read; this value names the mailbox a record will
    be attributed to, so anything that is not unambiguously one address --
    a control character, embedded whitespace, a second ``@``, a missing local
    part or domain -- yields nothing rather than a guess. Over-attributing a
    public record to a mailbox that never held it is the failure this whole
    path exists to avoid.
    """
    address = _CONTROL.sub(" ", str(value or "")).strip().lower()[:MAX_ACCOUNT]
    if not address or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in address
    ):
        return ""
    local, at_sign, domain = address.rpartition("@")
    if not at_sign or not local or not domain or "@" in local:
        return ""
    return address


def _delivered_to(message: EmailMessage) -> str:
    """The one mailbox this copy was delivered to, or empty.

    ``Delivered-To`` is written by the delivering server, once per delivery,
    naming the mailbox it delivered into -- which is precisely the custodial
    fact a per-mailbox copy carries and a Message-ID-keyed export metadata CSV
    cannot: the CSV describes every copy of a message at once, so for a
    personalized bulk mailing it names a hundred mailboxes for a hundred
    different records.

    Exactly one header, naming exactly one address, or nothing. A message that
    was forwarded through a chain carries several ``Delivered-To`` headers and
    the last hop is not reliably the mailbox that kept it; a message with none
    says nothing. Neither is guessed at -- the caller names and counts the
    message instead.
    """
    values = message.get_all("Delivered-To", [])
    if len(values) != 1:
        return ""
    addresses = [
        address
        for _display, address in getaddresses([str(values[0])])
        if address and address.strip()
    ]
    if len(addresses) != 1:
        return ""
    return single_address(addresses[0])


def parse_delivered_to(raw_message: bytes) -> str:
    """The one mailbox one raw message was delivered to, or empty.

    Headers only: the callers that need this -- Vault ingest before it knows
    whether a message will even be parsed, and the backfill reading a retained
    raw message purely to name its mailbox -- want one header, not the cost of
    decoding a multi-megabyte body they will not look at.
    """
    message = BytesParser(policy=policy.default).parsebytes(
        raw_message, headersonly=True,
    )
    return _delivered_to(message)


def _sent_at(message: EmailMessage) -> datetime | None:
    """The Date header as an aware datetime, or None when unusable.

    A malformed or missing Date is common in exported mail and is not an
    ingest failure: the record still exists and still has to be reviewed.
    """
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None or parsed.utcoffset() is None:
        # A naive value carries no zone, and guessing one would invent a fact
        # about when a public record was sent.
        return None
    return parsed


def _body_sha256(message: EmailMessage) -> str:
    """Fingerprint what the message actually says.

    Message-ID is a sender-supplied header, not an identity: a broken relay can
    reuse one on resend, and a mail-merge can personalize each copy's body
    while keeping it. Deduplicating on the header alone would fold a genuinely
    different record into an existing one and it would never reach a reviewer,
    which is how a responsive record goes missing from a production. Pairing it
    with the body's digest keeps the case it is meant for -- the same message
    found in several mailboxes -- and excludes the case it is not.

    Attachments are excluded on purpose: the text is what differs between two
    messages that share a Message-ID, and hashing multi-megabyte parts on every
    ingest would cost far more than it settles.

    A message with no text at all -- a bare calendar invitation, a message
    that is only an attachment -- returns the empty string rather than the
    digest of nothing. The digest of nothing is the same for every such
    message, so treating it as an identity would let two different
    attachment-only records that happen to share a Message-ID collapse into
    one, and the second would never reach a reviewer. An empty digest means
    "this message has no text fingerprint", and deduplication falls back to
    the byte and rendered-content hashes. Seeing one message twice is
    recoverable; never seeing it is not.
    """
    chunks = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True) or b""
        text = _decode(payload, part.get_content_charset())
        if content_type == "text/html":
            text = html.unescape(_HTML_TAG.sub(" ", text))
        chunks.append(text)
    normalized = _WHITESPACE.sub(" ", "\n".join(chunks)).strip()
    if not normalized:
        # No text to fingerprint -- an attachment-only message, or a bare
        # calendar invitation. Returning the digest of the empty string would
        # make every such message identical to every other, so one Message-ID
        # reused across two of them would fold the second into the first and
        # it would never reach a reviewer. An empty digest is the honest
        # answer: this message cannot corroborate its own Message-ID, so it is
        # not deduplicated on it.
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _decode(payload: bytes, charset: str | None) -> str:
    """Text from one part, whatever the message claims its charset is.

    ``errors="replace"`` only rescues a *known* codec. Exported mail routinely
    carries a garbled or invented charset label, and that raises ``LookupError``
    instead -- which, uncaught, fails the whole Vault export object. The object
    is then retried whole, hits the same message, and that custodian's
    collection never completes, on a statutory clock. A message that lies about
    its encoding is a message to read as best we can, not an outage.

    Two tiers, not three: UTF-8 with ``errors="replace"`` cannot raise, so it
    is the last one and anything after it would be unreachable. Bytes that are
    not UTF-8 come back as replacement characters rather than a lossless
    mapping, which is the honest result for a message whose declared encoding
    is unusable.
    """
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            pass
    return payload.decode("utf-8", errors="replace")


def _has_calendar_part(message: EmailMessage) -> bool:
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/calendar":
            return True
        filename = (part.get_filename() or "").lower()
        if filename.endswith(".ics"):
            return True
    return False


def parse_email_metadata(raw_message: bytes) -> EmailMetadata:
    """Read the reviewer-relevant headers out of one RFC 822 message."""
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    from_addresses, from_domains = _address_list(message, "From")
    to_addresses, _to_domains = _address_list(message, "To")
    cc_addresses, _cc_domains = _address_list(message, "Cc")
    list_id = _clean(message.get("List-Id", ""), MAX_HEADER)
    if not list_id:
        # A list that omits List-Id but offers an unsubscribe route is still a
        # list; recording which header answered keeps the fact auditable.
        list_id = _clean(message.get("List-Unsubscribe", ""), MAX_HEADER)
    return EmailMetadata(
        message_id=_clean(message.get("Message-ID", ""), MAX_MESSAGE_ID),
        in_reply_to=_clean(message.get("In-Reply-To", ""), MAX_MESSAGE_ID),
        from_address=from_addresses[:MAX_HEADER],
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        sent_at=_sent_at(message),
        sender_domain=(from_domains[0] if from_domains else "")[:MAX_DOMAIN],
        auto_submitted=_clean(message.get("Auto-Submitted", ""), 100),
        list_id=list_id,
        precedence=_clean(message.get("Precedence", ""), 100),
        has_calendar_part=_has_calendar_part(message),
        body_sha256=_body_sha256(message),
    )
