"""Label an evaluation set from decisions staff already made (issue #32)."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.evaluation import DEFAULT_LIMIT, build_evaluation_set
from apps.records.models import EvaluationSet


class Command(BaseCommand):
    help = (
        "Snapshot labelled examples (references and labels, never text) from "
        "production decisions into an EvaluationSet. Sends nothing anywhere."
    )

    def add_arguments(self, parser):
        parser.add_argument("--kind", required=True, choices=EvaluationSet.Kind.values)
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        parser.add_argument("--actor", required=True, help="Email of the accountable staff member.")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(email__iexact=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No staff user has the email {options['actor']}.")
        try:
            evaluation_set = build_evaluation_set(
                options["kind"], actor=actor, limit=options["limit"],
            )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(f"labels: {evaluation_set.label_counts}")
        self.stdout.write(f"evaluation set {evaluation_set.pk}")
