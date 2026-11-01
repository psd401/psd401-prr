# ABOUTME: Attach the per-mailbox custodians a Vault export's retained
# ABOUTME: metadata CSV already names, to records ingested before it was read.
"""Backfill per-mailbox custodians from retained Vault export metadata.

Records collected before this system parsed a Vault export's
``<export>-metadata.csv`` name only the mailboxes their collection task
happened to list -- and a task whose ``custodian_emails`` was empty produced
records naming no mailbox at all, which is exactly what happened to
PRR-2026-0052. The evidence was never lost: the CSV is retained with the rest
of the export and says, per copy of every message, which account held it.

This command reads that file and writes what it says, through the same
:func:`apps.records.vault_ingest.attach_custodians` ingest uses, so there is
one way custody is recorded and not two. It is deliberately narrow:

* **Staff accounts only.** ``PRR_CUSTODIAN_ACCOUNT_DOMAINS`` decides, by exact
  domain. Accounts outside it -- student mailboxes above all -- are counted in
  the report and never written, never listed by address.
* **Nothing but custody.** No review state is read as a decision or changed,
  nothing is released, denied, withheld or communicated. A record marked
  duplicate is skipped rather than revived.
* **Never automatic, and never anonymous.** No background task may invoke
  this command; it is a human operation, AST-guarded in ``test_compliance``,
  and ``--run-by`` names the reviewer or Records Officer whose run it is so
  the audit row it writes is attributable, exactly as
  ``fold_duplicate_record_copies`` and ``attest_external_initial_responses``
  attribute theirs.
* **Dry run by default.** ``--commit`` writes. The dry run reports exactly the
  counts a commit would produce within one run. Between a dry run and a later
  commit a record may have been marked duplicate; each write re-reads its
  record under a lock and reports such a record as ``FAILED ... changed since
  planning`` rather than reviving it.

Records the CSV alone cannot place are resolved from their own retained raw
message before anything is named and skipped:

* a Message-ID the export's CSV does not carry -- the CSV cannot say where a
  message it never listed was found, and that record is named and skipped;
* a Message-ID that several live records of one request share -- these are the
  pre-deduplication per-mailbox copies of a personalized bulk mailing, and the
  CSV, keyed on Message-ID alone, names every account that held *any* copy.
  Attaching that set to each copy would name a hundred mailboxes on a hundred
  different records. Each copy's own ``Delivered-To`` header names the one
  mailbox it was delivered into, so each copy is resolved from its own
  retained raw message: digest-verified read, exactly one ``Delivered-To``,
  staff domain filter, ``attach_custodians``. A copy with no retained message,
  with no single ``Delivered-To``, or whose mailbox is outside the configured
  domains is named and skipped, and counted.
"""

from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.api.identity import StaffRole, has_staff_role
from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.email_metadata import parse_delivered_to
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    VaultExportObject,
    VaultMessageArtifact,
)
from apps.records.storage import load_verified_object
from apps.records.vault_ingest import attach_custodians, safe_error_detail
from apps.records.vault_metadata import (
    custodian_accounts,
    custodian_accounts_for_message,
    is_metadata_csv_object,
    normalize_message_id,
    read_metadata_csv,
)


class AttachStale(Exception):
    """The record changed between planning and writing."""

SKIP_NO_METADATA_CSV = "no retained metadata CSV for this task's export"
SKIP_METADATA_CSV_UNREADABLE = (
    "this task's retained metadata CSV could not be read; the detail is on "
    "stderr"
)
SKIP_METADATA_CSV_EMPTY = (
    "this task's retained metadata CSV names no message; nothing to attach"
)
#: What ``_metadata_for`` found, and the report line each outcome earns. The
#: ingest run's own ``custodian_metadata`` snapshot distinguishes the same four
#: states; an operator reading this report should not have to go and look it
#: up to tell "no CSV was retained" from "the CSV was retained and unreadable".
TASK_SKIP_REASONS = {
    "absent": SKIP_NO_METADATA_CSV,
    "unreadable": SKIP_METADATA_CSV_UNREADABLE,
    "empty": SKIP_METADATA_CSV_EMPTY,
}
SKIP_UNMATCHED = "Message-ID absent from this export's metadata CSV"
SKIP_NO_STAFF_ACCOUNTS = "the CSV names no account in the configured domains"
#: The three ways a shared-Message-ID copy cannot name its own mailbox. Each
#: is reported per record and counted, because each needs a different answer:
#: a missing artifact is a retention question, a missing header is a fact
#: about the message, and an outside-domain mailbox is the staff-only rule
#: working as intended.
SKIP_NO_RETAINED_MESSAGE = (
    "several live records share this Message-ID and this copy has no "
    "retained raw message, so its own mailbox cannot be read"
)
SKIP_RETAINED_MESSAGE_UNREADABLE = (
    "several live records share this Message-ID and this copy's retained raw "
    "message could not be read; the detail is on stderr"
)
SKIP_DELIVERED_TO_MISSING = (
    "several live records share this Message-ID and this copy carries no "
    "single Delivered-To header naming its mailbox"
)
SKIP_DELIVERED_TO_OUTSIDE_DOMAINS = (
    "this copy's Delivered-To mailbox is outside the configured domains"
)
#: Every per-record skip line the report accounts for, in reading order.
SKIP_DELIVERED_TO_UNCORROBORATED = (
    "this copy's Delivered-To mailbox is not one the export's metadata CSV "
    "names for this message"
)
SKIP_REASONS = (
    SKIP_UNMATCHED,
    SKIP_NO_STAFF_ACCOUNTS,
    SKIP_NO_RETAINED_MESSAGE,
    SKIP_RETAINED_MESSAGE_UNREADABLE,
    SKIP_DELIVERED_TO_MISSING,
    SKIP_DELIVERED_TO_UNCORROBORATED,
    SKIP_DELIVERED_TO_OUTSIDE_DOMAINS,
)


class Command(BaseCommand):
    help = (
        "Attach per-mailbox custodians from retained Vault export metadata "
        "CSVs to the records that were ingested without them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-by",
            dest="run_by",
            required=True,
            help=(
                "Username of the reviewer or Records Officer whose run this "
                "is. The audit row is attributed to them."
            ),
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Write the custodians. Without this the command only reports.",
        )
        parser.add_argument(
            "--request",
            dest="request_number",
            default="",
            help="Limit the pass to one request number.",
        )

    # -- reading ----------------------------------------------------------

    def _tasks(self, request_number: str):
        tasks = (
            CollectionTask.objects.filter(
                kind=CollectionTask.Kind.VAULT_MAIL,
                vault_ingest_run__isnull=False,
            )
            .select_related("request")
            .order_by("pk")
        )
        if request_number:
            tasks = tasks.filter(request__number=request_number)
        return tasks

    def _metadata_for(self, task):
        """``(metadata, status)`` for this task's retained metadata CSV.

        ``status`` is ``absent`` (no metadata object was retained at all),
        ``unreadable`` (one was, and none of them could be read or parsed),
        ``empty`` (read, but naming no message) or ``parsed``. The three
        failures read differently in the report because they need different
        answers from an operator.
        """
        objects = (
            VaultExportObject.objects.filter(
                ingest_run__collection_task=task,
            )
            .exclude(storage_key="")
            .order_by("pk")
        )
        combined = None
        seen = False
        # Read once: this pass reads every retained metadata object against
        # the same ceiling, and a store read per object would be a cache
        # round trip per object.
        object_limit = runtime.get("PRR_VAULT_EXPORT_MAX_BYTES")
        for export_object in objects:
            if not is_metadata_csv_object(export_object.object_name):
                continue
            seen = True
            try:
                source = load_verified_object(
                    export_object.storage_key,
                    expected_size=export_object.size or 0,
                    expected_sha256=export_object.sha256,
                )
                try:
                    # The same reader Vault ingest uses, so the two callers
                    # cannot diverge on the byte bound, the decoding, or what
                    # counts as a parse.
                    parsed = read_metadata_csv(
                        source,
                        max_bytes=object_limit,
                    )
                finally:
                    source.close()
            except Exception as exc:  # noqa: BLE001 - reported, never guessed
                # One unreadable CSV is one task's problem, not the pass's.
                self.stderr.write(
                    f"{task.pk} {task.request.number}: metadata CSV unusable "
                    # The same bounded, control-character-stripped rendering
                    # ingest uses, so an error carrying hostile or unbounded
                    # text cannot reach an operator's terminal raw.
                    f"({type(exc).__name__}: {safe_error_detail(exc)})"
                )
                continue
            # The same merge ingest uses, so two readers of one retained
            # object cannot drift on what a merge is.
            combined = parsed if combined is None else combined.merged_with(parsed)
        if not seen:
            return None, "absent"
        if combined is None:
            return None, "unreadable"
        if not combined.accounts:
            return None, "empty"
        return combined, "parsed"

    def _delivered_to_for(self, record):
        """``(address, status)`` for this copy's own mailbox.

        Read from the copy's own retained raw message -- the artifact that
        created this record -- through the same digest-verified loader the
        metadata CSV uses, so a retained object that was altered or truncated
        names nothing rather than something. ``status`` is ``absent`` (no
        retained raw message to read), ``unreadable`` (there is one and it
        could not be read) or ``read``; an empty address with ``read`` means
        the message carries no single usable ``Delivered-To``.
        """
        artifact = (
            VaultMessageArtifact.objects.filter(record_item=record)
            .exclude(raw_storage_key="")
            .order_by("pk")
            .first()
        )
        if artifact is None or not (artifact.raw_size or 0):
            return "", "absent"
        if artifact.raw_size > runtime.get("PRR_VAULT_MESSAGE_MAX_BYTES"):
            # The same bound ingest retains under. A retained object larger
            # than it is not one this command will pull into memory to read
            # one header from.
            self.stderr.write(
                f"{record.pk} {record.request.number}: retained message "
                "exceeds the configured message byte limit"
            )
            return "", "unreadable"
        try:
            source = load_verified_object(
                artifact.raw_storage_key,
                expected_size=artifact.raw_size,
                expected_sha256=artifact.raw_sha256,
            )
            try:
                raw_message = source.read()
            finally:
                source.close()
            return parse_delivered_to(raw_message), "read"
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            # One unreadable message is one record's problem, not the pass's.
            self.stderr.write(
                f"{record.pk} {record.request.number}: retained message "
                # The same bounded, control-character-stripped rendering
                # ingest uses, so an error carrying hostile or unbounded text
                # cannot reach an operator's terminal raw.
                f"unusable ({type(exc).__name__}: {safe_error_detail(exc)})"
            )
            return "", "unreadable"

    def _live_records(self, task):
        """Every reviewable record this task collected that names a message."""
        return (
            RecordItem.objects.filter(collection_task=task)
            .exclude(review_state=RecordItem.ReviewState.DUPLICATE)
            .exclude(email_message_id="")
            .select_related("request")
            .order_by("created_at", "pk")
        )

    # -- planning ---------------------------------------------------------

    def _shared_message_ids(self, request_ids):
        """How many live records of each request carry each Message-ID.

        A Message-ID held by more than one live record of the *request* is a
        pre-deduplication copy pair. Counted across the request, not the task,
        because the copies were collected by different tasks -- and cached per
        request, because a request with several Vault-mail tasks would
        otherwise re-read the same record set once per task. Planning writes
        nothing, so the answer cannot change under the cache within a run.
        """
        missing = {
            request_id
            for request_id in request_ids
            if request_id not in self._shared_cache
        }
        if missing:
            counts = Counter(
                RecordItem.objects.filter(request_id__in=missing)
                .exclude(review_state=RecordItem.ReviewState.DUPLICATE)
                .exclude(email_message_id="")
                .values_list("request_id", "email_message_id")
            )
            for request_id in missing:
                self._shared_cache[request_id] = Counter()
            for (request_id, message_id), count in counts.items():
                self._shared_cache[request_id][message_id] = count
        return self._shared_cache

    def _plan_task(self, task, metadata, counters, attachments):
        """Decide every record of one task. Reads only; writes nothing."""
        records = list(self._live_records(task))
        shared = self._shared_message_ids(
            {record.request_id for record in records},
        )

        for record in records:
            counters["examined"] += 1
            if shared[record.request_id][record.email_message_id] > 1:
                # Several live copies of one message. The CSV's account set
                # describes all of them at once, so only this copy's own
                # Delivered-To can place this copy.
                self._plan_shared_copy(
                    record, task, metadata, counters, attachments,
                )
                continue
            if normalize_message_id(record.email_message_id) not in metadata.accounts:
                self._skip(record, SKIP_UNMATCHED, counters)
                continue
            # The same lookup ingest makes for a message it is processing.
            keep, declined = custodian_accounts_for_message(
                metadata, record.email_message_id,
            )
            counters["accounts_outside_domains"] += declined
            if not keep:
                self._skip(record, SKIP_NO_STAFF_ACCOUNTS, counters)
                continue
            attachments.append((record, task, keep))
            counters["attached"] += 1

    def _plan_shared_copy(self, record, task, metadata, counters, attachments):
        """Place one copy of a message several live records share.

        Its own ``Delivered-To`` or nothing: the CSV set is not attachable
        here, and no other header is a delivery fact.
        """
        delivered_to, status = self._delivered_to_for(record)
        if status == "absent":
            self._skip(record, SKIP_NO_RETAINED_MESSAGE, counters)
            return
        if status == "unreadable":
            self._skip(record, SKIP_RETAINED_MESSAGE_UNREADABLE, counters)
            return
        if not delivered_to:
            self._skip(record, SKIP_DELIVERED_TO_MISSING, counters)
            return
        # The same domain filter every other write goes through, applied here
        # so the mailbox can be counted before it is declined -- and so the
        # address itself never reaches the report.
        keep, declined = custodian_accounts([delivered_to])
        if not keep:
            counters["delivered_to_outside_domains"] += declined
            self._skip(record, SKIP_DELIVERED_TO_OUTSIDE_DOMAINS, counters)
            return
        # Delivered-To is a header a sender can write. The export's own
        # metadata names the mailboxes that held this message; a staff
        # mailbox the CSV never saw is not corroborated and is not written.
        known = metadata.accounts.get(
            normalize_message_id(record.email_message_id), (),
        )
        if known and delivered_to not in known:
            self._skip(record, SKIP_DELIVERED_TO_UNCORROBORATED, counters)
            return
        attachments.append((record, task, keep))
        counters["attached"] += 1
        counters["resolved_by_delivered_to"] += 1

    def _skip(self, record, reason, counters):
        counters["skipped"] += 1
        counters[f"skipped:{reason}"] += 1
        self.stdout.write(
            f"    skip {record.pk} {record.request.number}: {reason}"
        )

    # -- writing ----------------------------------------------------------

    def handle(self, *args, **options):
        officer = (
            get_user_model()
            .objects.filter(username=options["run_by"])
            .first()
        )
        if officer is None:
            raise CommandError(f"No user named {options['run_by']!r}.")
        if not has_staff_role(
            officer, StaffRole.REVIEWER, StaffRole.RECORDS_OFFICER,
        ):
            raise CommandError(
                f"{officer.username!r} does not hold a review role; a "
                "reviewer or Records Officer must own this run."
            )
        commit = bool(options["commit"])
        request_number = (options["request_number"] or "").strip()
        if request_number and not PublicRecordsRequest.objects.filter(
            number=request_number,
        ).exists():
            # A mistyped number would otherwise report a clean zero.
            raise CommandError(f"No request numbered {request_number!r}.")

        self.stdout.write(f"Run by: {officer.username}")
        self._shared_cache = {}
        counters = Counter()
        attachments = []
        for task in self._tasks(request_number):
            counters["tasks"] += 1
            metadata, status = self._metadata_for(task)
            if metadata is None:
                counters["tasks_without_metadata_csv"] += 1
                counters[f"tasks_metadata_{status}"] += 1
                self.stdout.write(
                    f"    skip task {task.pk} {task.request.number}: "
                    f"{TASK_SKIP_REASONS[status]}"
                )
                continue
            counters["tasks_with_metadata_csv"] += 1
            self._plan_task(task, metadata, counters, attachments)

        for record, _task, keep in attachments:
            self.stdout.write(
                f"    attach {record.pk} {record.request.number} "
                f"{len(keep)} account(s)"
            )

        self.stdout.write("Vault custodian backfill")
        self.stdout.write(f"  tasks : {counters['tasks']}")
        self.stdout.write(
            f"  tasks with a retained CSV : "
            f"{counters['tasks_with_metadata_csv']}"
        )
        self.stdout.write(
            f"  tasks without a usable CSV : "
            f"{counters['tasks_without_metadata_csv']}"
        )
        for status, reason in TASK_SKIP_REASONS.items():
            self.stdout.write(
                f"    {reason} : {counters[f'tasks_metadata_{status}']}"
            )
        self.stdout.write(f"  records examined : {counters['examined']}")
        self.stdout.write(
            ("  to attach : " if commit else "  would attach : ")
            + str(counters["attached"])
        )
        self.stdout.write(
            "  resolved from this copy's Delivered-To : "
            f"{counters['resolved_by_delivered_to']}"
        )
        self.stdout.write(f"  skipped : {counters['skipped']}")
        for reason in SKIP_REASONS:
            self.stdout.write(
                f"    {reason} : {counters[f'skipped:{reason}']}"
            )
        self.stdout.write(
            "  accounts outside the configured domains : "
            f"{counters['accounts_outside_domains']}"
        )
        self.stdout.write(
            "  Delivered-To mailboxes outside the configured domains : "
            f"{counters['delivered_to_outside_domains']}"
        )

        if not commit:
            self.stdout.write(
                "Dry run; nothing written. Add --commit to attach these "
                "custodians."
            )
            return

        attached = failed = 0
        for record, task, keep in attachments:
            try:
                with transaction.atomic():
                    # Planning read the queue once. A record marked duplicate
                    # since -- by a reviewer, or by the fold command -- is
                    # re-read under a lock and left alone rather than revived
                    # with custodians it no longer needs; the run says so.
                    current = (
                        RecordItem.objects.select_for_update().get(pk=record.pk)
                    )
                    if current.review_state == RecordItem.ReviewState.DUPLICATE:
                        raise AttachStale(
                            "changed since planning: now marked duplicate"
                        )
                    attach_custodians(current, task, keep)
            except Exception as exc:  # noqa: BLE001 - counted, pass goes on
                # One record that cannot be written is reported and counted;
                # each attach is its own transaction, so what was already
                # written stands and the summary still says what happened.
                failed += 1
                self.stderr.write(f"    FAILED {record.pk}: {exc}")
                continue
            attached += 1
        audit(
            "record.custodians_backfilled",
            actor=officer,
            tasks=counters["tasks"],
            tasks_with_metadata_csv=counters["tasks_with_metadata_csv"],
            tasks_without_metadata_csv=counters["tasks_without_metadata_csv"],
            tasks_metadata_absent=counters["tasks_metadata_absent"],
            tasks_metadata_unreadable=counters["tasks_metadata_unreadable"],
            tasks_metadata_empty=counters["tasks_metadata_empty"],
            examined=counters["examined"],
            attached=attached,
            failed=failed,
            skipped=counters["skipped"],
            skipped_unmatched=counters[f"skipped:{SKIP_UNMATCHED}"],
            skipped_no_staff_accounts=counters[
                f"skipped:{SKIP_NO_STAFF_ACCOUNTS}"
            ],
            resolved_by_delivered_to=counters["resolved_by_delivered_to"],
            skipped_no_retained_message=counters[
                f"skipped:{SKIP_NO_RETAINED_MESSAGE}"
            ],
            skipped_retained_message_unreadable=counters[
                f"skipped:{SKIP_RETAINED_MESSAGE_UNREADABLE}"
            ],
            skipped_delivered_to_uncorroborated=counters[
                f"skipped:{SKIP_DELIVERED_TO_UNCORROBORATED}"
            ],
            delivered_to_missing=counters[
                f"skipped:{SKIP_DELIVERED_TO_MISSING}"
            ],
            delivered_to_outside_domains=counters[
                "delivered_to_outside_domains"
            ],
            accounts_outside_domains=counters["accounts_outside_domains"],
            request_number=request_number,
        )
        self.stdout.write(f"  attached : {attached}")
        self.stdout.write(f"  failed : {failed}")
        self.stdout.write(
            self.style.SUCCESS(f"Attached {attached}; failed {failed}.")
        )
