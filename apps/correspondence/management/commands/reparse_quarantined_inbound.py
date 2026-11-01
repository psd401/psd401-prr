"""List, and on request re-parse, mailbox messages the parser quarantined whole.

Dry run by default: it re-reads every whole-message quarantine's retained raw
message with the current parser and says what would happen, writing nothing.
``--commit`` re-parses the named ``--receipt`` values (or every one with
``--all``). A released receipt joins the normal path: it is matched to its
case, or, when it is an eligible new request, it opens one with the message's
real arrival time, so its statutory deadline may already be close or past.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesHeaderParser

from django.core.management.base import BaseCommand, CommandError

from apps.configuration import runtime
from apps.correspondence import gmail_sync, receiving
from apps.correspondence.mime import InboundMimeSecurityError, parse_inbound_message
from apps.correspondence.models import InboundCorrespondenceReceipt
from apps.records import email_forwards


def quarantined_receipts():
    return InboundCorrespondenceReceipt.objects.filter(
        provider="gmail",
        provider_account=runtime.get("PRR_GMAIL_MAILBOX"),
        parsing_status__in=[
            InboundCorrespondenceReceipt.ParsingStatus.FAILED,
            InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
        ],
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
        matched_correspondence__isnull=True,
    ).order_by("received_at", "id")


def _sender_and_subject(raw_bytes: bytes) -> tuple[str, str]:
    header_bytes = gmail_sync._header_section(raw_bytes)
    if header_bytes is None:
        return "", ""
    headers = BytesHeaderParser(policy=policy.compat32).parsebytes(
        header_bytes, headersonly=True
    )
    return str(headers.get("From", "")), str(headers.get("Subject", ""))


class Command(BaseCommand):
    help = (
        "List whole-message inbound quarantines and what the current parser "
        "would do (dry run), or re-parse them with --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--receipt",
            action="append",
            default=[],
            dest="receipt_ids",
            help="An inbound receipt id to re-parse. Repeat for several.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="With --commit, re-parse every whole-message quarantine.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Re-parse. Without it nothing is written.",
        )

    def handle(self, *args, **options):
        receipts = quarantined_receipts()
        receipt_ids = list(dict.fromkeys(options["receipt_ids"]))
        if receipt_ids:
            receipts = receipts.filter(pk__in=receipt_ids)
        if options["commit"]:
            if not receipt_ids and not options["all"]:
                raise CommandError("--commit needs --receipt or --all.")
            self._commit(list(receipts))
            return
        self._dry_run(list(receipts))

    def _dry_run(self, receipts):
        limits = receiving._mime_limits()
        for receipt in receipts:
            raw_bytes = receiving.verified_raw_message(receipt)
            if raw_bytes is None:
                outcome, sender, subject = "EVIDENCE MISMATCH", "", ""
            else:
                sender, subject = _sender_and_subject(raw_bytes)
                try:
                    parsed = parse_inbound_message(raw_bytes, limits=limits)
                except InboundMimeSecurityError as exc:
                    outcome = f"STILL QUARANTINED ({exc.code})"
                else:
                    outcome = "WOULD RELEASE"
                    origin = email_forwards.original_sender(
                        parsed.subject, parsed.body
                    )
                    if origin is not None:
                        sent = (
                            f"{origin.sent_at:%Y-%m-%d %H:%M %Z}"
                            if origin.sent_at
                            else "date unreadable"
                        )
                        outcome += (
                            f" (forward of {origin.name} <{origin.email}>, "
                            f"sent {sent})"
                        )
            self.stdout.write(
                f"{outcome} | {receipt.received_at:%Y-%m-%d %H:%M} | "
                f"{receipt.pk} | {','.join(receipt.envelope_recipients)} | "
                f"{sender[:60]} | {subject[:80]}"
            )
        self.stdout.write(
            f"DRY RUN: {len(receipts)} quarantined receipt(s); nothing was written."
        )

    def _commit(self, receipts):
        from apps.records.email_intake import create_request_from_unmatched_email

        actor = gmail_sync.system_inbound_principal()
        counts: dict[str, int] = {}
        for receipt in receipts:
            result = receiving.reparse_quarantined_receipt(receipt.pk, actor=actor)
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
            line = f"{result.outcome} | {receipt.pk}"
            if result.reason:
                line += f" | {result.reason}"
            if result.outcome == "released":
                released = InboundCorrespondenceReceipt.objects.get(pk=receipt.pk)
                request = create_request_from_unmatched_email(released, actor=actor)
                released.refresh_from_db()
                case = request or released.matched_request or released.candidate_request
                line += (
                    f" | {released.matching_status}"
                    f" | {getattr(case, 'number', '') or '-'}"
                )
            self.stdout.write(line)
        self.stdout.write(f"COMMITTED: {counts}")
