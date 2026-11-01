from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.models import PublicIntakeSubmission


class Command(BaseCommand):
    help = (
        "Mark an operator-verified stale intake receipt failed without "
        "creating or retrying a legal request."
    )

    def add_arguments(self, parser):
        parser.add_argument("receipt_id", type=int)
        parser.add_argument(
            "--operator",
            required=True,
            help="Active staff username accountable for the recovery decision.",
        )
        parser.add_argument(
            "--reason",
            required=True,
            help="Incident-specific reason the receipt is known to be abandoned.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm the intake process is no longer running.",
        )
        parser.add_argument(
            "--confirm-unlinked",
            action="store_true",
            help=(
                "Additionally confirm that an unlinked receipt was reconciled "
                "against the request queue and upstream submission."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        reason = options["reason"].strip()
        if len(reason) < 10:
            raise CommandError("--reason must be a substantive incident-specific explanation")

        operator = get_user_model().objects.filter(
            username=options["operator"],
            is_active=True,
            is_staff=True,
        ).first()
        if operator is None:
            raise CommandError("--operator must identify an active staff user")

        receipt_model = PublicIntakeSubmission
        try:
            receipt = (
                receipt_model.objects.select_for_update()
                .select_related("request")
                .get(pk=options["receipt_id"])
            )
        except receipt_model.DoesNotExist as exc:
            raise CommandError("Intake receipt not found") from exc

        if receipt.state != receipt_model.State.PROCESSING:
            raise CommandError(
                f"Receipt is {receipt.state}, not processing; no recovery is allowed."
            )

        stale_minutes = runtime.get("PRR_PUBLIC_INTAKE_PROCESSING_STALE_MINUTES")
        stale_before = timezone.now() - timedelta(minutes=stale_minutes)
        if receipt.created_at > stale_before:
            raise CommandError(
                f"Receipt is not stale (minimum age is {stale_minutes} minutes)."
            )
        if not options["confirm"]:
            raise CommandError(
                "No change made. Verify the original process is gone, then re-run "
                "with --confirm."
            )
        if receipt.request_id is None and not options["confirm_unlinked"]:
            raise CommandError(
                "This receipt has no linked request. Reconcile the upstream "
                "submission against the request queue, then re-run with "
                "--confirm-unlinked as well as --confirm."
            )

        receipt.state = receipt_model.State.FAILED
        receipt.last_error = (
            f"Operator recovery by {operator.get_username()}: {reason}"
        )[:100]
        receipt.save(update_fields=["state", "last_error"])

        if receipt.request_id:
            target = receipt.request.number
        else:
            target = f"public-intake-receipt:{receipt.pk}"
        audit(
            "public.intake_recovered",
            target=target,
            actor=operator,
            receipt_id=receipt.pk,
            source="public_form",
            request_number=receipt.request.number if receipt.request_id else None,
            reason=reason,
        )

        if receipt.request_id:
            outcome = (
                f"Linked request {receipt.request.number} remains authoritative; "
                "retries with the same signed form now return that number and a "
                "failed state."
            )
        else:
            outcome = (
                "No request is linked. The original signed form now remains "
                "failed; do not start a new submission until the original data has "
                "been reconciled."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Receipt {receipt.pk} marked failed by {operator.get_username()}. "
                f"{outcome}"
            )
        )
