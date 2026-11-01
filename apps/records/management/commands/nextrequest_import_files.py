# ABOUTME: Dry-run-by-default CLI that plans and commits the NextRequest file
# ABOUTME: migration against a staged, digest-verified manifest.
"""Migrate staged NextRequest files into scanned, reviewable records.

Dry run by default. Committing requires both ``--commit`` and the exact plan
digest from the reviewed dry run, so the bytes that were reviewed are the
bytes that are stored.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.records.nextrequest_files import (
    RESOLVABLE_CLASSIFICATIONS,
    LegacyFileMigrationError,
    build_plan,
    commit_plan,
)


class Command(BaseCommand):
    help = "Plan or commit the NextRequest attachment migration."

    def add_arguments(self, parser):
        parser.add_argument("manifest", help="Manifest CSV of staged files.")
        parser.add_argument("files_root", help="Directory the manifest paths are relative to.")
        parser.add_argument("--operator", required=True)
        parser.add_argument(
            "--ambiguous-classification",
            choices=RESOLVABLE_CLASSIFICATIONS,
            default="",
            help=(
                "Resolve rows the manifest could not classify. Omit it and "
                "those rows block the run instead of being guessed."
            ),
        )
        parser.add_argument(
            "--no-expand-archives",
            action="store_true",
            help="Store archives whole. They cannot be previewed or redacted.",
        )
        parser.add_argument("--commit", action="store_true")
        parser.add_argument("--expected-plan-sha256", default="")

    def handle(self, *args, **options):
        manifest = Path(options["manifest"])
        files_root = Path(options["files_root"])
        if not manifest.is_file():
            raise CommandError(f"Manifest {manifest} does not exist.")
        if not files_root.is_dir():
            raise CommandError(f"Files root {files_root} does not exist.")

        try:
            plan = build_plan(
                manifest,
                files_root,
                ambiguous_classification=options["ambiguous_classification"],
                expand_archives=not options["no_expand_archives"],
            )
        except LegacyFileMigrationError as exc:
            raise CommandError(exc.messages[0]) from exc

        counts = plan.counts()
        self.stdout.write("NextRequest file migration plan")
        self.stdout.write(f"  manifest sha256 : {plan.manifest_sha256}")
        self.stdout.write(f"  plan sha256     : {plan.plan_sha256}")
        for label, key in (
            ("source documents", "manifest_rows"),
            ("planned items", "planned_items"),
            ("to store", "to_store"),
            ("  responsive records", "record_items"),
            ("  requester uploads", "intake_attachments"),
            ("duplicates collapsed", "duplicates"),
            ("empty, stored nothing", "skipped"),
            ("bytes to store", "bytes_to_store"),
        ):
            self.stdout.write(f"  {label:22}: {counts[key]:,}")

        for warning in plan.warnings:
            self.stdout.write(self.style.WARNING(f"  warning: {warning}"))
        for blocker in plan.blockers:
            self.stdout.write(self.style.ERROR(f"  blocked: {blocker}"))

        if plan.blockers:
            raise CommandError(
                f"{len(plan.blockers)} blocking issue(s). Nothing was written."
            )
        if not options["commit"]:
            self.stdout.write(self.style.SUCCESS("Dry run only. Nothing was written."))
            return
        if options["expected_plan_sha256"] != plan.plan_sha256:
            raise CommandError(
                "--expected-plan-sha256 does not match this plan. Re-review the "
                "dry run before committing."
            )

        try:
            results = commit_plan(
                plan, manifest, files_root, operator=options["operator"]
            )
        except LegacyFileMigrationError as exc:
            raise CommandError(exc.messages[0]) from exc
        self.stdout.write(self.style.SUCCESS(
            "Committed: "
            f"{results['created']} stored, {results['duplicate']} duplicate, "
            f"{results['skipped']} empty, {results['infected']} infected."
        ))
        if results["infected"]:
            self.stdout.write(self.style.ERROR(
                "Infected files were rejected and their evidence retained."
            ))
