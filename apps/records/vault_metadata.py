# ABOUTME: Read the per-mailbox custodian facts Google already retains in a
# ABOUTME: Vault export's metadata CSV, so one message names every account.
"""Per-mailbox custodians from a Vault export's retained metadata CSV.

Every Google Vault mail export writes a ``<export>-metadata.csv`` alongside
the MBOX/ZIP objects, with one row per *copy* of every exported message and
the columns ``Rfc822MessageId``, ``GmailMessageId``, ``FileName`` and
``Account``. ``Account`` is Google's own answer to "which mailbox had this
message", which is exactly the custodial fact
:class:`~apps.records.models.RecordItemCustodian` exists to record. Until now
the file was retained and never parsed, so a collection task whose
``custodian_emails`` list was empty produced records naming no custodian at
all even though the evidence sat in the export the whole time.

Two rules bound what this module will say:

* **It is descriptive, never a decision.** Parsing a CSV names mailboxes. It
  reviews nothing, releases nothing, communicates with nobody, and changes no
  record's review state.
* **Staff accounts only.** The district's Vault exports sweep student
  accounts as well as staff ones, and a student's mailbox is never attached to
  a public record as a custodian. ``PRR_CUSTODIAN_ACCOUNT_DOMAINS`` is the
  exact-domain list that decides this, deliberately separate from the
  ``external_sender`` tag's domain list so widening one can never silently
  widen the other. Accounts outside it are counted so a run can say how much
  it declined to attach, and never listed by address.

The parser is pure: text in, facts out, no database and no I/O.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from apps.configuration import runtime

from .email_metadata import single_address

#: Google's column names in the export metadata CSV, matched case-insensitively
#: because the header casing is not something this system controls.
MESSAGE_ID_COLUMN = "rfc822messageid"
ACCOUNT_COLUMN = "account"

#: The suffix every Vault export gives its metadata object.
METADATA_CSV_SUFFIX = "-metadata.csv"

#: A hostile or corrupt CSV must not be able to grow without bound in memory.
#: Message-IDs are capped at the stored column width; accounts at the
#: custodian column width.
MAX_MESSAGE_ID = 998
MAX_ACCOUNT = 255


@dataclass(frozen=True)
class MetadataCsv:
    """What one export's metadata CSV says, and what it could not say.

    The issue this implements asks for both a
    ``{message_id: {account}}`` mapping *and* a count of the rows that carry
    no ``Rfc822MessageId`` (ignored, but reported rather than silently
    dropped), so the parse result is a small record of both rather than a bare
    dict.
    """

    #: Message-ID in the stored ``<...>`` form to the lowercased accounts that
    #: held a copy of it. Every account, unfiltered: the domain decision
    #: belongs to :func:`custodian_accounts`, which is where it can be counted.
    accounts: dict[str, set[str]] = field(default_factory=dict)
    #: Rows read, including the ones ignored below.
    rows: int = 0
    #: Rows with a blank or missing ``Rfc822MessageId``. Nothing can be
    #: attached from them; they are counted so a report can say so.
    blank_message_id_rows: int = 0
    #: Rows with a Message-ID but no ``Account``.
    blank_account_rows: int = 0

    @property
    def message_ids(self) -> int:
        return len(self.accounts)

    def merged_with(self, other: MetadataCsv) -> MetadataCsv:
        """This CSV and another read as one: accounts unioned, counts summed.

        The one way several retained metadata objects for one export are
        combined, shared by Vault ingest and the backfill so the two readers
        cannot drift on what a merge is.
        """
        accounts = {
            message_id: set(found_in)
            for message_id, found_in in self.accounts.items()
        }
        for message_id, found_in in other.accounts.items():
            accounts.setdefault(message_id, set()).update(found_in)
        return MetadataCsv(
            accounts=accounts,
            rows=self.rows + other.rows,
            blank_message_id_rows=(
                self.blank_message_id_rows + other.blank_message_id_rows
            ),
            blank_account_rows=self.blank_account_rows + other.blank_account_rows,
        )


def normalize_message_id(value: object) -> str:
    """One Message-ID in the ``<...>`` form :mod:`email_metadata` stores.

    The CSV writes the bracket form inconsistently; the stored column always
    carries whatever the header said, which for real mail is bracketed. Both
    are normalized to the bracketed form so a lookup matches.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if not text:
        return ""
    # Truncate the inside, then wrap: truncating the wrapped form at the
    # stored column width would drop the closing bracket off a Message-ID that
    # sits exactly on the boundary, and the key would then match nothing.
    return f"<{text[: MAX_MESSAGE_ID - 2]}>"


def normalize_account(value: object) -> str:
    """One mailbox address, lowercased, as a custodian is stored."""
    return str(value or "").strip().lower()[:MAX_ACCOUNT]


def parse_metadata_csv(text: str) -> MetadataCsv:
    """Read ``{Message-ID: {account}}`` out of one export metadata CSV.

    Pure and total: a file with unexpected columns yields an empty mapping
    rather than raising, because a CSV this system cannot read must degrade to
    the behaviour that existed before it was read at all, never fail an export
    that is otherwise complete.
    """
    # utf-8-sig content decoded elsewhere can leave the BOM in the first
    # column name, which would hide the Message-ID column.
    reader = csv.DictReader(io.StringIO(str(text or "").lstrip("﻿")))
    if not reader.fieldnames:
        return MetadataCsv()
    columns = {
        str(name or "").strip().lower(): name for name in reader.fieldnames
    }
    message_id_field = columns.get(MESSAGE_ID_COLUMN)
    account_field = columns.get(ACCOUNT_COLUMN)
    if message_id_field is None or account_field is None:
        return MetadataCsv()

    accounts: dict[str, set[str]] = {}
    rows = blank_message_ids = blank_accounts = 0
    for row in reader:
        rows += 1
        message_id = normalize_message_id(row.get(message_id_field))
        if not message_id:
            blank_message_ids += 1
            continue
        account = normalize_account(row.get(account_field))
        if not account:
            blank_accounts += 1
            continue
        accounts.setdefault(message_id, set()).add(account)
    return MetadataCsv(
        accounts=accounts,
        rows=rows,
        blank_message_id_rows=blank_message_ids,
        blank_account_rows=blank_accounts,
    )


class MetadataCsvTooLarge(ValueError):
    """The retained metadata CSV is larger than a caller is willing to hold."""


def read_metadata_csv(source, *, max_bytes: int) -> MetadataCsv:
    """Parse one already-opened, integrity-verified metadata CSV.

    Shared by Vault ingest and the backfill command so the two readers of the
    same retained object cannot diverge on the byte bound, the decoding, or
    what counts as a parse. The caller owns opening and closing ``source``;
    only what happens to the bytes afterwards lives here.
    """
    payload = source.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise MetadataCsvTooLarge(
            "the retained metadata CSV exceeded its byte limit"
        )
    return parse_metadata_csv(payload.decode("utf-8-sig", errors="replace"))


def is_metadata_csv_object(object_name: str) -> bool:
    """Whether this export object is the metadata CSV."""
    return str(object_name or "").strip().lower().endswith(METADATA_CSV_SUFFIX)


def custodian_accounts(accounts) -> tuple[list[str], int]:
    """The accounts that may be attached, and how many were declined.

    Exact domain match against ``PRR_CUSTODIAN_ACCOUNT_DOMAINS``. Everything
    else — student mailboxes above all — is counted and dropped here, before
    any caller can write it. The kept list is sorted so a report and a set of
    custodian rows read the same way twice.
    """
    allowed = set(runtime.get("PRR_CUSTODIAN_ACCOUNT_DOMAINS"))
    kept, declined = [], 0
    for value in accounts or ():
        if not normalize_account(value):
            continue
        # The one definition of "one address" this codebase has. A control
        # character, a space, or a second "@" is a corrupt or hostile cell,
        # and its tail must not carry it past the domain check.
        account = single_address(value)
        if not account:
            declined += 1
            continue
        if account.rpartition("@")[2] in allowed:
            kept.append(account)
        else:
            declined += 1
    return sorted(dict.fromkeys(kept)), declined


def custodian_accounts_for_message(
    metadata: MetadataCsv | None,
    message_id: str,
) -> tuple[list[str], int]:
    """The attachable accounts one Message-ID's CSV rows name."""
    if metadata is None:
        return [], 0
    key = normalize_message_id(message_id)
    if not key:
        return [], 0
    return custodian_accounts(metadata.accounts.get(key, ()))
