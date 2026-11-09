"""Verify keyless Bedrock advisory inference with synthetic data only."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.configuration import runtime
from apps.records.advisory_ai import (
    AdvisoryProviderError,
    _validate_proposal,
    account_data_retention_mode,
    invoke_advisory_model,
    model_for_kind,
)
from apps.records.models import AdvisoryRun


class Command(BaseCommand):
    help = "Verify Bedrock advisory inference without reading or writing a request."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default="",
            help=(
                "Bedrock model identifier to verify instead of the configured "
                "request-intent model, e.g. before switching a setting to it."
            ),
        )

    def handle(self, *args, **options):
        # Zero data retention first (district decision, 2026-11-05): no model
        # is verified while Bedrock could keep what it is sent.
        try:
            mode = account_data_retention_mode()
        except AdvisoryProviderError as exc:
            raise CommandError(
                "Could not read the Bedrock data retention mode; zero "
                f"retention cannot be confirmed ({exc.code})."
            ) from exc
        if mode != "none":
            raise CommandError(
                f"Bedrock data retention mode is {mode or 'unset'}; zero "
                "retention (none) is required before any model is used."
            )
        kind = AdvisoryRun.Kind.REQUEST_INTENT
        model = options["model"] or model_for_kind(kind)
        received_date = timezone.localdate()
        payload = {
            "kind": kind,
            "request_description": (
                "Synthetic verification request for school board meeting minutes."
            ),
            # The instruction references this field, so the synthetic payload
            # carries it too: verification must exercise the real input shape.
            "request_received_date": received_date.isoformat(),
            "available_record_systems": [],
        }
        try:
            proposal = invoke_advisory_model(kind, payload, model_identifier=model)
            _validate_proposal(kind, proposal, received_date=received_date)
        except AdvisoryProviderError as exc:
            raise CommandError(
                f"Bedrock advisory verification failed: {exc.code}."
            ) from exc
        except ValueError as exc:
            raise CommandError(
                "Bedrock advisory verification returned invalid bounded JSON."
            ) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Bedrock advisory verification passed — "
                f"{model} in "
                f"{runtime.get('PRR_AI_AWS_REGION')}; "
                "data retention none; "
                "synthetic data only and no legal record was created."
            )
        )
