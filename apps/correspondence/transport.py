"""In-process Gmail transport helpers for frozen correspondence deliveries."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.utils import formataddr

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.authtoken.models import Token

from apps.configuration import runtime
from apps.records.service_identities import (
    OUTBOUND_PERMISSION,
    SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
    is_exact_service_identity_row,
)

from . import services
from .models import CorrespondenceDeliveryAttempt


def system_transport_principal():
    """Load the migration-provisioned, exact-scope Gmail worker principal."""
    try:
        actor = get_user_model().objects.get(
            username=SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
        )
    except get_user_model().DoesNotExist as exc:
        raise PermissionDenied(
            "The system outbound transport principal is not provisioned."
        ) from exc
    if (
        not is_exact_service_identity_row(actor, OUTBOUND_PERMISSION)
        or Token.objects.filter(user=actor).exists()
    ):
        raise PermissionDenied(
            "The system outbound transport principal does not have exact scope."
        )
    return actor


def delivery_recipient_addresses(claim: services.DeliveryClaim) -> tuple[str, ...]:
    """Return normalized recipient addresses from one frozen send claim."""
    if claim.action != CorrespondenceDeliveryAttempt.Action.SEND:
        return ()
    message_payload = claim.payload.get("message")
    if not isinstance(message_payload, dict):
        raise ValidationError("The delivery claim has no message payload.")
    recipients = message_payload.get("recipients")
    if not isinstance(recipients, dict):
        raise ValidationError("The delivery claim has no recipient snapshot.")
    addresses = []
    for recipient_type in ("to", "cc", "bcc"):
        entries = recipients.get(recipient_type, [])
        if not isinstance(entries, list):
            raise ValidationError("The delivery recipient snapshot is invalid.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("The delivery recipient snapshot is invalid.")
            address = str(entry.get("email", "")).strip()
            if not address:
                raise ValidationError("The delivery recipient snapshot is invalid.")
            addresses.append(address.lower())
    if not addresses:
        raise ValidationError("The delivery claim has no recipients.")
    return tuple(addresses)


def _formatted_recipients(entries) -> list[str]:
    formatted = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("The delivery recipient snapshot is invalid.")
        address = str(entry.get("email", "")).strip()
        display_name = str(entry.get("display_name", "")).strip()
        if not address:
            raise ValidationError("The delivery recipient snapshot is invalid.")
        formatted.append(formataddr((display_name, address)))
    return formatted


def build_delivery_message(
    claim: services.DeliveryClaim,
    *,
    actor,
) -> bytes:
    """Build the exact RFC822 message for a frozen SEND claim."""
    if claim.action != CorrespondenceDeliveryAttempt.Action.SEND:
        raise ValidationError("Only a send claim can be rendered as a message.")
    payload = claim.payload.get("message")
    if not isinstance(payload, dict):
        raise ValidationError("The delivery claim has no message payload.")
    recipients = payload.get("recipients")
    headers = payload.get("headers")
    attachments = payload.get("attachments", [])
    if (
        not isinstance(recipients, dict)
        or not isinstance(headers, dict)
        or not isinstance(attachments, list)
    ):
        raise ValidationError("The delivery claim payload is invalid.")

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = runtime.get("PRR_GMAIL_MAILBOX")
    message["Subject"] = str(payload.get("subject", ""))
    for recipient_type, header_name in (
        ("to", "To"),
        ("cc", "Cc"),
        ("bcc", "Bcc"),
    ):
        values = _formatted_recipients(recipients.get(recipient_type, []))
        if values:
            message[header_name] = values
    message["Message-ID"] = str(headers.get("Message-ID", ""))
    message["Reply-To"] = str(headers.get("Reply-To", ""))
    message.set_content(str(payload.get("body", "")))

    for attachment_payload in attachments:
        if not isinstance(attachment_payload, dict):
            raise ValidationError("The delivery attachment payload is invalid.")
        content_type = str(
            attachment_payload.get("content_type", "application/octet-stream")
        )
        if "/" not in content_type:
            raise ValidationError("The delivery attachment content type is invalid.")
        main_type, sub_type = content_type.split("/", 1)
        opened = services.open_claim_attachment(
            claim.delivery,
            attempt_id=claim.attempt.id,
            attachment_id=attachment_payload.get("id"),
            actor=actor,
        )
        try:
            content = opened.body.read()
        finally:
            opened.body.close()
        message.add_attachment(
            content,
            maintype=main_type,
            subtype=sub_type,
            filename=str(attachment_payload.get("filename", "attachment")),
        )

    return message.as_bytes()
