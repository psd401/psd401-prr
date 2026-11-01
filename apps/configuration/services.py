# ABOUTME: The only write path for application-managed settings — reason
# ABOUTME: required, dependencies checked, history and audit row appended.
"""Changing a switch is an accountable act, so it has exactly one door.

Every change through :func:`set_value` writes the current value, appends an
immutable ``AppSettingChange``, drops the cached read, and audit-logs. There
is no bulk path and no unattributed path: the system import is the one
actor-less source, and it exists only to write down what the environment was
already doing.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.audit.models import audit

from . import runtime, secrets, values, verification
from .models import AppSecret, AppSetting, AppSettingChange
from .prerequisites import PREREQUISITES
from .registry import (
    SETTINGS_BY_KEY,
    dependents_of,
    relations_for,
    spec_for,
)
from .signin import SIGN_IN_KEYS, lockout_blocker

REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 2000


def _clean_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if len(text) < REASON_MIN_LENGTH:
        raise ValidationError(
            "A substantive reason is required to change a district setting."
        )
    if len(text) > REASON_MAX_LENGTH:
        raise ValidationError(
            f"A setting-change reason may be at most {REASON_MAX_LENGTH} "
            "characters."
        )
    return text


def _coerce(spec, value, *, strict=False):
    """Validate and normalise, reporting the bound the admin just missed."""
    try:
        return values.coerce(spec, value, strict=strict)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _resolver(key=None, value=None):
    """Read any setting as it will be *after* the change being validated.

    Cross-setting checks have to judge the world the save would create, not
    the one it is leaving. Everything but the key under change is read
    uncached, because a five-second-old copy is exactly long enough to let
    two administrators each save a half of a contradiction.
    """

    def resolve(other: str):
        if other == key:
            return value
        return runtime.get_uncached(other)

    return resolve


def _secret_resolver(key=None, is_set=None):
    """The same, for the write-only settings.

    A cross-setting check may need to know whether a secret *exists* — the
    lockout rule does — and must never need its value. This answers only the
    question it is allowed to answer.
    """

    def resolve(other: str) -> bool:
        if other == key:
            return bool(is_set)
        return runtime.secret_is_set(other)

    return resolve


#: The PostgreSQL advisory lock every sign-in change takes. Any fixed 64-bit
#: value works, so long as nothing else in the database uses the same one.
SIGN_IN_ADVISORY_LOCK_ID = 254_0001


def _lock_sign_in_rows(key) -> None:
    """Serialize every sign-in change behind one lock.

    The lockout rule reads the *other* sign-in keys to judge this one. Two
    admins saving two different sign-in keys would otherwise each be judged
    against the other's pre-commit value, and together close every door
    neither could close alone. Taking one lock before this key's own row
    makes the second save wait for the first and be judged against its
    result.

    Row locks alone cannot do that for a key with no row yet, and before the
    first import no sign-in row exists at all. On PostgreSQL the lock is
    therefore a transaction-scoped advisory lock, which exists whether or
    not any row does. Other databases take the row locks; SQLite already
    serializes every write.
    """
    if key not in SIGN_IN_KEYS:
        return
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)", [SIGN_IN_ADVISORY_LOCK_ID]
            )
        return
    for model in (AppSetting, AppSecret):
        list(
            model.objects.select_for_update()
            .filter(key__in=SIGN_IN_KEYS)
            .order_by("key")
        )


def _check_no_lockout(key, *, resolve, resolve_secret) -> None:
    """Refuse a sign-in change that would leave nobody able to sign in.

    Unlike a prerequisite, this is asked in both directions: turning a
    sign-in switch *off* is exactly the change that can strand everyone.

    What it compares is before against after, not after alone. A deployment
    whose sign-in is *already* broken must still be repairable, and repair
    takes several saves -- open the admin route, then turn on break-glass,
    then fix the client. Judging the end state alone would refuse the first
    of those and leave the only way back in a shell on the host, which is
    the situation this whole phase exists to end. So the rule is narrower
    and exactly as protective: a change may not be the thing that closes the
    last door.
    """
    if key not in SIGN_IN_KEYS:
        return
    if lockout_blocker(_resolver(), _secret_resolver()) is not None:
        return
    problem = lockout_blocker(resolve, resolve_secret)
    if problem:
        raise ValidationError(problem)


def _check_enable_allowed(spec, *, resolve=None) -> None:
    """Dependencies and prerequisites, checked when a switch is turned on."""
    resolve = resolve or _resolver()
    missing = [
        SETTINGS_BY_KEY[required].label
        for required in spec.requires
        if not resolve(required)
    ]
    if missing:
        raise ValidationError(
            f"{spec.label} needs these switches on first: "
            + ", ".join(missing)
            + "."
        )
    if spec.prerequisite_rule:
        problem = PREREQUISITES[spec.prerequisite_rule](resolve)
        if problem:
            raise ValidationError(problem)


def _check_relations(spec, resolve) -> None:
    """Ordering constraints between this setting and another."""
    for relation in relations_for(spec.key):
        if resolve(relation.greater) < resolve(relation.lesser):
            raise ValidationError(relation.message)


def enable_blocker(key: str) -> str | None:
    """Why ``key`` could not be turned on right now, or None if it could.

    The import uses this to report an environment value it preserved even
    though the Settings page would have refused it.
    """
    spec = spec_for(key)
    if spec.type != values.BOOLEAN:
        return None
    try:
        _check_enable_allowed(spec)
    except ValidationError as error:
        return " ".join(error.messages)
    return None


@transaction.atomic
def set_value(
    key: str,
    value,
    *,
    actor,
    reason: str,
    ip_address=None,
) -> AppSetting:
    """Record one Application Admin's change to one setting."""
    spec = spec_for(key)
    if spec.type == values.SECRET:
        raise ValidationError(
            f"{spec.label} is a secret and is set through its own write-only "
            "path, not as an ordinary setting value."
        )
    new_value = _coerce(spec, value, strict=True)
    clean_reason = _clean_reason(reason)
    if actor is None:
        raise ValidationError("A setting change requires a named actor.")
    # Re-verify authority inside the one write door, as every other
    # accountable-human gate does, so a shell or a future caller cannot
    # change a district switch on the API layer's word alone. This reloads
    # and locks the actor and their role rows for the whole transaction.
    from apps.api.identity import StaffRole
    from apps.records.services import _require_current_staff

    actor = _require_current_staff(
        actor=actor,
        message="Application Admin authority is required to change a district setting.",
        roles=(StaffRole.ADMIN,),
    )

    stored_value = values.to_json(spec, new_value)
    _lock_sign_in_rows(key)
    row = AppSetting.objects.select_for_update().filter(key=key).first()
    old_value = row.value if row is not None else None
    # Read the value this change is actually replacing from the locked row,
    # not from the cache: a cached copy can be seconds behind, and deciding
    # whether this is an enable is what selects the dependency checks.
    effective_old = (
        _coerce(spec, old_value) if row is not None else runtime.get_uncached(key)
    )
    if row is not None and stored_value == old_value:
        raise ValidationError(f"{spec.label} is already set to that value.")

    resolve = _resolver(key, new_value)
    if spec.type == values.BOOLEAN and new_value and not effective_old:
        _check_enable_allowed(spec, resolve=resolve)
    # Ordering constraints hold after every save, including one that lowers
    # a value: a combined ceiling dropping below its per-item ceiling is as
    # broken as the reverse.
    _check_relations(spec, resolve)
    _check_no_lockout(key, resolve=resolve, resolve_secret=_secret_resolver())

    if row is None:
        # A key with no row has nothing to lock, so the deploy-time import
        # (or another first change) can insert it between our read and this
        # write. Refuse cleanly rather than failing on the unique key; the
        # administrator reloads and decides against the value now stored.
        try:
            with transaction.atomic():
                row = AppSetting.objects.create(key=key, value=stored_value)
        except IntegrityError:
            raise ValidationError(
                f"{spec.label} was changed at the same time by another "
                "change. Reload the page and try again."
            ) from None
    else:
        row.value = stored_value
        row.save(update_fields=["value", "updated_at"])

    # old_value is null for the first change to a key, and deliberately so,
    # even when the switch was effectively on beforehand through the
    # pre-import environment bootstrap. The history records what the store
    # held, and before this row the store held nothing. What the deployment
    # was running at that moment is the system-import row's job to say, and
    # on a normal deployment that row is written first.
    AppSettingChange.objects.create(
        key=key,
        old_value=old_value,
        new_value=stored_value,
        reason=clean_reason,
        actor=actor,
        source=AppSettingChange.Source.ADMIN,
    )
    # Drop the cached read now so anything later in this transaction sees the
    # new value, and again after commit so a concurrent reader that re-cached
    # the old value in the meantime does not keep it.
    runtime.invalidate(key)
    transaction.on_commit(lambda: runtime.invalidate(key))
    audit(
        "configuration.setting_changed",
        target=key,
        actor=actor,
        ip_address=ip_address,
        old_value=old_value,
        new_value=stored_value,
        reason=clean_reason,
        # What this change quietly stopped. Only a switch has dependents, and
        # only turning one off can make them inert.
        now_inert=[
            dependent
            for dependent in dependents_of(key)
            if spec.type == values.BOOLEAN
            and not new_value
            and runtime.get_uncached(dependent)
        ],
    )
    # After commit, so the check runs against the value that was actually
    # stored, and so a slow or unreachable provider cannot hold a write
    # transaction open.
    transaction.on_commit(lambda: verification.verify(key, new_value))
    return row


@transaction.atomic
def set_secret(
    key: str,
    value,
    *,
    actor,
    reason: str,
    ip_address=None,
) -> AppSecret | None:
    """Install or clear one write-only setting.

    The same door as :func:`set_value` in every way that matters — a named
    Application Admin, a substantive reason, an append-only history row, an
    audit row — and different in exactly one: nothing written here records
    the value. The history says a secret was replaced and what its
    fingerprint became; it never says what it is, and there is no read path
    that would let anyone ask later.

    A blank value clears the secret. That is a real thing to want (an
    incident, a decommissioned integration) and it is not the same as never
    having set one, so it is recorded as its own change.
    """
    spec = spec_for(key)
    if spec.type != values.SECRET:
        raise ValidationError(f"{spec.label} is not a secret.")
    plaintext = _coerce(spec, value)
    clean_reason = _clean_reason(reason)
    if actor is None:
        raise ValidationError("A setting change requires a named actor.")
    from apps.api.identity import StaffRole
    from apps.records.services import _require_current_staff

    actor = _require_current_staff(
        actor=actor,
        message="Application Admin authority is required to change a district setting.",
        roles=(StaffRole.ADMIN,),
    )

    _lock_sign_in_rows(key)
    row = AppSecret.objects.select_for_update().filter(key=key).first()
    old_fingerprint = row.fingerprint if row is not None else ""

    _check_no_lockout(
        key,
        resolve=_resolver(),
        resolve_secret=_secret_resolver(key, bool(plaintext)),
    )

    if plaintext:
        new_fingerprint = secrets.fingerprint(plaintext)
        if row is not None and new_fingerprint == old_fingerprint:
            raise ValidationError(f"{spec.label} is already set to that value.")
        ciphertext = secrets.encrypt(plaintext)
        if row is None:
            try:
                with transaction.atomic():
                    row = AppSecret.objects.create(
                        key=key,
                        ciphertext=ciphertext,
                        fingerprint=new_fingerprint,
                        set_by=actor,
                    )
            except IntegrityError:
                raise ValidationError(
                    f"{spec.label} was changed at the same time by another "
                    "change. Reload the page and try again."
                ) from None
        else:
            row.ciphertext = ciphertext
            row.fingerprint = new_fingerprint
            row.set_by = actor
            row.save(
                update_fields=["ciphertext", "fingerprint", "set_by", "updated_at"]
            )
        new_state = {"set": True, "fingerprint": new_fingerprint}
    else:
        if row is None or not row.ciphertext:
            raise ValidationError(f"{spec.label} is not set.")
        # Emptied, not deleted. A deleted row is indistinguishable from one
        # that never existed, and "never existed" is what sends the read
        # path back to the deployed environment -- which on this deployment
        # still holds the credential the administrator just cleared. The row
        # is the record that clearing happened.
        row.ciphertext = ""
        row.fingerprint = ""
        row.set_by = actor
        row.save(update_fields=["ciphertext", "fingerprint", "set_by", "updated_at"])
        new_state = {"set": False, "fingerprint": ""}

    old_state = (
        {"set": True, "fingerprint": old_fingerprint}
        if old_fingerprint
        else {"set": False, "fingerprint": ""}
    )
    AppSettingChange.objects.create(
        key=key,
        old_value=old_state,
        new_value=new_state,
        reason=clean_reason,
        actor=actor,
        source=AppSettingChange.Source.ADMIN,
    )
    audit(
        "configuration.secret_changed",
        target=key,
        actor=actor,
        ip_address=ip_address,
        old_value=old_state,
        new_value=new_state,
        reason=clean_reason,
    )
    return row


@transaction.atomic
def import_environment_value(key: str, value) -> tuple[AppSetting, bool]:
    """Write the deployed environment value down, once, as the stored value.

    Idempotent: a key that already has a row is left exactly as it is, so a
    redeploy never re-imports an environment value over an administrator's
    decision. Dependencies are not enforced -- the point is to preserve what
    the deployment is already doing, not to second-guess it -- but the import
    command logs any imported value the Settings page would have refused.
    """
    spec = spec_for(key)
    if spec.type == values.SECRET:
        raise ValidationError(
            f"{spec.key} is a secret; import it with import_environment_secret."
        )
    stored_value = values.to_json(spec, _coerce(spec, value))
    # get_or_create, not exists-then-create: an administrator's first change
    # to this key may be inserting it concurrently, and losing that race
    # means their decision already stands -- not a crash on the unique key.
    row, created = AppSetting.objects.get_or_create(
        key=key, defaults={"value": stored_value}
    )
    if not created:
        return row, False

    AppSettingChange.objects.create(
        key=key,
        old_value=None,
        new_value=stored_value,
        reason=(
            "System import of the value this deployment was already running "
            "when settings moved into the application."
        ),
        actor=None,
        source=AppSettingChange.Source.SYSTEM_IMPORT,
    )
    runtime.invalidate(key)
    transaction.on_commit(lambda: runtime.invalidate(key))
    audit(
        "configuration.setting_imported",
        target=key,
        actor=None,
        value=stored_value,
    )
    return row, True


@transaction.atomic
def import_environment_secret(key: str, value) -> tuple[AppSecret | None, bool]:
    """Write the deployed secret down, once, encrypted.

    Idempotent for the same reason the ordinary import is: a redeploy must
    never put an environment value back over an administrator's rotation.
    A blank deployed value imports nothing at all -- there is no such thing
    as importing the absence of a credential, and writing a row for it would
    make the first real rotation look like a replacement.
    """
    spec = spec_for(key)
    if spec.type != values.SECRET:
        raise ValidationError(f"{spec.key} is not a secret.")
    plaintext = _coerce(spec, value)
    if not plaintext:
        return None, False
    row = AppSecret.objects.filter(key=key).first()
    if row is not None:
        return row, False
    fingerprint = secrets.fingerprint(plaintext)
    try:
        with transaction.atomic():
            row = AppSecret.objects.create(
                key=key,
                ciphertext=secrets.encrypt(plaintext),
                fingerprint=fingerprint,
                set_by=None,
            )
    except IntegrityError:
        return AppSecret.objects.filter(key=key).first(), False

    AppSettingChange.objects.create(
        key=key,
        old_value=None,
        new_value={"set": True, "fingerprint": fingerprint},
        reason=(
            "System import of the credential this deployment was already "
            "running when settings moved into the application."
        ),
        actor=None,
        source=AppSettingChange.Source.SYSTEM_IMPORT,
    )
    audit(
        "configuration.secret_imported",
        target=key,
        actor=None,
        value={"set": True, "fingerprint": fingerprint},
    )
    return row, True
