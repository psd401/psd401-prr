# ABOUTME: Deterministic record tags — facts derived from stored email
# ABOUTME: metadata, never decisions about responsiveness or exemption.
"""Deterministic tags for the record review queue.

Each tag answers a question about the *message*, not about the law:

``auto_reply``
    The message announced itself as machine-generated (``Auto-Submitted``, a
    bulk ``Precedence``, or an out-of-office subject).
``calendar``
    The message carried a ``text/calendar`` part or an ``.ics`` attachment.
``bulk_list``
    The message came through a mailing list or bulk sender.
``external_sender``
    The sending domain is not one of the district's own mail domains.
``counsel_domain``
    The district's own law firms took part, per the administrator-managed
    :class:`~apps.records.models.CounselDomain` list (seeded empty).
``privilege_marker``
    The message text or subject carries an attorney-client or work-product
    marker.

A tag never decides anything. ``counsel_domain`` and ``privilege_marker``
separate candidates into the privilege queue so a human sees them before
anything ships; the withheld path, its required exemption citation, and second
review are unchanged.

Every tag is defined once, as a ``Q`` object. Display and filtering read the
same definition — the queryset annotates each tag with the very predicate the
filter uses, so a tag shown on a row and a tag filtered on can never disagree.
"""

from __future__ import annotations

from django.db.models import BooleanField, Case, Q, Value, When

from apps.configuration import runtime

AUTO_REPLY = "auto_reply"
CALENDAR = "calendar"
BULK_LIST = "bulk_list"
EXTERNAL_SENDER = "external_sender"
COUNSEL_DOMAIN = "counsel_domain"
PRIVILEGE_MARKER = "privilege_marker"

#: Stable display order; the API and the UI both present tags this way.
TAG_NAMES = (
    AUTO_REPLY,
    CALENDAR,
    BULK_LIST,
    EXTERNAL_SENDER,
    COUNSEL_DOMAIN,
    PRIVILEGE_MARKER,
)

TAG_LABELS = {
    AUTO_REPLY: "Automatic reply",
    CALENDAR: "Calendar",
    BULK_LIST: "Bulk or list mail",
    EXTERNAL_SENDER: "External sender",
    COUNSEL_DOMAIN: "District counsel",
    PRIVILEGE_MARKER: "Privilege marker",
}

#: The tags that put a record in the privilege queue.
PRIVILEGE_TAGS = (COUNSEL_DOMAIN, PRIVILEGE_MARKER)

#: Subject phrases an out-of-office responder writes for itself.
_AUTO_REPLY_SUBJECTS = (
    "out of office",
    "out-of-office",
    "automatic reply",
    "auto reply",
    "auto-reply",
    "autoreply",
    "automatische antwort",
)

#: Bulk/list values of the Precedence header. "list" and "bulk" are the
#: conventional markers and "junk" travels with them. RFC 3834's own
#: "auto_reply" spelling is not here: it says the message is machine-generated,
#: not that it came through a list, so `_auto_reply_q` reads it instead.
_BULK_PRECEDENCE = ("bulk", "list", "junk")

#: Markers a lawyer writes on a privileged communication. Matching one is a
#: reason for a human to look, never a reason to withhold.
_PRIVILEGE_PHRASES = (
    "attorney-client",
    "attorney client",
    "attorney/client",
    "attorney work product",
    "attorney work-product",
    "work product privilege",
    "privileged and confidential",
    "privileged & confidential",
    "subject to attorney",
)

#: The annotation name for one tag, e.g. ``tag_auto_reply``.
ANNOTATION_PREFIX = "tag_"


def annotation_name(tag: str) -> str:
    return f"{ANNOTATION_PREFIX}{tag}"


def district_email_domains() -> tuple[str, ...]:
    """The district's own mail domains, lowercased."""
    return tuple(
        domain.lower()
        for domain in runtime.get("PRR_DISTRICT_EMAIL_DOMAINS")
        if domain
    )


def counsel_domains() -> list[str]:
    """The administrator-managed district-counsel domains, lowercased.

    Seeded empty on purpose: privilege attaches to communications with the
    district's *own* counsel, and only an application administrator knows
    which firms those are. An empty list means the tag matches nothing.
    """
    from .models import CounselDomain

    return list(
        CounselDomain.objects.filter(active=True)
        .order_by("domain")
        .values_list("domain", flat=True)
    )


def _text_contains_any(phrases, *fields) -> Q:
    query = Q(pk__in=[])
    for phrase in phrases:
        for name in fields:
            query |= Q(**{f"{name}__icontains": phrase})
    return query


def _auto_reply_q() -> Q:
    submitted = ~Q(email_auto_submitted="") & ~Q(email_auto_submitted__iexact="no")
    precedence = Q(email_precedence__iexact="auto_reply")
    subject = _text_contains_any(
        _AUTO_REPLY_SUBJECTS,
        "original_filename",
    )
    return submitted | precedence | subject


def _bulk_list_q() -> Q:
    listed = ~Q(email_list_id="")
    precedence = Q(pk__in=[])
    for value in _BULK_PRECEDENCE:
        precedence |= Q(email_precedence__iexact=value)
    return listed | precedence


def _external_sender_q() -> Q:
    domains = district_email_domains()
    known = ~Q(email_sender_domain="")
    if not domains:
        # Nothing is configured as the district's own, so "external" has no
        # meaning here. Tagging every record external would be a confident
        # claim built on an unset setting, and a reviewer filtering on it
        # would be told that staff mail came from outside. An always-false
        # predicate keeps the tag present and empty instead.
        return Q(pk__in=[])
    return known & ~Q(email_sender_domain__in=domains)


def _counsel_domain_q(domains) -> Q:
    query = Q(pk__in=[])
    if not domains:
        # No firms entered yet, so nothing is district counsel. Returning an
        # always-false predicate keeps the tag present but empty rather than
        # silently tagging every record.
        return query
    for domain in domains:
        query |= Q(email_sender_domain__iexact=domain)
        # The correspondent columns hold a ", "-joined list of normalized
        # addresses, so an address for this domain either ends the string or is
        # followed by the separator. Matching the bare substring instead would
        # tag "@lawfirm.example.attacker.test" as district counsel, which is a
        # privilege claim nobody made. `email_from` is checked the same way as
        # the recipients: a From header may carry several addresses, and
        # `email_sender_domain` only records the first, so counsel sitting
        # second would otherwise be missed by both halves of this test.
        for name in ("email_from", "email_to", "email_cc"):
            query |= Q(**{f"{name}__iendswith": f"@{domain}"})
            query |= Q(**{f"{name}__icontains": f"@{domain}, "})
    return query


def _privilege_marker_q() -> Q:
    return _text_contains_any(
        _PRIVILEGE_PHRASES,
        "original_filename",
        "ocr_text",
    )


def tag_queries(*, counsel=None) -> dict[str, Q]:
    """Every tag's predicate, resolved against the current configuration.

    ``counsel`` lets a caller pass the domain list it already read so a list
    request does not query the table once per tag.
    """
    domains = counsel_domains() if counsel is None else list(counsel)
    return {
        AUTO_REPLY: _auto_reply_q(),
        CALENDAR: Q(email_has_calendar_part=True),
        BULK_LIST: _bulk_list_q(),
        EXTERNAL_SENDER: _external_sender_q(),
        COUNSEL_DOMAIN: _counsel_domain_q(domains),
        PRIVILEGE_MARKER: _privilege_marker_q(),
    }


def annotate_tags(queryset, *, counsel=None):
    """Annotate one boolean per tag using the filters' own predicates."""
    queries = tag_queries(counsel=counsel)
    return queryset.annotate(**{
        annotation_name(tag): Case(
            When(query, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        )
        for tag, query in queries.items()
    })


def filter_by_tag(queryset, tag: str, *, counsel=None):
    """Narrow to the records carrying ``tag``.

    ``privilege`` is the queue, not a tag: it is the union of the tags that
    mean a human must look before anything ships.
    """
    queries = tag_queries(counsel=counsel)
    if tag == "privilege":
        union = Q(pk__in=[])
        for name in PRIVILEGE_TAGS:
            union |= queries[name]
        return queryset.filter(union)
    if tag not in queries:
        return queryset.none()
    return queryset.filter(queries[tag])


def tags_for_item(item, *, counsel=None) -> list[str]:
    """The tags on one record, in display order.

    Reads the annotations :func:`annotate_tags` added when they are present,
    which is the case for every list and detail response. Falls back to one
    bounded existence query for an instance that arrived another way, so a tag
    is never silently dropped.
    """
    from .models import RecordItem

    present = []
    missing = []
    for tag in TAG_NAMES:
        value = getattr(item, annotation_name(tag), None)
        if value is None:
            missing.append(tag)
        elif value:
            present.append(tag)
    if missing:
        queries = tag_queries(counsel=counsel)
        matched = (
            RecordItem.objects.filter(pk=item.pk)
            .annotate(**{
                annotation_name(tag): Case(
                    When(queries[tag], then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                )
                for tag in missing
            })
            .values(*[annotation_name(tag) for tag in missing])
            .first()
        ) or {}
        present.extend(
            tag for tag in missing if matched.get(annotation_name(tag))
        )
    return [tag for tag in TAG_NAMES if tag in set(present)]
