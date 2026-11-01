"""List, and on request receive, mailbox messages PRR never received.

Dry run by default. It lists every unprocessed message since ``--after`` with
the outcome PRR would most likely give it, and writes nothing. Receiving is
opt-in and per message: ``--commit`` receives only the ``--message-id`` values
named, so an officer decides exactly which old messages become cases.
``--triage-only`` receives them into the staff Inbox without opening a case.

A case opened from an old message keeps the message's real arrival time, so
its statutory deadline may already have passed the moment it exists.
"""

from __future__ import annotations

import datetime as dt
import re
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parseaddr

from django.core.management.base import BaseCommand, CommandError

from apps.configuration import runtime
from apps.correspondence import gmail_sync
from apps.correspondence.models import InboundCorrespondenceReceipt
from apps.integrations.gmail import client as gmail_client
from apps.records.models import PublicRecordsRequest

_AUTOMATED_PRECEDENCE = {"bulk", "list", "junk"}
_CASE_NUMBER = re.compile(r"\b[A-Z]{2,5}-\d{4}-\d{4}\b")


def _headers(raw_bytes: bytes):
    header_bytes = gmail_sync._header_section(raw_bytes)
    if header_bytes is None:
        return None
    return BytesHeaderParser(policy=policy.default).parsebytes(
        header_bytes, headersonly=True
    )


def predict(fetched, *, mailbox: str, known_threads: set, numbers: set) -> str:
    """The outcome PRR would most likely give this message. Advisory only."""
    headers = _headers(fetched.raw_bytes)
    if headers is None:
        return "TRIAGE (headers unreadable)"
    subject = str(headers.get("Subject", ""))
    cited = sorted(set(_CASE_NUMBER.findall(subject)) & numbers)
    if cited or fetched.thread_id in known_threads:
        return "EXISTING CASE? " + (", ".join(cited) or "same thread")
    sender = parseaddr(str(headers.get("From", "")))[1].casefold()
    domain = sender.rpartition("@")[2]
    if any(
        domain == blocked or domain.endswith(f".{blocked}")
        for blocked in runtime.get("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS")
    ):
        return "TRIAGE (blocked sender domain)"
    recipients = gmail_sync._routing_recipients(fetched.raw_bytes)
    if [address.casefold() for address in recipients] != [mailbox]:
        return f"TRIAGE (sent to {', '.join(recipients)})"
    auto = str(headers.get("Auto-Submitted", "")).strip().casefold()
    precedence = {
        value.strip().casefold()
        for value in str(headers.get("Precedence", "")).split(",")
    }
    if (auto and auto != "no") or precedence & _AUTOMATED_PRECEDENCE or (
        headers.get("List-Id") is not None
    ):
        return "TRIAGE (automated or mailing list)"
    return "NEW CASE"


class Command(BaseCommand):
    help = (
        "List mailbox messages PRR never received (dry run), or receive named "
        "ones with --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--after",
            type=dt.date.fromisoformat,
            help="List unprocessed messages after this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--message-id",
            action="append",
            default=[],
            dest="message_ids",
            help="A Gmail message id to receive. Repeat for several.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Receive the named --message-id values. Without it nothing is written.",
        )
        parser.add_argument(
            "--triage-only",
            action="store_true",
            help="Receive into the staff Inbox without opening any case.",
        )

    def handle(self, *args, **options):
        message_ids = list(dict.fromkeys(options["message_ids"]))
        if options["commit"]:
            if not message_ids:
                raise CommandError(
                    "--commit receives only messages named with --message-id."
                )
            result = gmail_sync.catch_up_messages(
                message_ids, open_cases=not options["triage_only"]
            )
            self.stdout.write(str(result))
            return
        if options["triage_only"]:
            raise CommandError("--triage-only applies only with --commit.")
        if not message_ids and options["after"] is None:
            raise CommandError("Name --after or at least one --message-id.")
        self._dry_run(message_ids, after=options["after"])

    def _dry_run(self, message_ids, *, after):
        mailbox = str(runtime.get("PRR_GMAIL_MAILBOX")).strip().casefold()
        if not message_ids:
            message_ids = [
                listed.message_id
                for listed in gmail_client.list_messages(
                    gmail_sync.reconciliation_query(after=after)
                )
            ]
        received = set(
            InboundCorrespondenceReceipt.objects.filter(
                provider="gmail",
                provider_message_id__in=message_ids,
            ).values_list("provider_message_id", flat=True)
        )
        known_threads = set(
            InboundCorrespondenceReceipt.objects.filter(provider="gmail")
            .exclude(provider_thread_id="")
            .values_list("provider_thread_id", flat=True)
        )
        numbers = set(PublicRecordsRequest.objects.values_list("number", flat=True))
        rows = []
        for message_id in message_ids:
            fetched = gmail_client.get_message(message_id)
            if fetched.message_id in received:
                outcome = "ALREADY RECEIVED"
            else:
                outcome = predict(
                    fetched,
                    mailbox=mailbox,
                    known_threads=known_threads,
                    numbers=numbers,
                )
            headers = _headers(fetched.raw_bytes)
            rows.append(
                (
                    fetched.received_at,
                    outcome,
                    fetched.message_id,
                    str(headers.get("From", "")) if headers else "",
                    str(headers.get("Subject", "")) if headers else "",
                )
            )
        rows.sort()
        for received_at, outcome, message_id, sender, subject in rows:
            self.stdout.write(
                f"{outcome} | {received_at:%Y-%m-%d %H:%M} | {message_id} | "
                f"{sender[:60]} | {subject[:90]}"
            )
        self.stdout.write(f"DRY RUN: {len(rows)} message(s); nothing was written.")
