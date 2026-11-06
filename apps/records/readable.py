# ABOUTME: A record's extracted text, cleaned for a person to read in review:
# ABOUTME: email markup an inbox would never show is dropped. Read-only.
"""Readable text for review.

``RecordItem.ocr_text`` is kept exactly as extracted, because search reads
it. Email bodies arrive in it with Outlook conditional comments, style
blocks, tags, and markdown image and link syntax, which a reviewer should
not have to read past. ``readable_text`` returns a reading copy without
them. It never writes anything back.
"""

import html
import re

from apps.audit.models import audit

from .models import RecordItem

#: The longest reading copy served at once; a reviewer needing more opens the
#: page preview or the original.
READABLE_TEXT_LIMIT = 200_000

_CONDITIONAL = re.compile(r"<!--\[if[^\]]*\]>.*?<!\[endif\]-->", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>", re.IGNORECASE)
#: Real HTML element names, and Office-namespaced ones such as v:background
#: or o:p, with nothing after the name but name="value" attributes. Any
#: other "<...>" is text a person wrote ("<REDACTED>", "<Mark Smith>",
#: "<jane@example.org>", "<https://...>", "a < b") and stays.
_ELEMENTS = (
    "a|abbr|address|area|article|aside|b|base|bdi|bdo|blockquote|body|br|button|"
    "caption|center|cite|code|col|colgroup|dd|del|details|dfn|div|dl|dt|em|embed|"
    "fieldset|figcaption|figure|font|footer|form|h[1-6]|head|header|hr|html|i|iframe|"
    "img|input|ins|kbd|label|legend|li|link|main|map|mark|meta|nav|noscript|object|"
    "ol|optgroup|option|p|param|picture|pre|q|s|samp|section|select|small|source|"
    "span|strike|strong|style|sub|summary|sup|svg|table|tbody|td|template|"
    "textarea|tfoot|th|thead|time|title|tr|track|tt|u|ul|var|video|wbr|xml"
)
_ATTRIBUTES = r"""(?:\s+[\w:-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>]+))*"""
_TAG = re.compile(
    r"<(?:/?(?:" + _ELEMENTS + r"|(?:o|v|w|m|st1):[a-z]+)" + _ATTRIBUTES
    + r"\s*/?|!doctype[^<>]*)>",
    re.IGNORECASE,
)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:https?|mailto):[^)]*\)")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_INVISIBLE = re.compile("[\\u200b\\u200c\\u200d\\ufeff\\u00ad]")


def readable_text(raw: str) -> str:
    """``raw`` without markup, one blank line at most between paragraphs."""
    text = _CONDITIONAL.sub("", raw)
    text = _COMMENT.sub("", text)
    text = _BLOCK.sub("", text)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = html.unescape(text).replace("\u00a0", " ")
    text = _INVISIBLE.sub("", text)
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        # A table cell border left behind as a lone pipe carries nothing.
        if line.strip() in {"|", "||"}:
            continue
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


def record_reading_text(item: RecordItem, *, actor) -> dict:
    """The reading copy of one record, audited like a page preview."""
    text = readable_text(item.ocr_text or "")
    truncated = len(text) > READABLE_TEXT_LIMIT
    audit(
        "record.text_viewed",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
    )
    return {"text": text[:READABLE_TEXT_LIMIT], "truncated": truncated}
