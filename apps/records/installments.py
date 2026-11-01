"""Installment membership, revocation, access codes, and one-action release.

``production.py`` owns turning a locked installment into immutable bytes.
This module owns everything around that: which records are in an installment,
whether the requester can still fetch it, and the single officer action that
releases a package and sends its notice. The legal guarantees are unchanged --
membership only moves while no package is prepared, every write is audited,
and only a current Records Officer may act.
"""

from __future__ import annotations

import itertools

from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .models import Installment, PublicRecordsRequest, RecordItem, Redaction
from .production import release_installment_with_notice
from .services import CLOSED_STATUSES, _require_current_records_officer


class InstallmentNoticeUnsent(ValidationError):
    """The package was released, but its notice did not go out.

    Raised after the release has committed, so the caller can tell a release
    that stands with an unsent notice apart from a release that never
    happened. The notice stays a draft; pressing the button again once the
    cause is fixed sends it.
    """


def release_ready_record_items(queryset):
    """Narrow record items to the ones an installment could actually ship.

    These are the conditions ``production._artifact_for_item`` enforces at
    packaging time, expressed in SQL so the Releases tab can list and count
    them. Production remains the authority; this is a filter, and
    ``test_release_ready_filter_matches_production`` keeps the two in step.
    """
    active_redactions = Redaction.objects.filter(
        record_item=OuterRef("pk"),
        voided_at__isnull=True,
    )
    second_review_outstanding = Redaction.objects.filter(
        record_item=OuterRef("pk"),
        voided_at__isnull=True,
        exemption__requires_second_review=True,
    )
    redacted_artifact_ready = (
        ~Q(redacted_storage_key="")
        & ~Q(redacted_sha256="")
        & Q(redacted_content_length__isnull=False)
        & ~Q(redacted_content_type="")
        & ~Q(redacted_manifest_sha256="")
    )
    withheld_second_review = Q(
        review_state=RecordItem.ReviewState.WITHHELD,
    ) & (
        Q(withholding_exemption__requires_second_review=True)
        | Q(withholding_exemptions__requires_second_review=True)
    )
    return (
        queryset.annotate(
            _has_active_redactions=Exists(active_redactions),
            _needs_redaction_second_review=Exists(second_review_outstanding),
        )
        .filter(
            reviewed_at__isnull=False,
            reviewed_by__isnull=False,
            review_state__in=[
                RecordItem.ReviewState.RESPONSIVE,
                RecordItem.ReviewState.WITHHELD,
            ],
        )
        .exclude(
            Q(second_reviewed_at__isnull=True)
            & (Q(_needs_redaction_second_review=True) | withheld_second_review)
        )
        .filter(Q(_has_active_redactions=False) | redacted_artifact_ready)
        .distinct()
    )


def unreleased_record_items(queryset):
    """Items no live installment has claimed yet.

    Revoking an installment releases its claim, so its records can be
    re-packaged into a new one. That is the difference between revoking a
    download and simply letting the link expire.
    """
    # Written as NOT EXISTS rather than exclude(installments__...) because a
    # reverse-relation exclude matches the NULLs of the outer join too, and
    # would drop every record that is in no installment at all -- which is
    # precisely the set this is supposed to return.
    live_membership = Installment.objects.filter(
        items=OuterRef("pk"),
        revoked_at__isnull=True,
    )
    return queryset.annotate(
        _in_live_installment=Exists(live_membership),
    ).filter(_in_live_installment=False)


def _validated_membership(prr, item_ids):
    """Resolve requested record ids to this request's releasable items."""
    unique_ids = list(dict.fromkeys(item_ids or []))
    if not unique_ids:
        raise ValidationError(
            "An installment must contain at least one record item."
        )
    # Read once: the number the check used is the number the officer is told,
    # even if an administrator changes the limit mid-request.
    maximum_items = runtime.get("PRR_INSTALLMENT_MAX_ITEMS")
    if len(unique_ids) > maximum_items:
        raise ValidationError(
            f"An installment may hold at most {maximum_items} record items."
        )
    items = list(
        RecordItem.objects.select_for_update()
        .filter(pk__in=unique_ids, request=prr)
        .order_by("created_at", "pk")
    )
    if len(items) != len(unique_ids):
        raise ValidationError(
            "Every selected record must belong to this request."
        )
    ready = set(
        release_ready_record_items(
            RecordItem.objects.filter(pk__in=unique_ids)
        ).values_list("pk", flat=True)
    )
    not_ready = [item for item in items if item.pk not in ready]
    if not_ready:
        raise ValidationError(
            "These records are not ready for release: "
            + ", ".join(str(item.pk) for item in not_ready[:10])
        )
    return items


def _reject_records_already_claimed(items, *, excluding=None):
    live = Installment.objects.filter(
        revoked_at__isnull=True,
        items__pk__in=[item.pk for item in items],
    )
    if excluding is not None:
        live = live.exclude(pk=excluding)
    live_pks = set(live.values_list("items__pk", flat=True))
    claimed = [item.pk for item in items if item.pk in live_pks]
    if claimed:
        raise ValidationError(
            "These records are already in a live installment: "
            + ", ".join(str(pk) for pk in claimed[:10])
        )


def _is_trivial_run(code):
    """One repeated character, or a straight run like 12345678 or abcdefgh.

    Deliberately narrow: it stops the two codes an officer is most likely to
    type without thinking, and nothing more. Mixed weak patterns are a
    dictionary problem with no clean edge; the length floor and the
    per-address and per-installment guess throttles are the real defense.
    """
    steps = {ord(b) - ord(a) for a, b in itertools.pairwise(code)}
    return steps <= {0} or steps == {1} or steps == {-1}


def _locked_installment(installment_id):
    return (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )


def _locked_open_installment(installment_id):
    installment = _locked_installment(installment_id)
    if installment.package_prepared_at is not None:
        raise ValidationError(
            "Installment contents are locked once a package is prepared; "
            "create a new installment instead."
        )
    if installment.released_at is not None:
        raise ValidationError(
            "A released installment cannot change its contents."
        )
    return installment


@transaction.atomic
def create_installment(request_number, *, actor, item_ids):
    """Open the next installment for one request from selected records."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to create an installment."
        ),
    )
    prr = PublicRecordsRequest.objects.select_for_update().get(
        number=request_number,
    )
    if prr.status in CLOSED_STATUSES:
        raise ValidationError(
            "This request is closed; reopen it before releasing records."
        )
    items = _validated_membership(prr, item_ids)
    _reject_records_already_claimed(items)
    next_sequence = (
        Installment.objects.filter(request=prr)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
        or 0
    ) + 1
    installment = Installment.objects.create(
        request=prr,
        sequence=next_sequence,
    )
    installment.items.set(items)
    audit(
        "installment.created",
        target=prr.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        item_count=len(items),
        record_item_ids=sorted(str(item.pk) for item in items),
    )
    return installment


@transaction.atomic
def set_installment_items(installment_id, *, actor, item_ids):
    """Replace one unprepared installment's membership in a single action.

    Membership is a filing decision on an open case, so like
    ``create_installment`` it refuses a closed request. Revoking a link and
    setting or clearing its access code are different: a released link
    outlives closure by its whole download window, and an officer must be
    able to manage it without reopening the case.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to change installment "
            "contents."
        ),
    )
    installment = _locked_open_installment(installment_id)
    if installment.request.status in CLOSED_STATUSES:
        raise ValidationError(
            "This request is closed; reopen it before changing what an "
            "installment contains."
        )
    items = _validated_membership(installment.request, item_ids)
    _reject_records_already_claimed(items, excluding=installment.pk)
    previous = sorted(
        str(pk) for pk in installment.items.values_list("pk", flat=True)
    )
    installment.items.set(items)
    audit(
        "installment.items_set",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        item_count=len(items),
        previous_record_item_ids=previous,
        record_item_ids=sorted(str(item.pk) for item in items),
    )
    return installment


@transaction.atomic
def revoke_installment(installment_id, *, actor, reason):
    """Withdraw requester access to one released installment.

    The released artifact, its manifest, and the audit trail stay exactly as
    they were; only the requester's ability to fetch the bytes ends. This
    works on a closed request on purpose: the link outlives closure, and
    taking it back must not require reopening the case.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to revoke a download."
        ),
    )
    explanation = str(reason or "").strip()
    if len(explanation) < 10:
        raise ValidationError(
            "Revoking a requester download requires a substantive reason."
        )
    installment = _locked_installment(installment_id)
    if installment.released_at is None:
        raise ValidationError("Only a released installment can be revoked.")
    if installment.revoked_at is not None:
        raise ValidationError("This installment download is already revoked.")
    installment.revoked_at = timezone.now()
    installment.revoked_by = current_actor
    installment.revocation_reason = explanation[:1000]
    installment.save(
        update_fields=["revoked_at", "revoked_by", "revocation_reason"]
    )
    audit(
        "installment.revoked",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        reason=installment.revocation_reason,
        download_count=installment.download_count,
    )
    return installment


@transaction.atomic
def set_installment_access_code(installment_id, *, actor, access_code):
    """Put a second factor on one installment's requester download.

    The code is hashed the way a password is. Nothing stores, logs, or returns
    it in clear text; the officer sends it to the requester in a separate
    message, which is the only reason a second factor helps at all.

    Like ``revoke_installment`` and ``clear_installment_access_code``, this
    works on a closed request on purpose: a released link outlives closure,
    and managing it must not require reopening the case.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to set an access code."
        ),
    )
    # Stripped once, here, so the length rule and the stored hash agree on
    # what the code is; a requester never has to reproduce stray spaces.
    code = str(access_code or "").strip()
    minimum_length = runtime.get("PRR_INSTALLMENT_ACCESS_CODE_MIN_LENGTH")
    if len(code) < minimum_length:
        raise ValidationError(
            "An installment access code must be at least "
            f"{minimum_length} characters."
        )
    if len(code) > 128:
        raise ValidationError(
            "An installment access code is at most 128 characters."
        )
    if _is_trivial_run(code):
        raise ValidationError(
            "An access code cannot be a single repeated or sequential run "
            "of characters."
        )
    installment = _locked_installment(installment_id)
    if installment.revoked_at is not None:
        raise ValidationError(
            "A revoked installment download cannot be changed."
        )
    installment.access_code_hash = make_password(code)
    installment.access_code_set_at = timezone.now()
    installment.access_code_set_by = current_actor
    installment.save(
        update_fields=[
            "access_code_hash",
            "access_code_set_at",
            "access_code_set_by",
        ]
    )
    audit(
        "installment.access_code_set",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
    )
    return installment


@transaction.atomic
def clear_installment_access_code(installment_id, *, actor):
    """Remove the second factor, for instance when a requester loses it."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to clear an access code."
        ),
    )
    installment = _locked_installment(installment_id)
    if installment.revoked_at is not None:
        raise ValidationError(
            "A revoked installment download cannot be changed."
        )
    if not installment.access_code_hash:
        raise ValidationError("This installment has no access code.")
    installment.access_code_hash = ""
    installment.access_code_set_at = None
    installment.access_code_set_by = None
    installment.save(
        update_fields=[
            "access_code_hash",
            "access_code_set_at",
            "access_code_set_by",
        ]
    )
    audit(
        "installment.access_code_cleared",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
    )
    return installment


def release_installment_and_send_notice(installment_id, *, actor, is_final):
    """Release, draft the notice, and send it as one accountable human act.

    These were three clicks expressing one decision by one Records Officer.
    The notice is still a full ``Correspondence`` snapshot, still approved by
    a named human, still delivered through the ordinary queue: nothing here
    is automated in the rule 6 sense, because a person pressed the button and
    is recorded as having done so.

    Release and drafting share one transaction so a failed draft can never
    leave a released-but-unannounced package. Approval and queueing run after
    that commits, for the same reason ``approve_and_send`` checks transport
    before approving: a delivery failure must not silently undo a release the
    audit log already records.
    """
    from apps.correspondence import services as correspondence_services
    from apps.correspondence.models import Correspondence

    with transaction.atomic():
        installment, download_url, notice = release_installment_with_notice(
            installment_id,
            actor=actor,
            is_final=is_final,
        )
    if notice.status != Correspondence.Status.DRAFT:
        # Already sent on an earlier press. Nothing happened this time, so
        # nothing is recorded as having happened.
        return installment, download_url, notice
    try:
        correspondence_services.approve_and_send(
            notice,
            actor=actor,
            expected_version=notice.version,
        )
    except ValidationError as exc:
        # The release above has committed and stands. Say so, and leave a
        # record of the send that did not happen, rather than letting a
        # transport error read as if the button did nothing.
        reason = "; ".join(exc.messages)
        audit(
            "installment.release_notice_unsent",
            target=installment.request.number,
            actor=actor,
            installment_id=installment.pk,
            sequence=installment.sequence,
            correspondence_id=str(notice.pk),
            reason=reason,
        )
        raise InstallmentNoticeUnsent(
            "The package was released and its notice drafted, but the notice "
            f"was not sent: {reason} Once that is resolved, press Release and "
            "send again, or send the draft from Messages."
        ) from exc
    notice.refresh_from_db()
    audit(
        "installment.released_and_sent",
        target=installment.request.number,
        actor=actor,
        installment_id=installment.pk,
        sequence=installment.sequence,
        correspondence_id=str(notice.pk),
        correspondence_status=notice.status,
        is_final=is_final,
    )
    return installment, download_url, notice
