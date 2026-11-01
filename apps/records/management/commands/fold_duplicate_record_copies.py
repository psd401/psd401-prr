# ABOUTME: Fold pre-deduplication per-mailbox copies of one message into the
# ABOUTME: record that survived, on a named Records Officer's decision.
"""Fold the copies the metadata backfill could not fill in.

``backfill_record_email_metadata`` leaves a record unchanged when another
record of the same request already carries the same Message-ID *and* the same
body digest. Those are per-mailbox copies of one message that the pre-
deduplication ingest filed as separate records: today they sit in the
unreviewed queue as N copies of one public record, and the record that does
carry the message names only the mailbox it happened to be ingested from.

This pass resolves that, one copy at a time and in the name of a Records
Officer given on the command line:

* the copy's retained raw message is re-read with the same digest-verified
  loader the backfill uses, so the key is computed from the bytes rather than
  inferred;
* the surviving record — the one in that request already carrying that
  Message-ID and body digest — gains the copy's custodians, so it names every
  mailbox the message was found in;
* the copy is marked ``DUPLICATE`` through :func:`apps.records.review.review_record`,
  the same per-record service the reviewer's own control uses, which locks the
  record, refuses active redactions, clears second review and derived
  artifacts, and writes ``record.reviewed`` with the previous state;
* the copy's retained Vault message is re-pointed at the survivor exactly as
  ingest marks a duplicate at arrival, so a later byte-identical collection
  of the same message attaches its custodian to the record under review and
  not to the copy that was set aside.

Nothing here communicates, releases, denies, or withholds. It records a
housekeeping judgement about which copy is the record, and every step appends
an audit row. Any copy that already carries a human review decision is
skipped, not folded: a person's judgement about a record is not something a
backfill overturns. That holds at the moment of writing, not only at planning:
every fold re-reads the copy and the survivor under a row lock and steps back
if either changed in between, so a reviewer working the queue while this runs
is never overruled. No background task may invoke this command; it is a human
operation, AST-guarded in ``test_compliance``-style tests.

Dry run by default; ``--commit`` writes. Within one run the dry-run decision
for every copy is the decision ``--commit`` would write; between two runs a
reviewer may have decided a copy, and such a fold is reported as stale rather
than forced.
"""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.api.identity import StaffRole, has_staff_role
from apps.audit.models import audit
from apps.records.email_metadata import parse_email_metadata
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    VaultMessageArtifact,
)
from apps.records.review import review_record
from apps.records.storage import load_verified_object
from apps.records.vault_ingest import _mark_duplicate, attach_custodians


class FoldStale(Exception):
    """The copy or its survivor changed between planning and writing."""


@dataclass(frozen=True)
class Fold:
    """One copy, the record it folds into, and where the copy was found."""

    copy: RecordItem
    survivor: RecordItem
    task: CollectionTask | None
    message_id: str
    body_sha256: str


class Command(BaseCommand):
    help = (
        "Fold pre-deduplication custodian copies into their surviving record "
        "and mark them duplicate, on a Records Officer's decision."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reviewed-by",
            dest="reviewed_by",
            required=True,
            help=(
                "Username of the Records Officer or reviewer whose decision "
                "this is."
            ),
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Record the folds. Without this the command only reports.",
        )
        parser.add_argument(
            "--request",
            dest="request_number",
            default="",
            help="Limit the pass to one request number.",
        )

    # -- planning ---------------------------------------------------------

    def _candidates(self, request_number: str):
        """Every record that has no Message-ID but does retain its bytes.

        These are exactly the records the metadata backfill left unchanged:
        it only ever skips a record whose key another record already holds.
        """
        artifacts = (
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.CREATED,
                record_item__isnull=False,
                record_item__email_message_id="",
            )
            .exclude(raw_storage_key="")
            .select_related(
                "record_item",
                "record_item__request",
                "record_item__collection_task",
                "export_object__ingest_run__collection_task",
            )
            .order_by("pk")
        )
        if request_number:
            artifacts = artifacts.filter(
                record_item__request__number=request_number,
            )
        return artifacts

    def _identity(self, artifact) -> tuple[str, str]:
        """The copy's ``(Message-ID, body digest)`` read from its own bytes."""
        source = load_verified_object(
            artifact.raw_storage_key,
            expected_size=artifact.raw_size or 0,
            expected_sha256=artifact.raw_sha256,
        )
        try:
            raw_message = source.read()
        finally:
            source.close()
        metadata = parse_email_metadata(raw_message)
        return metadata.message_id, metadata.body_sha256

    def _plan(self, artifacts):
        """Decide every copy's outcome. Reads only; writes nothing."""
        folds, skipped, unreadable = [], [], 0
        planned = set()
        for artifact in artifacts.iterator():
            copy = artifact.record_item
            if copy.pk in planned:
                # Nothing stops a record from having two retained artifacts.
                # One record is one decision, not one per artifact.
                continue
            planned.add(copy.pk)
            try:
                message_id, body_sha256 = self._identity(artifact)
            except Exception as exc:  # noqa: BLE001 - reported, never guessed
                # One unreadable or unparseable historical message must not
                # abort the pass and strand every later copy. The message is
                # kept so a real defect reads differently from a bad object.
                unreadable += 1
                self.stderr.write(
                    f"{copy.pk}: retained message unusable "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            reason = self._ineligible(copy, message_id, body_sha256)
            if reason is not None:
                skipped.append((copy, reason))
                continue
            survivor, survivor_reason = self._survivor(
                copy, message_id, body_sha256,
            )
            if survivor is None:
                skipped.append((copy, survivor_reason))
                continue
            folds.append(
                Fold(
                    copy=copy,
                    survivor=survivor,
                    task=self._task_for(copy, artifact),
                    message_id=message_id,
                    body_sha256=body_sha256,
                )
            )
        return folds, skipped, unreadable

    def _ineligible(self, copy, message_id: str, body_sha256: str) -> str | None:
        """Why this copy must not fold, or ``None`` when it may."""
        if not message_id or not body_sha256:
            # Half an identity is exactly what ingest refuses to deduplicate
            # on: a sender-supplied Message-ID with no body to corroborate it.
            # An attachment-only message therefore never folds.
            return "no body digest to corroborate the Message-ID"
        if copy.review_state != RecordItem.ReviewState.UNREVIEWED:
            return (
                "already reviewed as "
                f"{copy.get_review_state_display()}; a human decision is not "
                "overturned here"
            )
        if copy.redactions.filter(voided_at__isnull=True).exists():
            return "carries an active redaction"
        if copy.installments.exists():
            return "already released in an installment"
        return None

    def _survivor(
        self, copy, message_id: str, body_sha256: str,
    ) -> tuple[RecordItem | None, str]:
        """The record of this request that already carries this message."""
        survivor = (
            RecordItem.objects.filter(
                request_id=copy.request_id,
                email_message_id=message_id,
                email_body_sha256=body_sha256,
            )
            .exclude(pk=copy.pk)
            .order_by("created_at", "pk")
            .first()
        )
        if survivor is None:
            return None, "no surviving record of this request carries it"
        if survivor.review_state == RecordItem.ReviewState.DUPLICATE:
            # Folding into a record a human already set aside would leave the
            # request with no copy of this message anyone is reviewing.
            return None, (
                f"the surviving record {survivor.pk} is itself marked "
                "duplicate"
            )
        return survivor, ""

    def _task_for(self, copy, artifact) -> CollectionTask | None:
        """The collection task that found this copy, if it is still on record."""
        if copy.collection_task_id:
            return copy.collection_task
        return getattr(
            getattr(artifact.export_object, "ingest_run", None),
            "collection_task",
            None,
        )

    # -- execution --------------------------------------------------------

    def _fold(self, fold: Fold, *, officer):
        with transaction.atomic():
            # Planning read the queue once; a reviewer may have decided this
            # copy since, or the survivor may have moved. Both are re-read
            # under a lock and the fold steps back rather than overruling
            # anyone. The survivor is re-read for its own sake too: two copies
            # folding into one survivor in one run must each see what the
            # previous fold wrote, not a stale plan-time instance.
            copy = RecordItem.objects.select_for_update().get(pk=fold.copy.pk)
            reason = self._ineligible(copy, fold.message_id, fold.body_sha256)
            if reason is not None:
                raise FoldStale(f"changed since planning: {reason}")
            survivor = (
                RecordItem.objects.select_for_update().get(pk=fold.survivor.pk)
            )
            if (
                survivor.review_state == RecordItem.ReviewState.DUPLICATE
                or survivor.email_message_id != fold.message_id
                or survivor.email_body_sha256 != fold.body_sha256
            ):
                raise FoldStale(
                    "changed since planning: the surviving record "
                    f"{survivor.pk} no longer carries this message as a "
                    "reviewable record"
                )
            if fold.task is not None:
                attach_custodians(survivor, fold.task)
            review_record(
                copy.pk,
                state=RecordItem.ReviewState.DUPLICATE,
                actor=officer,
            )
            # Ingest's byte-hash deduplication only ever looks at artifacts
            # still marked created-with-a-record. Left as it was, the copy's
            # artifact would keep claiming to be the record for these bytes,
            # and the next mailbox to yield the identical message would have
            # its custodian attached to the copy nobody is reviewing. The same
            # helper ingest uses re-points it at the survivor.
            for artifact in VaultMessageArtifact.objects.filter(
                record_item=copy,
                outcome=VaultMessageArtifact.Outcome.CREATED,
            ).order_by("pk"):
                _mark_duplicate(artifact, survivor)
            audit(
                "record.copy_folded",
                target=str(copy.pk),
                actor=officer,
                request_number=copy.request.number,
                record_item_id=str(copy.pk),
                survivor_record_item_id=str(survivor.pk),
                message_id=fold.message_id,
                custodian=copy.custodian,
            )

    def handle(self, *args, **options):
        officer = (
            get_user_model()
            .objects.filter(username=options["reviewed_by"])
            .first()
        )
        if officer is None:
            raise CommandError(f"No user named {options['reviewed_by']!r}.")
        if not has_staff_role(
            officer, StaffRole.REVIEWER, StaffRole.RECORDS_OFFICER,
        ):
            raise CommandError(
                f"{officer.username!r} does not hold a review role; a "
                "reviewer or Records Officer must own this decision."
            )
        commit = bool(options["commit"])
        request_number = (options["request_number"] or "").strip()
        if request_number and not PublicRecordsRequest.objects.filter(
            number=request_number,
        ).exists():
            # A mistyped number would otherwise report a clean zero.
            raise CommandError(f"No request numbered {request_number!r}.")

        folds, skipped, unreadable = self._plan(
            self._candidates(request_number),
        )
        # Counted from the plan itself, so the figure is the set that was
        # actually decided rather than a second, separate read of the table.
        examined = len(folds) + len(skipped) + unreadable

        self.stdout.write(f"Decision by: {officer.username}")
        self.stdout.write(f"  examined : {examined}")
        self.stdout.write(
            ("  to fold : " if commit else "  would fold : ") + str(len(folds))
        )
        self.stdout.write(f"  skipped : {len(skipped)}")
        self.stdout.write(f"  unusable : {unreadable}")
        for copy, reason in skipped:
            self.stdout.write(
                f"    skip {copy.pk} {copy.request.number}: {reason}"
            )
        for fold in folds:
            self.stdout.write(
                f"    fold {fold.copy.pk} -> {fold.survivor.pk} "
                f"{fold.copy.request.number} {fold.message_id}"
            )

        if not commit:
            self.stdout.write(
                "Dry run; nothing written. Add --commit to record these folds."
            )
            return

        folded, failed = 0, 0
        for fold in folds:
            try:
                self._fold(fold, officer=officer)
            except (
                ValidationError,
                FoldStale,
                RecordItem.DoesNotExist,
                IntegrityError,
            ) as exc:
                # One copy that cannot be folded is reported and counted, and
                # the pass goes on: each fold is its own transaction, so what
                # was already written stands, and the summary below still
                # says what happened to the rest.
                failed += 1
                detail = "; ".join(getattr(exc, "messages", None) or [str(exc)])
                self.stderr.write(f"    FAILED {fold.copy.pk}: {detail}")
                continue
            folded += 1
        audit(
            "record.copies_folded",
            actor=officer,
            examined=examined,
            folded=folded,
            failed=failed,
            skipped=len(skipped),
            unusable=unreadable,
            request_number=request_number,
        )
        self.stdout.write(f"  folded : {folded}")
        self.stdout.write(f"  failed : {failed}")
        self.stdout.write(
            self.style.SUCCESS(f"Folded {folded}; failed {failed}.")
        )
