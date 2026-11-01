# ABOUTME: Populate email metadata and custodian attribution on existing
# ABOUTME: record items from their retained raw Vault messages.
"""Backfill email metadata onto records ingested before it was stored.

Every Vault message is retained byte-for-byte as an immutable object with its
own digest (``VaultMessageArtifact.raw_storage_key``), so the metadata the
review queue now needs — From, To, Cc, Date, Message-ID, In-Reply-To, sender
domain, ``Auto-Submitted``, list headers, and whether there was a calendar
part — can be read from the message itself rather than guessed.

The same pass records each record's full custodian set from the collection
task that produced it, replacing the old comma-joined ``custodian`` string.

Nothing here reviews, releases, withholds, or communicates. It writes
descriptive facts about retained messages and appends one audit row. Records
whose raw message was never retained, or whose bytes fail their recorded
digest, are counted and skipped rather than filled in with a guess.

Dry run by default; ``--commit`` writes.
"""

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from apps.audit.models import audit
from apps.records.email_metadata import (
    RECORD_ITEM_METADATA_FIELDS,
    parse_email_metadata,
)
from apps.records.models import RecordItem, VaultMessageArtifact
from apps.records.storage import load_verified_object
from apps.records.vault_ingest import attach_custodians


class Command(BaseCommand):
    help = "Backfill email metadata on record items from retained raw messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Write the metadata. Without this the command only reports.",
        )
        parser.add_argument(
            "--request",
            dest="request_number",
            default="",
            help="Limit the pass to one request number.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="include_populated",
            help=(
                "Re-read every retained message, including records that "
                "already carry metadata."
            ),
        )

    def handle(self, *args, **options):
        commit = bool(options["commit"])
        artifacts = (
            VaultMessageArtifact.objects.filter(
                outcome=VaultMessageArtifact.Outcome.CREATED,
                record_item__isnull=False,
            )
            .exclude(raw_storage_key="")
            .select_related(
                "record_item",
                "export_object__ingest_run__collection_task",
            )
            .order_by("pk")
        )
        request_number = (options["request_number"] or "").strip()
        if request_number:
            artifacts = artifacts.filter(
                record_item__request__number=request_number,
            )
        if not options["include_populated"]:
            artifacts = artifacts.filter(record_item__email_message_id="")

        updated = 0
        custodians_recorded = 0
        unreadable = 0
        collisions = 0
        examined = 0
        #: Dry-run only: the deduplication keys a write would already have
        #: taken by the time it reached this record.
        planned = set()
        for artifact in artifacts.iterator():
            examined += 1
            item = artifact.record_item
            try:
                source = load_verified_object(
                    artifact.raw_storage_key,
                    expected_size=artifact.raw_size or 0,
                    expected_sha256=artifact.raw_sha256,
                )
            except Exception as exc:  # noqa: BLE001 - reported, never guessed
                unreadable += 1
                self.stderr.write(
                    f"{item.pk}: retained message unreadable ({type(exc).__name__})"
                )
                continue
            try:
                raw_message = source.read()
            finally:
                source.close()
            try:
                metadata = parse_email_metadata(raw_message)
            except Exception as exc:  # noqa: BLE001 - reported, never guessed
                # Isolated for the same reason an unreadable object is: one
                # bad historical message must not abort the pass and strand
                # every later record without metadata.
                unreadable += 1
                self.stderr.write(
                    f"{item.pk}: retained message unparseable "
                    f"({type(exc).__name__})"
                )
                continue
            task = getattr(
                getattr(artifact.export_object, "ingest_run", None),
                "collection_task",
                None,
            )
            self.stdout.write(
                f"{item.pk}: message_id={metadata.message_id or '(none)'} "
                f"sender_domain={metadata.sender_domain or '(none)'} "
                f"calendar={metadata.has_calendar_part}"
            )
            if not commit:
                # A dry run has to predict the collisions a write would hit,
                # not report zero of them. An IntegrityError only fires on a
                # real write, so without this the summary would promise to
                # update records that --commit will in fact leave alone.
                # Both sources of collision count: a key another record
                # already carries, and a key an earlier record in this very
                # pass would have taken first.
                key = (
                    item.request_id, metadata.message_id, metadata.body_sha256,
                )
                if metadata.message_id and metadata.body_sha256 and (
                    key in planned
                    or RecordItem.objects.filter(
                        request_id=item.request_id,
                        email_message_id=metadata.message_id,
                        email_body_sha256=metadata.body_sha256,
                    )
                    .exclude(pk=item.pk)
                    .exists()
                ):
                    collisions += 1
                    self.stderr.write(
                        f"{item.pk}: another record of this request already "
                        f"carries this message "
                        f"({metadata.message_id}); --commit would leave it "
                        "unchanged for review"
                    )
                else:
                    planned.add(key)
                continue
            try:
                with transaction.atomic():
                    locked = (
                        RecordItem.objects.select_for_update().get(pk=item.pk)
                    )
                    for field, value in (
                        metadata.as_record_item_fields().items()
                    ):
                        setattr(locked, field, value)
                    locked.save(
                        update_fields=list(RECORD_ITEM_METADATA_FIELDS),
                    )
                    updated += 1
                    if task is not None and not locked.custodians.exists():
                        attach_custodians(locked, task)
                        custodians_recorded += 1
            except IntegrityError:
                # Records ingested before deduplication existed can already
                # hold two copies of one message. Folding them together is a
                # decision about the record set, so the pass names the pair
                # and leaves it to a human rather than aborting and stranding
                # every later record without metadata.
                collisions += 1
                self.stderr.write(
                    f"{item.pk}: another record of this request already "
                    f"carries this message "
                    f"({metadata.message_id or '(none)'}); left unchanged "
                    "for review"
                )

        summary = {
            "examined": examined,
            "updated": updated,
            "custodian_sets_recorded": custodians_recorded,
            "unreadable": unreadable,
            "collisions": collisions,
            "request_number": request_number,
        }
        if commit:
            audit("record.email_metadata.backfilled", **summary)
        self.stdout.write(
            self.style.SUCCESS(
                ("Wrote" if commit else "Would write")
                + f" metadata for {examined - unreadable - collisions} of "
                f"{examined} retained messages ({unreadable} unreadable, "
                f"{collisions} already recorded on another record)."
            )
        )
