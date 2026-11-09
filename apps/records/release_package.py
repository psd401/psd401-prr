"""One-step release package (issue #47).

``build_release_package`` is the Releases tab's "Build package". With no
installment open, it opens one from every release-ready record no live
installment has claimed. With one open (an officer's partial selection, or a
build that stopped partway), it finishes that installment as chosen and adds
nothing to it; the tab says how many ready records it leaves for the next
package. Either way it then generates the RCW 42.56.210(3) exemption log when
the installment needs one, and prepares the immutable package. Each step is the existing service, run in
the same order an officer used to click through; nothing here releases or
sends. Release and send stays a separate, human-only action (rule 6).

The blockers an officer can fix elsewhere are checked before anything is
created, and come back as ``ReleasePackageBlocked`` naming the case tab that
fixes them. Building again is safe: an open installment is finished rather
than duplicated, and a prepared one is returned as it is.
"""

from django.core.exceptions import ValidationError
from django.db.models import Q

from .exemption_logs import generate_exemption_log
from .fees import open_deposit_gate
from .installments import (
    create_installment,
    release_ready_record_items,
    unreleased_record_items,
)
from .models import FeeDepositGate, Installment, PublicRecordsRequest, RecordItem
from .production import prepare_installment
from .services import CLOSED_STATUSES, _require_current_records_officer


class ReleasePackageBlocked(ValidationError):
    """A blocker the officer fixes on another part of the case."""

    def __init__(self, message: str, *, code: str, tab: str):
        super().__init__(message, code=code)
        self.code = code
        self.tab = tab


def _needs_exemption_log(installment: Installment) -> bool:
    # The same condition the installment serializer reports as has_exemptions.
    return installment.items.filter(
        Q(review_state=RecordItem.ReviewState.WITHHELD)
        | Q(redactions__isnull=False, redactions__voided_at__isnull=True)
        | Q(withholding_exemptions__isnull=False)
        | Q(withholding_exemption__isnull=False)
    ).exists()


def _exemption_log_ready(installment: Installment) -> bool:
    return bool(
        installment.exemption_log_storage_key
        and installment.exemption_log_manifest_sha256
        and installment.exemption_log_prepared_at is not None
    )


def build_release_package(request_number, *, actor, _client=None) -> Installment:
    """Create, log, and prepare the next release package in one action."""
    _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to build a release package.",
    )
    prr = PublicRecordsRequest.objects.get(number=request_number)
    if prr.status in CLOSED_STATUSES:
        raise ReleasePackageBlocked(
            "This request is closed; reopen it before releasing records.",
            code="request_closed",
            tab="overview",
        )
    gate = open_deposit_gate(prr, scope=FeeDepositGate.Scope.PRODUCTION)
    if gate is not None:
        raise ReleasePackageBlocked(
            f"A ${gate.amount:.2f} production deposit must be recorded or lifted first.",
            code="deposit_required",
            tab="releases",
        )

    installment = (
        Installment.objects.filter(request=prr, revoked_at__isnull=True, released_at__isnull=True)
        .order_by("-sequence")
        .first()
    )
    if installment is None:
        ready = unreleased_record_items(
            release_ready_record_items(RecordItem.objects.filter(request=prr))
        )
        item_ids = [str(pk) for pk in ready.values_list("pk", flat=True)]
        if not item_ids:
            raise ReleasePackageBlocked(
                "No records are ready to release. A record is ready once it is "
                "reviewed, any heightened-review citation has a second approval, "
                "and any redactions are rendered.",
                code="no_ready_records",
                tab="review",
            )
        installment = create_installment(prr.number, actor=actor, item_ids=item_ids)

    if _needs_exemption_log(installment) and not _exemption_log_ready(installment):
        installment = generate_exemption_log(installment.pk, actor=actor, _client=_client)
    if installment.package_prepared_at is None:
        installment = prepare_installment(installment.pk, actor=actor, _client=_client)
    installment.refresh_from_db()
    return installment
