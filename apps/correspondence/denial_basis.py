"""Cycle-free validation for immutable denial exemption evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from django.core.exceptions import ValidationError

from apps.audit.models import AuditLog

from .models import Correspondence


def _normalized_citation_text(value: str) -> str:
    """Normalize case and whitespace without accepting compatibility glyphs."""
    return " ".join(
        unicodedata.normalize("NFC", str(value or "")).lower().split()
    )


def _is_citation_word_character(value: str) -> bool:
    category = unicodedata.category(value) if value else ""
    return bool(value) and (
        value.isalnum()
        or value in {"_", "§"}
        or category.startswith(("C", "M"))
    )


def _has_whitespace_subsection_extension(body: str, *, end: int) -> bool:
    """Detect a short legal subsection token after normalized whitespace."""
    if body[end : end + 1] != " ":
        return False
    tail = body[end + 1 :]
    if not tail:
        return False
    opener = unicodedata.normalize("NFKC", tail[0])
    closer = {"(": ")", "[": "]"}.get(opener)
    if closer is None:
        return False
    token_characters = []
    for character in tail[1:10]:
        if unicodedata.normalize("NFKC", character) == closer:
            token = unicodedata.normalize(
                "NFKC",
                "".join(token_characters),
            ).lower()
            return (
                re.fullmatch(
                    r"(?:\d+[a-z]?|[a-z]|[ivxlcdm]+)",
                    token,
                )
                is not None
            )
        token_characters.append(character)
    return False


def _is_complete_citation_occurrence(
    body: str,
    *,
    start: int,
    citation_length: int,
) -> bool:
    before = body[start - 1] if start else ""
    end = start + citation_length
    after = body[end] if end < len(body) else ""
    following = body[end + 1] if end + 1 < len(body) else ""
    compatibility_after = unicodedata.normalize("NFKC", after)
    after_category = unicodedata.category(after) if after else ""
    if _is_citation_word_character(before):
        return False
    if (
        _is_citation_word_character(after)
        or compatibility_after in {"(", "["}
    ):
        return False
    if (
        after_category.startswith(("P", "S"))
        and _is_citation_word_character(following)
    ):
        return False
    return not _has_whitespace_subsection_extension(body, end=end)


def require_recipient_visible_denial_citations(
    body: str,
    citations: list[str],
) -> None:
    """Require each frozen authority as a complete string in visible prose."""
    normalized_body = _normalized_citation_text(body)
    for citation in citations:
        normalized_citation = _normalized_citation_text(citation)
        start = normalized_body.find(normalized_citation)
        while start >= 0:
            if _is_complete_citation_occurrence(
                normalized_body,
                start=start,
                citation_length=len(normalized_citation),
            ):
                break
            start = normalized_body.find(
                normalized_citation,
                start + 1,
            )
        if not normalized_citation or start < 0:
            raise ValidationError(
                "The recipient-visible body must contain every selected "
                "exemption citation exactly."
            )


def _snapshot_rows(bases) -> list[dict]:
    return [
        {
            "id": basis.pk,
            "selected_by_id": basis.selected_by_id,
            "created_at": basis.created_at.isoformat(),
            "exemption_id": basis.exemption_id,
            "citation": basis.citation_snapshot,
            "short_name": basis.short_name_snapshot,
            "description": basis.description_snapshot,
            "requires_second_review": basis.requires_second_review_snapshot,
        }
        for basis in bases
    ]


def _serialize_evidence(
    rows: list[dict],
) -> tuple[str, int, list[str], list[dict]]:
    serialized = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        len(rows),
        [row["citation"] for row in rows],
        rows,
    )


def denial_basis_evidence(
    correspondence: Correspondence,
    *,
    lock: bool = False,
) -> tuple[str, int, list[str], list[dict]]:
    """Validate current taxonomy and return the immutable basis evidence."""
    if correspondence.kind != Correspondence.Kind.DENIAL:
        return "", 0, [], []
    queryset = correspondence.denial_citations.select_related(
        "exemption"
    ).order_by("citation_snapshot", "exemption_id", "pk")
    if lock:
        queryset = queryset.select_for_update(of=("self", "exemption"))
    bases = list(queryset)
    rows = _snapshot_rows(bases)
    if not rows or any(not basis.exemption.active for basis in bases):
        raise ValidationError(
            "A denial requires at least one active exemption citation."
        )
    if any(
        basis.citation_snapshot != basis.exemption.citation
        or basis.short_name_snapshot != basis.exemption.short_name
        or basis.description_snapshot != basis.exemption.description
        or basis.requires_second_review_snapshot
        != basis.exemption.requires_second_review
        for basis in bases
    ):
        raise ValidationError(
            "The denial citation snapshot no longer matches the current "
            "exemption taxonomy; edit the draft or create a replacement."
        )
    if any(row["requires_second_review"] for row in rows):
        raise ValidationError(
            "This denial basis requires persisted second-review evidence, "
            "which correspondence approval does not yet support."
        )
    return _serialize_evidence(rows)


def _frozen_denial_basis_evidence(
    correspondence: Correspondence,
    *,
    lock: bool = False,
) -> tuple[str, int, list[str], list[dict]]:
    """Return evidence derived only from the immutable through-row snapshot."""
    if correspondence.kind != Correspondence.Kind.DENIAL:
        return "", 0, [], []
    queryset = correspondence.denial_citations.order_by(
        "citation_snapshot",
        "exemption_id",
        "pk",
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return _serialize_evidence(_snapshot_rows(list(queryset)))


def require_approved_denial_basis_snapshot(
    correspondence: Correspondence,
    *,
    lock: bool = False,
) -> None:
    if correspondence.kind != Correspondence.Kind.DENIAL:
        return
    approvals = list(
        AuditLog.objects.filter(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.id),
        ).values_list("detail", flat=True)[:2]
    )
    (
        actual_sha256,
        actual_count,
        actual_citations,
        actual_snapshot,
    ) = _frozen_denial_basis_evidence(correspondence, lock=lock)
    if len(approvals) != 1:
        raise ValidationError(
            "Approved denial-basis snapshot integrity check failed."
        )
    detail = approvals[0]
    expected_sha256 = detail.get("denial_citation_snapshot_sha256")
    expected_count = detail.get("denial_citation_count")
    expected_citations = detail.get("denial_exemption_citations")
    expected_snapshot = detail.get("denial_basis_snapshot")
    expected_body_sha256 = detail.get("body_sha256")
    actual_body_sha256 = hashlib.sha256(
        correspondence.approved_body.encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or not isinstance(expected_body_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_body_sha256) is None
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or actual_count < 1
        or expected_count != actual_count
        or expected_sha256 != actual_sha256
        or expected_body_sha256 != actual_body_sha256
        or expected_citations != actual_citations
        or expected_snapshot != actual_snapshot
    ):
        raise ValidationError(
            "Approved denial-basis snapshot integrity check failed."
        )
    require_recipient_visible_denial_citations(
        correspondence.approved_body,
        actual_citations,
    )
