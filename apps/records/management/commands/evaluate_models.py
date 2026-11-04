"""Score candidate advisory models on one evaluation set (issue #32)."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.evaluation import evaluate_models
from apps.records.models import EvaluationSet


def _price(value: str) -> tuple[str, tuple[float, float]]:
    try:
        model, rates = value.split("=", 1)
        input_rate, output_rate = rates.split("/", 1)
        return model.strip(), (float(input_rate), float(output_rate))
    except ValueError as exc:
        raise CommandError(
            f"--price {value!r} must look like MODEL=INPUT/OUTPUT in USD per million tokens."
        ) from exc


class Command(BaseCommand):
    help = (
        "Send every example in an evaluation set to every model through the "
        "advisory provider path and store an EvaluationRun. Real request and "
        "record text is sent, so advisory AI must be enabled."
    )

    def add_arguments(self, parser):
        parser.add_argument("--set", required=True, dest="set_id")
        parser.add_argument("--models", required=True, help="Comma-separated model identifiers.")
        parser.add_argument(
            "--price", action="append", default=[],
            help="MODEL=INPUT/OUTPUT, USD per million tokens; repeat per model. "
                 "A model without one reports no cost.",
        )
        parser.add_argument("--actor", required=True, help="Email of the accountable staff member.")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(email__iexact=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No staff user has the email {options['actor']}.")
        evaluation_set = EvaluationSet.objects.filter(pk=options["set_id"]).first()
        if evaluation_set is None:
            raise CommandError(f"No evaluation set {options['set_id']}.")
        models = [model.strip() for model in options["models"].split(",") if model.strip()]
        if not models:
            raise CommandError("Name at least one model.")
        prices = dict(_price(value) for value in options["price"])
        try:
            run = evaluate_models(evaluation_set, models, actor=actor, prices=prices)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        for model, result in run.results.items():
            self.stdout.write(
                f"{model}: accuracy {result['accuracy']} safety_recall {result['safety_recall']} "
                f"errors {result['errors']} cost/item {result['cost_per_item_usd']} "
                f"p95 {result['latency_p95_ms']}ms"
            )
        self.stdout.write(f"evaluation run {run.pk}")
