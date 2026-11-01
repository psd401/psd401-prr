"""Tell the records team when inbound mail has waited too long for triage."""

from __future__ import annotations

import datetime as dt
from collections import Counter

from django.utils import timezone

from apps.configuration import runtime
from apps.correspondence.models import InboundCorrespondenceReceipt
from apps.records import notifications
from apps.records.deadlines import business_days_between

from .models import WorkItem

#: Inbound mail a person has to look at before anything happens to it: no
#: case was opened and no case was matched with certainty.
TRIAGE_KINDS = (
    WorkItem.WorkKind.INBOUND_UNMATCHED,
    WorkItem.WorkKind.INBOUND_CANDIDATE,
    WorkItem.WorkKind.INBOUND_AMBIGUOUS,
    WorkItem.WorkKind.INBOUND_QUARANTINED,
)


def _secondary_address(receipt, mailbox: str) -> str:
    """The address a message went to when it was not the intake mailbox."""
    for address in receipt.envelope_recipients or ():
        normalized = str(address).strip().casefold()
        if normalized and normalized != mailbox:
            return normalized
    return ""


def alert_waiting_inbound_triage(*, today: dt.date | None = None) -> dict:
    """Send one daily alert naming how much inbound mail is still untriaged."""
    today = today or timezone.localdate()
    threshold = runtime.get("PRR_INBOX_TRIAGE_ALERT_BUSINESS_DAYS")
    mailbox = str(runtime.get("PRR_GMAIL_MAILBOX")).strip().casefold()
    items = list(
        WorkItem.objects.filter(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            work_kind__in=TRIAGE_KINDS,
            disposition=WorkItem.Disposition.NEEDS_ATTENTION,
        ).values_list("source_id", "work_kind")
    )
    receipts = {
        str(receipt.pk): receipt
        for receipt in InboundCorrespondenceReceipt.objects.filter(
            pk__in=[source_id for source_id, _kind in items]
        ).only("pk", "received_at", "envelope_recipients")
    }
    by_kind = Counter()
    by_address = Counter()
    oldest = None
    for source_id, kind in items:
        receipt = receipts.get(source_id)
        if receipt is None:
            continue
        received_on = timezone.localdate(receipt.received_at)
        if business_days_between(received_on, today) < threshold:
            continue
        by_kind[WorkItem.WorkKind(kind).label] += 1
        address = _secondary_address(receipt, mailbox)
        if address:
            by_address[address] += 1
        if oldest is None or received_on < oldest:
            oldest = received_on
    waiting = sum(by_kind.values())
    if not waiting:
        return {"status": "ok", "waiting": 0, "alerted": False}
    sent = notifications.notify_inbound_mailbox(
        signal_type="inbound.triage_waiting",
        subject=(
            f"[INBOX TRIAGE] {waiting} inbound message(s) waiting more than "
            f"{threshold} business day(s)"
        ),
        template_name="notifications/inbound_triage_waiting.txt",
        context={
            "waiting": waiting,
            "threshold": threshold,
            "oldest": oldest,
            "by_kind": sorted(by_kind.items()),
            "by_address": sorted(by_address.items()),
        },
        condition_key=f"{today.isoformat()}:{waiting}",
    )
    return {"status": "ok", "waiting": waiting, "alerted": bool(sent)}
