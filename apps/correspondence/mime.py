"""Bounded, side-effect-free parsing of hostile inbound MIME messages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import quopri
import re
import struct
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from email import policy
from email.errors import MessageDefect
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from io import BytesIO
from types import MappingProxyType

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
_ENHANCED_STATUS = re.compile(r"([245])\.[0-9]{1,3}\.[0-9]{1,3}\Z")
_DSN_ADDRESS_TYPE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_HEADER_NAME = re.compile(br"[!-9;-~]+\Z")
_RAW_HEADER_LINE_BREAK = re.compile(br"\r\n|\r|\n")
_TEXT_HEADER_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_MIME_TOKEN = re.compile(br"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_BOUNDARY_CHARACTERS = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'()+_,-./:=? "
)
_OLE_COMPOUND_FILE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_CONTENT_TYPES = {
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
}
_OOXML_MAIN_PARTS = {
    (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml"
    ): (
        (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "ppt/presentation.xml",
    ),
    (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml"
    ): (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xl/workbook.xml",
    ),
    (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ): (
        (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "word/document.xml",
    ),
}
_OOXML_CONTENT_TYPES = {
    package_type
    for package_type, _main_part in _OOXML_MAIN_PARTS.values()
}
_OOXML_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_MAX_OOXML_ENTRIES = 10_000
_MAX_OOXML_CONTENT_TYPES_BYTES = 1024 * 1024
_MAX_OOXML_COMPRESSION_RATIO = 100
_SUPPORTED_CONTENT_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/rtf",
    *_OLE_CONTENT_TYPES,
    *_OOXML_CONTENT_TYPES,
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "message/rfc822",
    "text/csv",
    "text/plain",
}
_PERSISTED_HEADER_ALLOWLIST = (
    "From",
    "To",
    "Cc",
    "Reply-To",
    "Date",
    "Subject",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Auto-Submitted",
    "Precedence",
    "List-Id",
)
_SINGLETON_HEADERS = {
    "auto-submitted",
    "content-disposition",
    "content-transfer-encoding",
    "content-type",
    "date",
    "from",
    "in-reply-to",
    "message-id",
    "mime-version",
    "references",
    "reply-to",
    "subject",
}
_DSN_STATUS_CLASS_BY_ACTION = {
    "failed": "5",
    "delayed": "4",
    "delivered": "2",
    "relayed": "2",
    "expanded": "2",
}
_ERROR_MESSAGES = {
    "message_too_large": "The inbound message exceeds its size limit.",
    "malformed_message": "The inbound message is malformed.",
    "too_many_headers": "The inbound message has too many headers.",
    "header_line_too_long": "An inbound message header is too long.",
    "too_many_mime_parts": "The inbound message has too many MIME parts.",
    "mime_depth_exceeded": "The inbound message is nested too deeply.",
    "body_too_large": "The inbound message body exceeds its size limit.",
    "too_many_attachments": "The inbound message has too many attachments.",
    "attachment_too_large": "An inbound attachment exceeds its size limit.",
    "attachment_total_too_large": (
        "The inbound attachments exceed their combined size limit."
    ),
    "attachment_content_type_mismatch": (
        "An inbound attachment does not match its declared file type."
    ),
}


@dataclass(frozen=True, slots=True)
class InboundMimeLimits:
    max_raw_bytes: int
    max_header_count: int
    max_header_line_bytes: int
    max_parts: int
    max_depth: int
    max_body_bytes: int
    max_attachment_count: int
    max_attachment_bytes: int
    max_total_attachment_bytes: int

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_raw_bytes,
                self.max_header_count,
                self.max_header_line_bytes,
                self.max_parts,
                self.max_depth,
                self.max_body_bytes,
                self.max_attachment_count,
                self.max_attachment_bytes,
                self.max_total_attachment_bytes,
            )
        ):
            raise ValueError("Inbound MIME limits must all be positive.")


DEFAULT_INBOUND_MIME_LIMITS = InboundMimeLimits(
    max_raw_bytes=75 * 1024 * 1024,
    max_header_count=100,
    max_header_line_bytes=998,
    max_parts=100,
    max_depth=10,
    max_body_bytes=1024 * 1024,
    max_attachment_count=10,
    max_attachment_bytes=25 * 1024 * 1024,
    max_total_attachment_bytes=50 * 1024 * 1024,
)


class InboundMimeSecurityError(ValueError):
    """A safe, machine-readable reason to quarantine inbound evidence."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES.get(code, "The inbound message is unsafe."))


@dataclass(frozen=True, slots=True)
class ParsedInboundAttachment:
    filename: str
    content_type: str
    size: int
    sha256: str
    content: bytes

    @property
    def payload(self) -> bytes:
        """Compatibility alias for persistence callers."""
        return self.content


@dataclass(frozen=True, slots=True)
class ParsedDeliveryStatusRecipient:
    recipient_index: int
    action: str
    status: str
    _recipient_fingerprint: str = field(
        default="",
        repr=False,
        compare=False,
    )

    def matches_recipient_email(self, email: str) -> bool:
        return bool(self._recipient_fingerprint) and hmac.compare_digest(
            self._recipient_fingerprint,
            _dsn_recipient_fingerprint(email),
        )


@dataclass(frozen=True, slots=True)
class ParsedDeliveryStatusReport:
    original_message_id: str
    recipients: tuple[ParsedDeliveryStatusRecipient, ...]


@dataclass(frozen=True, slots=True)
class ParsedInboundMessage:
    subject: str
    body: str
    from_address: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    rfc_message_id: str
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    headers: Mapping[str, str]
    attachments: tuple[ParsedInboundAttachment, ...]
    delivery_status_report: ParsedDeliveryStatusReport | None


@dataclass(frozen=True, slots=True)
class _AttachedMessageSpan:
    body_start: int
    body_end: int
    transfer_encoding: bytes


def _security_error(code: str) -> InboundMimeSecurityError:
    return InboundMimeSecurityError(code)


def _split_raw_header_lines(value: bytes) -> list[bytes]:
    """Split only on line endings accepted by the RFC 5322 parser."""
    if not value:
        return []
    lines = _RAW_HEADER_LINE_BREAK.split(value)
    if value.endswith((b"\r", b"\n")):
        lines.pop()
    return lines


def _split_parsed_header_lines(value: str) -> list[str]:
    """Keep other Unicode line separators inside their physical header line."""
    if not value:
        return []
    lines = _TEXT_HEADER_LINE_BREAK.split(value)
    if value.endswith(("\r", "\n")):
        lines.pop()
    return lines


def _validate_top_level_header_block(
    raw_message: bytes,
    limits: InboundMimeLimits,
) -> None:
    separators = [
        position
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (position := raw_message.find(separator)) >= 0
    ]
    if not separators:
        raise _security_error("malformed_message")

    block = raw_message[: min(separators)]
    header_count = 0
    has_header = False
    for line in _split_raw_header_lines(block):
        if len(line) > limits.max_header_line_bytes:
            raise _security_error("header_line_too_long")
        if line.startswith((b" ", b"\t")):
            if not has_header:
                raise _security_error("malformed_message")
            continue
        name, delimiter, _value = line.partition(b":")
        if not delimiter or not _HEADER_NAME.fullmatch(name):
            raise _security_error("malformed_message")
        header_count += 1
        has_header = True
        if header_count > limits.max_header_count:
            raise _security_error("too_many_headers")


def _raw_header_and_body_span(
    raw_message: bytes,
    *,
    start: int,
    end: int,
) -> tuple[int, int, int]:
    for separator in (b"\r\n", b"\n", b"\r"):
        if raw_message.startswith(separator, start, end):
            return start, start, start + len(separator)
    separators = [
        (position, len(separator))
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (
            position := raw_message.find(
                separator,
                start,
                end,
            )
        )
        >= 0
    ]
    if not separators:
        raise _security_error("malformed_message")
    position, separator_length = min(separators)
    return start, position, position + separator_length


def _preflight_raw_headers(
    block: bytes,
    limits: InboundMimeLimits,
    state: list[int],
) -> dict[bytes, list[bytes]]:
    # Header folding must not turn one nominal field into an almost raw-size
    # allocation. Derive a whole-block ceiling from the public count and
    # physical-line limits rather than introducing an independent contract.
    if len(block) > limits.max_header_count * (
        limits.max_header_line_bytes + 2
    ):
        raise _security_error("too_many_headers")
    headers: dict[bytes, list[bytes]] = {}
    current_name: bytes | None = None
    for line in _split_raw_header_lines(block):
        if len(line) > limits.max_header_line_bytes:
            raise _security_error("header_line_too_long")
        if line.startswith((b" ", b"\t")):
            if current_name is None:
                raise _security_error("malformed_message")
            headers[current_name][-1] += b" " + line.strip()
            continue

        name, delimiter, value = line.partition(b":")
        if not delimiter or not _HEADER_NAME.fullmatch(name):
            raise _security_error("malformed_message")
        current_name = name.lower()
        headers.setdefault(current_name, []).append(value.strip())
        state[1] += 1
        if state[1] > limits.max_header_count:
            raise _security_error("too_many_headers")
    return headers


def _preflight_boundary(content_type: bytes) -> bytes | None:
    media_type, separator, parameters = content_type.partition(b";")
    if media_type.strip().lower().split(b"/", 1)[0] != b"multipart":
        return None

    # The stdlib accepts comments, RFC2231 continuations, and conflicting
    # duplicate parameters. A lightweight regex can therefore preflight one
    # boundary while the full parser chooses another and materializes an
    # unbounded tree. Restrict multipart Content-Type to one unambiguous,
    # ordinary boundary parameter before invoking BytesParser.
    if not separator:
        raise _security_error("malformed_message")
    cursor = 0
    boundary: bytes | None = None
    while cursor < len(parameters):
        while cursor < len(parameters) and parameters[cursor] in b" \t":
            cursor += 1
        name_start = cursor
        while (
            cursor < len(parameters)
            and parameters[cursor] not in b"=; \t"
        ):
            cursor += 1
        name = parameters[name_start:cursor]
        if not name or _MIME_TOKEN.fullmatch(name) is None:
            raise _security_error("malformed_message")
        while cursor < len(parameters) and parameters[cursor] in b" \t":
            cursor += 1
        if cursor >= len(parameters) or parameters[cursor] != ord("="):
            raise _security_error("malformed_message")
        cursor += 1
        while cursor < len(parameters) and parameters[cursor] in b" \t":
            cursor += 1

        if cursor < len(parameters) and parameters[cursor] == ord('"'):
            cursor += 1
            value = bytearray()
            while cursor < len(parameters):
                byte = parameters[cursor]
                cursor += 1
                if byte == ord('"'):
                    break
                if byte == ord("\\"):
                    if cursor >= len(parameters):
                        raise _security_error("malformed_message")
                    byte = parameters[cursor]
                    cursor += 1
                if byte in b"\r\n\x00":
                    raise _security_error("malformed_message")
                value.append(byte)
            else:
                raise _security_error("malformed_message")
            parameter_value = bytes(value)
        else:
            value_start = cursor
            while cursor < len(parameters) and parameters[cursor] != ord(";"):
                cursor += 1
            parameter_value = parameters[value_start:cursor].strip()
            if (
                not parameter_value
                or _MIME_TOKEN.fullmatch(parameter_value) is None
            ):
                raise _security_error("malformed_message")

        while cursor < len(parameters) and parameters[cursor] in b" \t":
            cursor += 1
        if cursor < len(parameters):
            if parameters[cursor] != ord(";"):
                raise _security_error("malformed_message")
            cursor += 1
            if cursor == len(parameters):
                raise _security_error("malformed_message")

        normalized_name = name.lower()
        if normalized_name.startswith(b"boundary") and (
            normalized_name != b"boundary"
        ):
            raise _security_error("malformed_message")
        if normalized_name == b"boundary":
            if boundary is not None:
                raise _security_error("malformed_message")
            boundary = parameter_value

    if (
        boundary is None
        or not 1 <= len(boundary) <= 70
        or boundary[-1] == ord(" ")
        or any(byte not in _BOUNDARY_CHARACTERS for byte in boundary)
    ):
        raise _security_error("malformed_message")
    return boundary


def _preflight_multipart_children(
    raw_message: bytes,
    boundary: bytes,
    *,
    start: int,
    end: int,
):
    # Search the original immutable input with bounded positions. Yielding
    # slices here retained a raw-sized copy at every nesting level.
    delimiter = re.compile(
        br"--"
        + re.escape(boundary)
        + br"(--)?[ \t]*(?:\r\n|\n|\r|$)",
        flags=re.MULTILINE,
    )
    child_start: int | None = None
    for match in delimiter.finditer(raw_message, start, end):
        if (
            match.start() != start
            and raw_message[match.start() - 1] not in (10, 13)
        ):
            continue
        if child_start is not None:
            child_end = match.start()
            if raw_message[child_end - 2 : child_end] == b"\r\n":
                child_end -= 2
            elif raw_message[child_end - 1 : child_end] in {b"\r", b"\n"}:
                child_end -= 1
            else:
                raise _security_error("malformed_message")
            yield child_start, child_end
        if match.group(1):
            return
        child_start = match.end()
    if child_start is not None:
        # The full parser will report the absent closing delimiter. Inspecting
        # the trailing candidate here still bounds obviously hostile input.
        yield child_start, end


def _preflight_delivery_status_block(
    raw_message: bytes,
    *,
    start: int,
    end: int,
    depth: int,
    limits: InboundMimeLimits,
    state: list[int],
) -> None:
    """Count one RFC 3464 field block before stdlib object materialization."""
    state[0] += 1
    if state[0] > limits.max_parts:
        raise _security_error("too_many_mime_parts")
    if depth > limits.max_depth:
        raise _security_error("mime_depth_exceeded")

    # BytesParser represents each blank-line-delimited delivery-status block
    # as a separate Message. Bound a block before taking the small validation
    # slice so one hostile field block cannot duplicate the raw message in
    # memory during preflight.
    if end - start > limits.max_header_count * (
        limits.max_header_line_bytes + 2
    ):
        raise _security_error("too_many_headers")
    headers = _preflight_raw_headers(
        raw_message[start:end],
        limits,
        state,
    )
    if any(
        len(headers.get(name.encode("ascii"), ())) > 1
        for name in _SINGLETON_HEADERS
    ):
        raise _security_error("malformed_message")


def _preflight_delivery_status_blocks(
    raw_message: bytes,
    *,
    start: int,
    end: int,
    depth: int,
    limits: InboundMimeLimits,
    state: list[int],
) -> None:
    """Traverse stdlib delivery-status pseudo-parts without allocating them."""
    block_start = start
    line_start = start
    block_count = 0
    for line_break in _RAW_HEADER_LINE_BREAK.finditer(
        raw_message,
        start,
        end,
    ):
        if line_break.start() - line_start > limits.max_header_line_bytes:
            raise _security_error("header_line_too_long")
        if line_break.start() == line_start:
            _preflight_delivery_status_block(
                raw_message,
                start=block_start,
                end=line_start,
                depth=depth,
                limits=limits,
                state=state,
            )
            block_count += 1
            block_start = line_break.end()
        line_start = line_break.end()

    if end - line_start > limits.max_header_line_bytes:
        raise _security_error("header_line_too_long")
    # The email parser creates one empty child for an empty delivery-status
    # body, but does not create an extra child after a terminating blank line.
    if block_start < end or block_count == 0:
        _preflight_delivery_status_block(
            raw_message,
            start=block_start,
            end=end,
            depth=depth,
            limits=limits,
            state=state,
        )


def _raw_span_starts_with_header_field(
    raw_message: bytes,
    *,
    start: int,
    end: int,
    limits: InboundMimeLimits,
) -> bool:
    """Return whether FeedParser can treat the span as a nested message."""
    line_break = _RAW_HEADER_LINE_BREAK.search(raw_message, start, end)
    line_end = line_break.start() if line_break is not None else end
    delimiter = raw_message.find(b":", start, line_end)
    if delimiter <= start:
        return False
    if line_end - start > limits.max_header_line_bytes:
        raise _security_error("header_line_too_long")
    return _HEADER_NAME.fullmatch(
        raw_message[start:delimiter]
    ) is not None


def _raw_span_starts_with_noncanonical_feedparser_header(
    raw_message: bytes,
    *,
    start: int,
    end: int,
    limits: InboundMimeLimits,
) -> bool:
    """Return whether FeedParser accepts a non-RFC first header line."""
    line_break = _RAW_HEADER_LINE_BREAK.search(raw_message, start, end)
    line_end = line_break.start() if line_break is not None else end
    if line_end - start > limits.max_header_line_bytes:
        raise _security_error("header_line_too_long")
    first_line = raw_message[start:line_end]
    # email.feedparser.headerRE additionally accepts a Unix envelope line,
    # an orphan continuation, and an empty header name. A later ordinary
    # header is irrelevant when the first line matches none of its branches.
    return first_line.startswith(
        (b"From ", b" ", b"\t", b":")
    )


def _preflight_raw_part(
    raw_message: bytes,
    *,
    start: int,
    end: int,
    depth: int,
    limits: InboundMimeLimits,
    state: list[int],
    default_media_type: bytes,
    is_root: bool,
    collect_attached_messages: bool,
    attached_message_spans: list[_AttachedMessageSpan],
) -> None:
    state[0] += 1
    if state[0] > limits.max_parts:
        raise _security_error("too_many_mime_parts")
    if depth > limits.max_depth:
        raise _security_error("mime_depth_exceeded")

    header_start, header_end, body_start = _raw_header_and_body_span(
        raw_message,
        start=start,
        end=end,
    )
    header_block = raw_message[header_start:header_end]
    headers = _preflight_raw_headers(header_block, limits, state)
    if any(
        len(headers.get(name.encode("ascii"), ())) > 1
        for name in _SINGLETON_HEADERS
    ):
        raise _security_error("malformed_message")
    content_type = headers.get(b"content-type", [default_media_type])[0]
    media_type = content_type.partition(b";")[0].strip().lower()
    if media_type.startswith(b"message/") and media_type not in {
        b"message/rfc822",
        b"message/delivery-status",
    }:
        # FeedParser recursively materializes every message/* subtype, even
        # unknown or externally-referencing types. This application supports
        # only attached RFC822 evidence and RFC 3464 delivery-status blocks;
        # reject the rest before the stdlib can build an unbounded child tree.
        raise _security_error("malformed_message")
    boundary = _preflight_boundary(content_type)
    if boundary is not None:
        child_default_media_type = (
            b"message/rfc822"
            if media_type == b"multipart/digest"
            else b"text/plain"
        )
        for child_start, child_end in _preflight_multipart_children(
            raw_message,
            boundary,
            start=body_start,
            end=end,
        ):
            _preflight_raw_part(
                raw_message,
                start=child_start,
                end=child_end,
                depth=depth + 1,
                limits=limits,
                state=state,
                default_media_type=child_default_media_type,
                is_root=False,
                collect_attached_messages=collect_attached_messages,
                attached_message_spans=attached_message_spans,
            )
        return

    if media_type == b"message/rfc822":
        transfer_encoding = headers.get(
            b"content-transfer-encoding",
            [b""],
        )[0].strip().lower()
        if transfer_encoding not in {
            b"",
            b"7bit",
            b"8bit",
            b"binary",
            b"base64",
            b"quoted-printable",
        }:
            raise _security_error("malformed_message")
        if is_root and transfer_encoding in {b"base64", b"quoted-printable"}:
            # The stdlib exposes encoded root wrappers as pseudo-children
            # rather than a byte-exact decoded message tree. They are not
            # attachment spans and cannot safely supply canonical body text.
            raise _security_error("malformed_message")
        is_extracted_attachment = not is_root
        if collect_attached_messages and is_extracted_attachment:
            attached_message_spans.append(
                _AttachedMessageSpan(
                    body_start=body_start,
                    body_end=end,
                    transfer_encoding=transfer_encoding,
                )
            )
        encoded_raw_header = (
            transfer_encoding in {b"base64", b"quoted-printable"}
            and _raw_span_starts_with_header_field(
                raw_message,
                start=body_start,
                end=end,
                limits=limits,
            )
        )
        if (
            transfer_encoding in {b"base64", b"quoted-printable"}
            and not encoded_raw_header
            and _raw_span_starts_with_noncanonical_feedparser_header(
                raw_message,
                start=body_start,
                end=end,
                limits=limits,
            )
        ):
            # FeedParser accepts these non-RFC first-line alternatives and can
            # then promote a later Content-Type into an unbounded child tree.
            # Such input is not a canonical transfer encoding and must fail
            # before parsing.
            raise _security_error("malformed_message")
        raw_nested_message = transfer_encoding in {
            b"",
            b"7bit",
            b"8bit",
            b"binary",
        } or encoded_raw_header
        if raw_nested_message:
            # FeedParser ignores a message/rfc822 transfer-encoding label when
            # the raw body itself starts like an RFC822 message. Preflight that
            # exact representation with the shared counters before the stdlib
            # can materialize a disguised multipart tree. Strict transfer
            # decoding remains the authority for the retained attachment.
            _preflight_raw_part(
                raw_message,
                start=body_start,
                end=end,
                depth=depth + 1,
                limits=limits,
                state=state,
                default_media_type=b"text/plain",
                is_root=False,
                collect_attached_messages=(
                    collect_attached_messages
                    and not is_extracted_attachment
                ),
                attached_message_spans=attached_message_spans,
            )
        return

    if media_type == b"message/delivery-status":
        _preflight_delivery_status_blocks(
            raw_message,
            start=body_start,
            end=end,
            depth=depth + 1,
            limits=limits,
            state=state,
        )


def _preflight_raw_structure(
    raw_message: bytes,
    limits: InboundMimeLimits,
) -> tuple[_AttachedMessageSpan, ...]:
    # This deliberately handles only structure visible without transfer
    # decoding. The full standards parser remains authoritative, but obvious
    # header/part/depth bombs are rejected before it builds an object graph.
    attached_message_spans: list[_AttachedMessageSpan] = []
    _preflight_raw_part(
        raw_message,
        start=0,
        end=len(raw_message),
        depth=0,
        limits=limits,
        state=[0, 0],
        default_media_type=b"text/plain",
        is_root=True,
        collect_attached_messages=True,
        attached_message_spans=attached_message_spans,
    )
    return tuple(attached_message_spans)


def _validate_part_headers(
    part: Message,
    limits: InboundMimeLimits,
) -> int:
    raw_items = list(part.raw_items())
    if len(raw_items) > limits.max_header_count:
        raise _security_error("too_many_headers")
    normalized_names = [name.casefold() for name, _value in raw_items]
    if any(
        normalized_names.count(name) > 1
        for name in _SINGLETON_HEADERS
    ):
        raise _security_error("malformed_message")
    for name, value in raw_items:
        physical_lines = _split_parsed_header_lines(str(value)) or [""]
        for index, line in enumerate(physical_lines):
            rendered = f"{name}: {line}" if index == 0 else line
            if len(rendered.encode("utf-8", errors="surrogateescape")) > (
                limits.max_header_line_bytes
            ):
                raise _security_error("header_line_too_long")
    try:
        for header in part.values():
            if getattr(header, "defects", ()):
                raise _security_error("malformed_message")
    except (AttributeError, IndexError, RecursionError) as exc:
        # Python's structured-header parser can surface implementation
        # exceptions for bounded but adversarial address/MIME parameter trees.
        # Those inputs are malformed evidence, not transient parser failures.
        raise _security_error("malformed_message") from exc
    return len(raw_items)


def _validate_message_structure(
    message: Message,
    limits: InboundMimeLimits,
) -> tuple[int, int, int]:
    part_count = 0
    header_count = 0
    maximum_depth = 0
    stack: list[tuple[Message, int, bool]] = [(message, 0, False)]
    while stack:
        part, depth, ignore_validation = stack.pop()
        part_count += 1
        maximum_depth = max(maximum_depth, depth)
        if part_count > limits.max_parts:
            raise _security_error("too_many_mime_parts")
        if depth > limits.max_depth:
            raise _security_error("mime_depth_exceeded")

        if not ignore_validation:
            if part.defects:
                raise _security_error("malformed_message")
            header_count += _validate_part_headers(part, limits)
            if header_count > limits.max_header_count:
                raise _security_error("too_many_headers")

        children = part.get_payload()
        if not isinstance(children, list):
            if part.is_multipart():
                raise _security_error("malformed_message")
            continue
        encoded_attached_message = (
            part.get_content_type() == "message/rfc822"
            and str(
                part.get("Content-Transfer-Encoding", "")
            ).strip().lower()
            in {"base64", "quoted-printable"}
        )
        stack.extend(
            (
                child,
                depth + 1,
                ignore_validation or encoded_attached_message,
            )
            for child in reversed(children)
        )
    return part_count, header_count, maximum_depth


def _enforce_decoded_limits(
    size: int,
    decoded_limits: tuple[tuple[int, str], ...],
) -> None:
    for maximum, error_code in decoded_limits:
        if size > maximum:
            raise _security_error(error_code)


def _strict_payload_bytes(
    part: Message,
    *,
    decoded_limits: tuple[tuple[int, str], ...] = (),
) -> bytes:
    transfer_encoding = (
        str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    )
    if transfer_encoding not in {
        "",
        "7bit",
        "8bit",
        "binary",
        "base64",
        "quoted-printable",
    }:
        raise _security_error("malformed_message")

    if transfer_encoding == "base64":
        encoded = part.get_payload(decode=False)
        decoded = _strict_base64_bytes(
            encoded,
            decoded_limits=decoded_limits,
        )
    elif transfer_encoding == "quoted-printable":
        encoded = part.get_payload(decode=False)
        decoded = _strict_quoted_printable_bytes(
            encoded,
            decoded_limits=decoded_limits,
        )
    else:
        decoded = part.get_payload(decode=True)
        if decoded is None:
            raw_payload = part.get_payload(decode=False)
            if not isinstance(raw_payload, str):
                raise _security_error("malformed_message")
            try:
                decoded = raw_payload.encode("utf-8", errors="surrogateescape")
            except UnicodeEncodeError as exc:
                raise _security_error("malformed_message") from exc
        if transfer_encoding in {"", "7bit"} and any(
            byte > 127 for byte in decoded
        ):
            raise _security_error("malformed_message")

    if part.defects:
        raise _security_error("malformed_message")
    _enforce_decoded_limits(len(decoded), decoded_limits)
    return decoded


def _strict_base64_bytes(
    encoded,
    *,
    decoded_limits: tuple[tuple[int, str], ...] = (),
) -> bytes:
    if not isinstance(encoded, (str, bytes)):
        raise _security_error("malformed_message")
    if isinstance(encoded, str):
        try:
            encoded = encoded.encode("ascii")
        except UnicodeEncodeError as exc:
            raise _security_error("malformed_message") from exc
    encoded_length = 0
    padding = 0
    saw_padding = False
    for byte in encoded:
        if byte in b"\r\n\t ":
            continue
        if (
            not 65 <= byte <= 90
            and not 97 <= byte <= 122
            and not 48 <= byte <= 57
            and byte not in b"+/="
        ):
            raise _security_error("malformed_message")
        encoded_length += 1
        if byte == 61:
            saw_padding = True
            padding += 1
        elif saw_padding:
            raise _security_error("malformed_message")
    if encoded_length % 4 or padding > 2:
        raise _security_error("malformed_message")
    decoded_size = (encoded_length // 4) * 3 - padding
    _enforce_decoded_limits(decoded_size, decoded_limits)

    encoded = re.sub(br"[\r\n\t ]", b"", encoded)
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _security_error("malformed_message") from exc


def _strict_quoted_printable_bytes(
    encoded,
    *,
    decoded_limits: tuple[tuple[int, str], ...] = (),
) -> bytes:
    if not isinstance(encoded, (str, bytes)):
        raise _security_error("malformed_message")
    if isinstance(encoded, str):
        try:
            encoded = encoded.encode("ascii")
        except UnicodeEncodeError as exc:
            raise _security_error("malformed_message") from exc

    index = 0
    decoded_size = 0
    hexadecimal = b"0123456789abcdefABCDEF"
    while index < len(encoded):
        if encoded[index] != 61:
            decoded_size += 1
            index += 1
            continue
        if encoded[index + 1 : index + 3] == b"\r\n":
            index += 3
            continue
        if encoded[index + 1 : index + 2] == b"\n":
            index += 2
            continue
        if (
            index + 2 < len(encoded)
            and encoded[index + 1] in hexadecimal
            and encoded[index + 2] in hexadecimal
        ):
            decoded_size += 1
            index += 3
            continue
        raise _security_error("malformed_message")
    _enforce_decoded_limits(decoded_size, decoded_limits)
    return quopri.decodestring(encoded)


def _attached_message_bytes(
    part: Message,
    raw_message: bytes,
    span: _AttachedMessageSpan,
    *,
    decoded_limits: tuple[tuple[int, str], ...] = (),
) -> bytes:
    try:
        parsed_transfer_encoding = (
            str(part.get("Content-Transfer-Encoding", ""))
            .strip()
            .lower()
            .encode("ascii")
        )
    except UnicodeEncodeError as exc:
        raise _security_error("malformed_message") from exc
    if parsed_transfer_encoding != span.transfer_encoding:
        raise _security_error("malformed_message")
    if not (
        0 <= span.body_start <= span.body_end <= len(raw_message)
    ):
        raise _security_error("malformed_message")

    encoded_size = span.body_end - span.body_start
    if span.transfer_encoding in {b"", b"7bit", b"8bit", b"binary"}:
        _enforce_decoded_limits(encoded_size, decoded_limits)
    encoded = raw_message[span.body_start : span.body_end]

    if span.transfer_encoding == b"base64":
        return _strict_base64_bytes(
            encoded,
            decoded_limits=decoded_limits,
        )
    if span.transfer_encoding == b"quoted-printable":
        return _strict_quoted_printable_bytes(
            encoded,
            decoded_limits=decoded_limits,
        )
    if span.transfer_encoding in {b"", b"7bit"} and any(
        byte > 127 for byte in encoded
    ):
        raise _security_error("malformed_message")
    return encoded


def _is_attachment(part: Message, *, is_root: bool) -> bool:
    if part.get_content_disposition() == "attachment":
        return True
    if part.get_filename() is not None:
        return True
    if not is_root and part.get_content_type() == "message/rfc822":
        return True
    return not part.is_multipart() and part.get_content_maintype() != "text"


def _sanitize_filename(value: str | None) -> str:
    name = unicodedata.normalize("NFKC", value or "")
    name = os.path.basename(name.replace("\\", "/"))
    name = "".join(
        character
        for character in name
        if not unicodedata.category(character).startswith("C")
    )
    name = name[:255]
    return "attachment" if name in {"", ".", ".."} else name


def _looks_like_text(payload: bytes) -> bool:
    prefix = payload[:8192]
    if b"\x00" in prefix:
        return False
    if any(byte < 32 and byte not in {9, 10, 13} for byte in prefix):
        return False
    try:
        prefix.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            prefix.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def _looks_like_email(payload: bytes) -> bool:
    if not _looks_like_text(payload):
        return False
    header_block, separator, _body = payload.replace(
        b"\r\n", b"\n"
    ).partition(b"\n\n")
    if not separator:
        return False
    return any(
        b":" in line
        for line in _split_raw_header_lines(header_block)
        if not line.startswith((b" ", b"\t"))
    )


def _zip_eocd_metadata(
    payload: bytes,
) -> tuple[int, int, int] | None:
    # The classic EOCD must occur within the final 65,557 bytes (the fixed
    # record plus its maximum comment). Validate it before ZipFile reads and
    # materializes the central directory. ZIP64 is outside this narrow,
    # bounded OOXML acceptance profile.
    search_start = max(0, len(payload) - (65_535 + 22))
    position = payload.rfind(b"PK\x05\x06", search_start)
    while position >= 0:
        if position + 22 <= len(payload):
            (
                _signature,
                disk_number,
                central_directory_disk,
                entries_on_disk,
                total_entries,
                central_directory_size,
                central_directory_offset,
                comment_length,
            ) = struct.unpack_from("<4s4H2LH", payload, position)
            if (
                position + 22 + comment_length == len(payload)
                and disk_number == 0
                and central_directory_disk == 0
                and entries_on_disk == total_entries
                and total_entries != 0xFFFF
                and central_directory_offset + central_directory_size
                == position
            ):
                return (
                    total_entries,
                    central_directory_offset,
                    central_directory_size,
                )
        position = payload.rfind(
            b"PK\x05\x06",
            search_start,
            position,
        )
    return None


def _central_directory_matches_eocd(
    payload: bytes,
    *,
    entry_count: int,
    offset: int,
    size: int,
) -> bool:
    cursor = offset
    directory_end = offset + size
    for _index in range(entry_count):
        if (
            cursor + 46 > directory_end
            or payload[cursor : cursor + 4] != b"PK\x01\x02"
        ):
            return False
        filename_length, extra_length, comment_length = struct.unpack_from(
            "<3H",
            payload,
            cursor + 28,
        )
        if struct.unpack_from("<H", payload, cursor + 34)[0] != 0:
            return False
        cursor += (
            46
            + filename_length
            + extra_length
            + comment_length
        )
        if cursor > directory_end:
            return False
    return cursor == directory_end


def _ooxml_content_type(
    payload: bytes,
    *,
    max_entry_expanded_bytes: int = 25 * 1024 * 1024,
    max_total_expanded_bytes: int = 50 * 1024 * 1024,
) -> str | None:
    metadata = _zip_eocd_metadata(payload)
    if metadata is None:
        return None
    entry_count, directory_offset, directory_size = metadata
    if (
        entry_count > _MAX_OOXML_ENTRIES
        or not _central_directory_matches_eocd(
            payload,
            entry_count=entry_count,
            offset=directory_offset,
            size=directory_size,
        )
    ):
        return None
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) != entry_count:
                return None
            names = {entry.filename.lstrip("/") for entry in entries}
            if len(names) != len(entries):
                return None
            expanded_total = 0
            for entry in entries:
                if entry.flag_bits & 0x1:
                    return None
                if entry.is_dir():
                    if entry.file_size or entry.compress_size:
                        return None
                    continue
                if entry.file_size > max_entry_expanded_bytes:
                    return None
                expanded_total += entry.file_size
                if expanded_total > max_total_expanded_bytes:
                    return None
                if (
                    entry.file_size
                    and (
                        entry.compress_size == 0
                        or entry.file_size
                        > entry.compress_size
                        * _MAX_OOXML_COMPRESSION_RATIO
                    )
                ):
                    return None
            try:
                manifest_info = archive.getinfo("[Content_Types].xml")
            except KeyError:
                return None
            if (
                manifest_info.is_dir()
                or manifest_info.flag_bits & 0x1
                or manifest_info.file_size
                > _MAX_OOXML_CONTENT_TYPES_BYTES
            ):
                return None
            with archive.open(manifest_info) as manifest_file:
                manifest = manifest_file.read(
                    _MAX_OOXML_CONTENT_TYPES_BYTES + 1
                )
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return None

    if (
        len(manifest) > _MAX_OOXML_CONTENT_TYPES_BYTES
        or b"<!doctype" in manifest.lower()
        or b"<!entity" in manifest.lower()
    ):
        return None
    try:
        root = ElementTree.fromstring(
            manifest,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (
        DefusedXmlException,
        ElementTree.ParseError,
        LookupError,
        RecursionError,
        ValueError,
    ):
        return None
    if root.tag != f"{{{_OOXML_CONTENT_TYPES_NAMESPACE}}}Types":
        return None

    detected: set[str] = set()
    for element in root.iter():
        if (
            element.tag
            != f"{{{_OOXML_CONTENT_TYPES_NAMESPACE}}}Override"
        ):
            continue
        main_part = _OOXML_MAIN_PARTS.get(element.get("ContentType", ""))
        if main_part is None:
            continue
        package_type, required_name = main_part
        declared_name = element.get("PartName", "").lstrip("/")
        if declared_name == required_name and required_name in names:
            detected.add(package_type)
    if len(detected) != 1:
        return None
    return detected.pop()


def _sniff_content_type(
    payload: bytes,
    declared: str,
    *,
    max_entry_expanded_bytes: int = 25 * 1024 * 1024,
    max_total_expanded_bytes: int = 50 * 1024 * 1024,
) -> str | None:
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if payload.startswith(b"{\\rtf"):
        return "application/rtf"
    if payload.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
        return "application/octet-stream"
    if payload.startswith(b"PK\x03\x04"):
        return _ooxml_content_type(
            payload,
            max_entry_expanded_bytes=max_entry_expanded_bytes,
            max_total_expanded_bytes=max_total_expanded_bytes,
        )
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if _looks_like_email(payload) and declared == "message/rfc822":
        return "message/rfc822"
    if _looks_like_text(payload):
        return "text/plain"
    return None


def _declared_type_is_compatible(declared: str, sniffed: str) -> bool:
    if declared == "application/octet-stream" or declared == sniffed:
        return True
    if sniffed == "application/octet-stream":
        return declared in _OLE_CONTENT_TYPES
    if sniffed == "text/plain":
        return declared in {"text/csv", "text/plain"}
    return False


def _attachment_from_part(
    part: Message,
    payload: bytes,
    *,
    attachment_count: int,
    attachment_total: int,
    limits: InboundMimeLimits,
) -> ParsedInboundAttachment:
    if attachment_count > limits.max_attachment_count:
        raise _security_error("too_many_attachments")
    if len(payload) > limits.max_attachment_bytes:
        raise _security_error("attachment_too_large")
    if attachment_total + len(payload) > limits.max_total_attachment_bytes:
        raise _security_error("attachment_total_too_large")

    declared = part.get_content_type().lower()
    sniffed = _sniff_content_type(
        payload,
        declared,
        max_entry_expanded_bytes=limits.max_attachment_bytes,
        max_total_expanded_bytes=limits.max_total_attachment_bytes,
    )
    if (
        sniffed is None
        or sniffed not in _SUPPORTED_CONTENT_TYPES
        or not _declared_type_is_compatible(declared, sniffed)
    ):
        raise _security_error("attachment_content_type_mismatch")

    return ParsedInboundAttachment(
        filename=_sanitize_filename(part.get_filename()),
        content_type=sniffed,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content=payload,
    )


def _decode_text(part: Message, limits: InboundMimeLimits) -> str:
    payload = _strict_payload_bytes(
        part,
        decoded_limits=((limits.max_body_bytes, "body_too_large"),),
    )

    charset = part.get_content_charset() or "utf-8"
    try:
        decoded = payload.decode(charset)
    except LookupError:
        decoded = payload.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        try:
            decoded = payload.decode("cp1252")
        except UnicodeDecodeError:
            decoded = payload.decode("utf-8", errors="replace")
    if "\x00" in decoded:
        raise _security_error("malformed_message")
    return decoded


class _PlainTextHTMLParser(HTMLParser):
    _IGNORED_TAGS = {"script", "style", "template"}
    _BREAK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "pre",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.ignored_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self.ignored_tags.append(tag)
        elif not self.ignored_tags and tag in self._BREAK_TAGS:
            self.fragments.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            # HTML does not define script, style, or template as void
            # elements. Treat a self-closing spelling as an opening tag and
            # suppress everything through a corresponding end tag (or EOF).
            self.ignored_tags.append(tag)
        elif not self.ignored_tags and tag in self._BREAK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            if self.ignored_tags and self.ignored_tags[-1] == tag:
                self.ignored_tags.pop()
        elif not self.ignored_tags and tag in self._BREAK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_tags:
            self.fragments.append(data)

    def plain_text(self) -> str:
        text = "".join(self.fragments).replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(value: str) -> str:
    parser = _PlainTextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise _security_error("malformed_message") from exc
    return parser.plain_text()


_BodyCandidates = tuple[tuple[str, ...], tuple[str, ...]]


def _related_root(
    part: Message,
    children: list[Message],
) -> Message | None:
    """Choose the multipart/related part that carries the readable body.

    This only selects which of the sender's own parts is shown as the body;
    every attachment is still extracted by the independent traversal, so it is
    never a reason to quarantine a message. Real mail routinely carries
    Content-IDs that are not RFC message IDs (Gmail's inline images use
    ``<ii_...>`` and its bounce notices ``<icon.png>``), so a ``start`` that
    does not name exactly one well-formed Content-ID falls back to the
    RFC 2387 default root, the first part.
    """
    if not children:
        return None
    declared_start = part.get_param("start", header="content-type")
    if declared_start is None:
        return children[0]
    expected_content_id = str(declared_start).strip()
    if _MESSAGE_ID.fullmatch(expected_content_id) is None:
        return children[0]
    matches = [
        child
        for child in children
        if [str(value).strip() for value in child.get_all("Content-ID", [])]
        == [expected_content_id]
    ]
    return matches[0] if len(matches) == 1 else children[0]


def _body_candidates(
    part: Message,
    decoded_bodies: Mapping[int, str],
    *,
    is_root: bool,
) -> _BodyCandidates:
    if _is_attachment(part, is_root=is_root):
        return (), ()

    if not part.is_multipart():
        body = decoded_bodies.get(id(part))
        if body is None:
            return (), ()
        if part.get_content_type() == "text/plain":
            return (body,), ()
        if part.get_content_type() == "text/html":
            return (), (body,)
        return (), ()

    children = part.get_payload()
    if not isinstance(children, list):
        raise _security_error("malformed_message")

    subtype = part.get_content_subtype()
    if subtype == "related":
        root = _related_root(part, children)
        if root is None:
            return (), ()
        return _body_candidates(
            root,
            decoded_bodies,
            is_root=False,
        )

    if subtype == "alternative":
        plain_candidates: list[str] = []
        html_candidates: list[str] = []
        for child in children:
            child_plain, child_html = _body_candidates(
                child,
                decoded_bodies,
                is_root=False,
            )
            plain_candidates.extend(child_plain)
            html_candidates.extend(child_html)
        return tuple(plain_candidates), tuple(html_candidates)

    # multipart/mixed and other container types define a primary message body
    # followed by additional material. Select the first body-bearing child;
    # the independent evidence traversal still extracts every attachment.
    for child in children:
        candidates = _body_candidates(
            child,
            decoded_bodies,
            is_root=False,
        )
        if candidates[0] or candidates[1]:
            return candidates
    return (), ()


def _select_body(candidates: _BodyCandidates) -> str:
    plain_candidates, html_candidates = candidates
    for candidate in plain_candidates:
        if candidate.strip():
            return candidate
    for candidate in html_candidates:
        body = _html_to_text(candidate)
        if body:
            return body
    return ""


def _clean_header(value) -> str:
    value = str(value or "")
    if "\x00" in value:
        raise _security_error("malformed_message")
    return "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )


def _decoded_headers(
    message: Message,
    limits: InboundMimeLimits,
) -> Mapping[str, str]:
    decoded: dict[str, str] = {}
    for name in _PERSISTED_HEADER_ALLOWLIST:
        values = [
            _clean_header(value)
            for value in message.get_all(name, [])
        ]
        if not values:
            continue
        value = ", ".join(values)
        if len(value.encode("utf-8")) > limits.max_header_line_bytes:
            raise _security_error("header_line_too_long")
        decoded[name] = value
    return MappingProxyType(decoded)


def _addresses(values) -> tuple[str, ...]:
    addresses: list[str] = []
    seen: set[str] = set()
    raw_values = [str(value) for value in values]
    if any("\x00" in value for value in raw_values):
        raise _security_error("malformed_message")
    for _display_name, address in getaddresses(raw_values):
        address = address.strip()
        canonical = address.casefold()
        if address and canonical not in seen:
            seen.add(canonical)
            addresses.append(address)
    return tuple(addresses)


def _message_ids(value) -> tuple[str, ...]:
    value = str(value or "")
    if "\x00" in value:
        raise _security_error("malformed_message")
    return tuple(_MESSAGE_ID.findall(value))


def _one_dsn_field(part: Message, name: str) -> str:
    values = part.get_all(name, [])
    if len(values) != 1:
        raise _security_error("malformed_message")
    value = str(values[0]).strip()
    if not value or any(
        unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise _security_error("malformed_message")
    return value


def _validate_dsn_typed_address(value: str) -> None:
    address_type, separator, address = value.partition(";")
    if (
        not separator
        or _DSN_ADDRESS_TYPE.fullmatch(address_type.strip()) is None
        or not address.strip()
    ):
        raise _security_error("malformed_message")


def _dsn_recipient_fingerprint(address: str) -> str:
    canonical = unicodedata.normalize("NFKC", str(address).strip()).casefold()
    return hashlib.sha256(
        b"psd401-prr-dsn-recipient-v1\0" + canonical.encode("utf-8")
    ).hexdigest()


def _dsn_recipient_address_fingerprint(value: str) -> str:
    address_type, _separator, address = value.partition(";")
    if address_type.strip().casefold() != "rfc822":
        return ""
    normalized_address = address.strip()
    parsed_addresses = _addresses([normalized_address])
    if (
        len(parsed_addresses) != 1
        or parsed_addresses[0].casefold()
        != normalized_address.casefold()
    ):
        raise _security_error("malformed_message")
    return _dsn_recipient_fingerprint(parsed_addresses[0])


def _strict_message_id(message: Message) -> str:
    values = message.get_all("Message-ID", [])
    if len(values) != 1:
        raise _security_error("malformed_message")
    message_id = str(values[0]).strip()
    if _MESSAGE_ID.fullmatch(message_id) is None:
        raise _security_error("malformed_message")
    return message_id


def _parse_header_only_payload(
    payload: bytes,
    *,
    existing_header_count: int,
    limits: InboundMimeLimits,
) -> Message:
    # A text/rfc822-headers body is header evidence, not an unrestricted text
    # body. Remove only the boundary-adjacent terminal line endings; any
    # internal blank line or body prose remains malformed header evidence.
    header_block = payload.rstrip(b"\r\n")
    state = [0, existing_header_count]
    headers = _preflight_raw_headers(header_block, limits, state)
    if any(
        len(headers.get(name.encode("ascii"), ())) > 1
        for name in _SINGLETON_HEADERS
    ):
        raise _security_error("malformed_message")
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(
            header_block + b"\r\n\r\n",
            headersonly=True,
        )
    except (MessageDefect, UnicodeError, ValueError, TypeError) as exc:
        raise _security_error("malformed_message") from exc
    if parsed.defects:
        raise _security_error("malformed_message")
    _validate_part_headers(parsed, limits)
    return parsed


def _parse_encoded_original_message(
    payload: bytes,
    *,
    outer_part_count: int,
    outer_header_count: int,
    limits: InboundMimeLimits,
) -> Message:
    _validate_top_level_header_block(payload, limits)
    _preflight_raw_structure(payload, limits)
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(payload)
    except (MessageDefect, UnicodeError, ValueError, TypeError) as exc:
        raise _security_error("malformed_message") from exc
    nested_part_count, nested_header_count, nested_depth = (
        _validate_message_structure(parsed, limits)
    )

    # The outer parser represents a transfer-encoded message/rfc822 body as
    # one unvalidated pseudo-child. Replace that one placeholder with the
    # decoded tree when enforcing the public aggregate limits.
    if outer_part_count - 1 + nested_part_count > limits.max_parts:
        raise _security_error("too_many_mime_parts")
    if outer_header_count + nested_header_count > limits.max_header_count:
        raise _security_error("too_many_headers")
    if nested_depth + 2 > limits.max_depth:
        raise _security_error("mime_depth_exceeded")
    return parsed


def _delivery_status_recipients(
    delivery_status_part: Message,
) -> tuple[ParsedDeliveryStatusRecipient, ...]:
    blocks = delivery_status_part.get_payload()
    if not isinstance(blocks, list) or len(blocks) < 2:
        raise _security_error("malformed_message")

    # RFC 3464 requires a per-message Reporting-MTA field before the ordered
    # per-recipient blocks. Validate it, but intentionally do not retain it.
    _validate_dsn_typed_address(_one_dsn_field(blocks[0], "Reporting-MTA"))

    recipients: list[ParsedDeliveryStatusRecipient] = []
    for recipient_index, block in enumerate(blocks[1:], start=1):
        final_recipient = _one_dsn_field(block, "Final-Recipient")
        _validate_dsn_typed_address(final_recipient)
        action = _one_dsn_field(block, "Action").casefold()
        expected_status_class = _DSN_STATUS_CLASS_BY_ACTION.get(action)
        status = _one_dsn_field(block, "Status")
        status_match = _ENHANCED_STATUS.fullmatch(status)
        if (
            expected_status_class is None
            or status_match is None
            or status_match.group(1) != expected_status_class
        ):
            raise _security_error("malformed_message")
        recipients.append(
            ParsedDeliveryStatusRecipient(
                recipient_index=recipient_index,
                action=action,
                status=status,
                _recipient_fingerprint=(
                    _dsn_recipient_address_fingerprint(final_recipient)
                ),
            )
        )
    return tuple(recipients)


def _parse_delivery_status_report(
    message: Message,
    *,
    attached_message_payloads: Mapping[int, bytes],
    structure_counts: tuple[int, int, int],
    limits: InboundMimeLimits,
) -> ParsedDeliveryStatusReport | None:
    if message.get_content_type().casefold() != "multipart/report":
        return None
    report_type = message.get_param("report-type", header="content-type")
    if (
        report_type is None
        or str(report_type).strip().casefold() != "delivery-status"
    ):
        # A top-level automated report is never requester-authored evidence.
        # Unsupported or mislabeled reports fail closed instead of entering
        # the generic reply/clarification matcher.
        raise _security_error("malformed_message")

    children = message.get_payload()
    if not isinstance(children, list):
        raise _security_error("malformed_message")
    delivery_status_parts = [
        child
        for child in children
        if child.get_content_type().casefold() == "message/delivery-status"
    ]
    original_parts = [
        child
        for child in children
        if child.get_content_type().casefold()
        in {"message/rfc822", "text/rfc822-headers"}
    ]
    if len(delivery_status_parts) != 1 or len(original_parts) != 1:
        raise _security_error("malformed_message")
    delivery_status_part = delivery_status_parts[0]
    original_part = original_parts[0]
    if children.index(delivery_status_part) >= children.index(original_part):
        raise _security_error("malformed_message")

    recipients = _delivery_status_recipients(delivery_status_part)
    _part_count, header_count, _maximum_depth = structure_counts
    if original_part.get_content_type().casefold() == "text/rfc822-headers":
        payload = _strict_payload_bytes(
            original_part,
            decoded_limits=((limits.max_body_bytes, "body_too_large"),),
        )
        original_message = _parse_header_only_payload(
            payload,
            existing_header_count=header_count,
            limits=limits,
        )
    else:
        transfer_encoding = str(
            original_part.get("Content-Transfer-Encoding", "")
        ).strip().casefold()
        if transfer_encoding in {"base64", "quoted-printable"}:
            payload = attached_message_payloads.get(id(original_part))
            if payload is None:
                raise _security_error("malformed_message")
            original_message = _parse_encoded_original_message(
                payload,
                outer_part_count=structure_counts[0],
                outer_header_count=header_count,
                limits=limits,
            )
        else:
            embedded_messages = original_part.get_payload()
            if (
                not isinstance(embedded_messages, list)
                or len(embedded_messages) != 1
            ):
                raise _security_error("malformed_message")
            original_message = embedded_messages[0]

    return ParsedDeliveryStatusReport(
        original_message_id=_strict_message_id(original_message),
        recipients=recipients,
    )


def parse_inbound_message(
    raw_message: bytes,
    *,
    limits: InboundMimeLimits = DEFAULT_INBOUND_MIME_LIMITS,
) -> ParsedInboundMessage:
    """Parse one raw message without network, filesystem, or rendering I/O."""
    if not isinstance(raw_message, bytes):
        raise TypeError("raw_message must be bytes")
    if len(raw_message) > limits.max_raw_bytes:
        raise _security_error("message_too_large")

    _validate_top_level_header_block(raw_message, limits)
    attached_message_spans = _preflight_raw_structure(raw_message, limits)
    try:
        # Defects are inspected explicitly while traversing. Using
        # ``raise_on_defect`` here would incorrectly reject a standards-valid
        # base64-encoded message/rfc822 attachment because the stdlib tries to
        # parse its encoded text before transfer-decoding it.
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except (MessageDefect, UnicodeError, ValueError, TypeError) as exc:
        raise _security_error("malformed_message") from exc

    try:
        structure_counts = _validate_message_structure(message, limits)
    except InboundMimeSecurityError:
        raise
    except (MessageDefect, UnicodeError, ValueError, TypeError) as exc:
        raise _security_error("malformed_message") from exc
    decoded_bodies: dict[int, str] = {}
    attachments: list[ParsedInboundAttachment] = []
    attached_message_payloads: dict[int, bytes] = {}
    attachment_total = 0
    attached_message_span_index = 0
    stack: list[tuple[Message, int]] = [(message, 0)]

    try:
        while stack:
            part, depth = stack.pop()

            is_attachment = _is_attachment(part, is_root=depth == 0)
            if is_attachment:
                attachment_count = len(attachments) + 1
                if attachment_count > limits.max_attachment_count:
                    raise _security_error("too_many_attachments")
                decoded_limits = (
                    (
                        limits.max_attachment_bytes,
                        "attachment_too_large",
                    ),
                    (
                        limits.max_total_attachment_bytes - attachment_total,
                        "attachment_total_too_large",
                    ),
                )
                if part.get_content_type() == "message/rfc822":
                    if depth == 0:
                        # A root message/rfc822 is a wrapper, not a MIME
                        # attachment. If it declares attachment disposition,
                        # there is no enclosing boundary span from which to
                        # preserve an exact decoded payload.
                        raise _security_error("malformed_message")
                    if attached_message_span_index >= len(
                        attached_message_spans
                    ):
                        raise _security_error("malformed_message")
                    attached_message_span = attached_message_spans[
                        attached_message_span_index
                    ]
                    attached_message_span_index += 1
                    payload = _attached_message_bytes(
                        part,
                        raw_message,
                        attached_message_span,
                        decoded_limits=decoded_limits,
                    )
                    attached_message_payloads[id(part)] = payload
                else:
                    payload = _strict_payload_bytes(
                        part,
                        decoded_limits=decoded_limits,
                    )
                parsed_attachment = _attachment_from_part(
                    part,
                    payload,
                    attachment_count=attachment_count,
                    attachment_total=attachment_total,
                    limits=limits,
                )
                attachments.append(parsed_attachment)
                attachment_total += parsed_attachment.size
                continue

            if part.is_multipart():
                children = part.get_payload()
                if not isinstance(children, list):
                    raise _security_error("malformed_message")
                stack.extend(
                    (child, depth + 1) for child in reversed(children)
                )
                continue

            content_type = part.get_content_type()
            if content_type in {"text/plain", "text/html"}:
                decoded_bodies[id(part)] = _decode_text(part, limits)

        if attached_message_span_index != len(attached_message_spans):
            raise _security_error("malformed_message")
        delivery_status_report = _parse_delivery_status_report(
            message,
            attached_message_payloads=attached_message_payloads,
            structure_counts=structure_counts,
            limits=limits,
        )
        body = _select_body(
            _body_candidates(
                message,
                decoded_bodies,
                is_root=True,
            )
        )
        if delivery_status_report is not None:
            # The normalized report below is the staff-facing evidence. Human
            # provider prose, embedded headers, and failed-recipient addresses
            # remain only in the quarantined raw message.
            body = ""
            attachments = []
    except InboundMimeSecurityError:
        raise
    except (MessageDefect, UnicodeError, ValueError, TypeError) as exc:
        raise _security_error("malformed_message") from exc

    if len(body.encode("utf-8")) > limits.max_body_bytes:
        raise _security_error("body_too_large")

    headers = _decoded_headers(message, limits)
    from_addresses = _addresses(message.get_all("From", []))
    message_ids = _message_ids(message.get("Message-ID"))
    return ParsedInboundMessage(
        subject=_clean_header(message.get("Subject")),
        body=body,
        from_address=from_addresses[0] if from_addresses else "",
        to_addresses=_addresses(message.get_all("To", [])),
        cc_addresses=_addresses(message.get_all("Cc", [])),
        rfc_message_id=message_ids[0] if message_ids else "",
        in_reply_to=_message_ids(message.get("In-Reply-To")),
        references=_message_ids(message.get("References")),
        headers=headers,
        attachments=tuple(attachments),
        delivery_status_report=delivery_status_report,
    )
