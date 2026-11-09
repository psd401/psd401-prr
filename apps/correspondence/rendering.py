"""Allowlisted, versioned plain-text correspondence rendering."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.template.loader import render_to_string


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    body: str


TEMPLATES = {
    ("acknowledgment", "1"): "correspondence/acknowledgment/v1.txt",
    ("acknowledgment", "2"): "correspondence/acknowledgment/v2.txt",
    ("clarification_request", "1"): "correspondence/clarification_request/v1.txt",
    ("estimate_revision", "1"): "correspondence/estimate_revision/v1.txt",
    ("estimate_revision", "2"): "correspondence/estimate_revision/v2.txt",
    ("fee_estimate", "1"): "correspondence/fee_estimate/v1.txt",
    ("interpretation_estimate", "1"): "correspondence/interpretation_estimate/v1.txt",
    ("installment_notice", "1"): "correspondence/installment_notice/v1.txt",
    ("installment_notice", "2"): "correspondence/installment_notice/v2.txt",
    ("installment_notice", "3"): "correspondence/installment_notice/v3.txt",
    ("submission_receipt", "1"): "correspondence/submission_receipt/v1.txt",
    ("no_records_closure", "1"): "correspondence/no_records_closure/v1.txt",
    ("no_records_closure", "2"): "correspondence/no_records_closure/v2.txt",
    ("completion_closure", "1"): "correspondence/completion_closure/v1.txt",
    ("nonpayment_closure", "1"): "correspondence/nonpayment_closure/v1.txt",
    ("third_party_notice", "1"): "correspondence/third_party_notice/v1.txt",
    ("scope_clarification", "1"): "correspondence/scope_clarification/v1.txt",
}


def canonical_body(value: str) -> str:
    body = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValidationError("Correspondence body cannot be empty.")
    return f"{body}\n"


def validate_subject(value: str) -> str:
    subject = (value or "").strip()
    if not subject:
        raise ValidationError("Correspondence subject cannot be empty.")
    if any(
        unicodedata.category(character).startswith("C")
        for character in subject
    ):
        raise ValidationError(
            "Correspondence subject cannot contain control characters."
        )
    if len(subject) > 998:
        raise ValidationError("Correspondence subject is too long.")
    return subject


def render_versioned_template(
    identifier: str,
    version: str,
    context: dict,
) -> RenderedMessage:
    """Render only a source-controlled template in the explicit registry."""
    try:
        path = TEMPLATES[(identifier, str(version))]
    except KeyError as exc:
        raise ValidationError("Unknown correspondence template or version.") from exc

    raw = render_to_string(path, context)
    subject = ""
    body_lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if not subject and line.strip().startswith("Subject:"):
            subject = line.split("Subject:", 1)[1].strip()
            continue
        body_lines.append(line)
    return RenderedMessage(
        subject=validate_subject(subject),
        body=canonical_body("\n".join(body_lines)),
    )
