# ABOUTME: The one place a district setting's value is parsed, validated, and
# ABOUTME: normalised — for the save path, the store read, and the env import.
"""Every managed setting has exactly one idea of what a valid value is.

Before Phase B these bounds lived in ``config/settings.py`` and fired at
process start: an out-of-range value crash-looped the container, and nobody
could see the limit without shell access. The bounds did not move so much as
change when they apply. They are now enforced *on save*, against a named
Application Admin who is told the limit in the same breath as the refusal.

The same function does three jobs so the three can never drift:

* the save path validates what an administrator typed,
* the read path coerces the JSON the store gives back,
* the one-time environment import parses the string the operator's
  ``.env.app`` supplied.

That is why :func:`coerce` accepts a string for every type. A JSON column
round-trips an integer as an integer and a list as a list, but the
environment only ever has text, and a settings page posting "3600" for a
seconds field is the same intent as posting ``3600``.

Failures raise :class:`ValueError` with a sentence an administrator can act
on. Callers turn it into whatever their layer needs -- the save path into a
``ValidationError``, the read path into the registry default.
"""

import datetime as dt
import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email

#: Types a :class:`~apps.configuration.registry.SettingSpec` may declare.
BOOLEAN = "boolean"
INTEGER = "integer"
TEXT = "text"
EMAIL = "email"
DOMAIN = "domain"
DOMAIN_LIST = "domain_list"
EMAIL_LIST = "email_list"
RATE = "rate"
CHOICE = "choice"
SECRET = "secret"
DATE_LIST = "date_list"

TYPES = frozenset(
    {
        BOOLEAN,
        INTEGER,
        TEXT,
        EMAIL,
        DOMAIN,
        DOMAIN_LIST,
        EMAIL_LIST,
        RATE,
        CHOICE,
        SECRET,
        DATE_LIST,
    }
)

#: The value types whose Python representation is a tuple of strings.
LIST_TYPES = frozenset({DOMAIN_LIST, EMAIL_LIST, DATE_LIST})

#: The final label is at least two characters, as a real top-level domain is
#: and as Django's email validator requires. That is what lets a reply domain
#: on its own guarantee a deliverable reply address.
_DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]",
    flags=re.ASCII,
)

#: DRF spells a throttle rate ``<number>/<period>``. The ceiling matches the
#: one ``config/settings.py`` enforced at import: a rate is a brake, and a
#: five-digit one is a typo rather than a decision.
_RATE_PATTERN = re.compile(r"([1-9][0-9]{0,4})/([a-z]+)", flags=re.ASCII)
_RATE_MAX_REQUESTS = 10000

#: A calendar date exactly as a district writes one: YYYY-MM-DD.
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", flags=re.ASCII)

#: Periods DRF's ``SimpleRateThrottle`` understands, by first letter.
RATE_PERIODS = ("s", "sec", "m", "min", "h", "hour", "d", "day")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        # A bool is an int in Python and "True" is not a value any text
        # setting means; refuse rather than silently store "True".
        raise ValueError("A true/false value is not valid here.")
    return str(value).strip()


def _split(value) -> list[str]:
    """One list, whether it arrived as JSON, a tuple, or a comma-separated line."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        raise ValueError("Enter a comma-separated list.")
    return [str(item).strip() for item in items]


def _boolean(spec, value, *, strict=False):
    if isinstance(value, bool):
        return value
    if strict:
        raise ValueError(f"{spec.label} is a true/false setting.")
    normalized = str(value).strip().lower()
    if normalized in {"true", "false"}:
        return normalized == "true"
    raise ValueError(f"{spec.label} is a true/false setting.")


def _integer(spec, value, *, strict=False):
    if isinstance(value, bool):
        raise ValueError(f"{spec.label} is a number, not a true/false setting.")
    if strict and not isinstance(value, int):
        raise ValueError(f"{spec.label} is a number.")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{spec.label} must be a whole number between "
            f"{spec.minimum} and {spec.maximum}."
        ) from exc
    if not spec.minimum <= parsed <= spec.maximum:
        raise ValueError(
            f"{spec.label} must be between {spec.minimum} and {spec.maximum}."
        )
    return parsed


def _text(spec, value):
    text = _as_text(value)
    if not text:
        if spec.allow_blank:
            return ""
        raise ValueError(f"{spec.label} may not be empty.")
    if len(text) > spec.max_length:
        raise ValueError(
            f"{spec.label} may be at most {spec.max_length} characters."
        )
    if spec.pattern and re.fullmatch(spec.pattern, text, flags=re.ASCII) is None:
        raise ValueError(spec.format_message or f"{spec.label} is not in a valid form.")
    return text


def _one_email(label, supplied):
    address = str(supplied).strip().lower()
    try:
        validate_email(address)
    except DjangoValidationError as exc:
        raise ValueError(f"{label} must be an email address.") from exc
    return address


def _email(spec, value):
    text = _as_text(value)
    if not text:
        if spec.allow_blank:
            return ""
        raise ValueError(f"{spec.label} may not be empty.")
    if len(text) > spec.max_length:
        raise ValueError(
            f"{spec.label} may be at most {spec.max_length} characters."
        )
    return _one_email(spec.label, text)


def _one_domain(label, supplied):
    # A leading "@" is accepted and dropped, as the Google-group domain
    # parser always did. "@psd401.net" can only mean the domain psd401.net,
    # so refusing it would be a format lecture rather than a safety check --
    # and the value is normalised before it is stored either way.
    domain = str(supplied).strip().lower().lstrip("@")
    if _DOMAIN_PATTERN.fullmatch(domain) is None:
        raise ValueError(f"{label} must be an exact DNS domain, such as psd401.net.")
    return domain


def _domain(spec, value):
    text = _as_text(value)
    if not text:
        if spec.allow_blank:
            return ""
        raise ValueError(f"{spec.label} may not be empty.")
    return _one_domain(spec.label, text)


def _unique(items):
    """Order-preserving de-duplication, as the old env parsers did."""
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _domain_list(spec, value):
    domains = _unique(
        _one_domain(spec.label, item) for item in _split(value) if str(item).strip()
    )
    if not domains and not spec.allow_blank:
        raise ValueError(f"{spec.label} needs at least one domain.")
    if len(domains) > spec.max_items:
        raise ValueError(f"{spec.label} may list at most {spec.max_items} domains.")
    return domains


def _email_list(spec, value):
    addresses = _unique(
        _one_email(spec.label, item) for item in _split(value) if str(item).strip()
    )
    if not addresses and not spec.allow_blank:
        raise ValueError(f"{spec.label} needs at least one address.")
    if len(addresses) > spec.max_items:
        raise ValueError(f"{spec.label} may list at most {spec.max_items} addresses.")
    return addresses


def _one_date(label, supplied):
    text = str(supplied).strip()
    try:
        # fromisoformat also accepts "20260622" and ISO week dates such as
        # "2026-W26-3", which is ten characters and names a different day. A
        # district calendar is written YYYY-MM-DD, so that shape is required
        # before the calendar check.
        if _DATE_PATTERN.fullmatch(text) is None:
            raise ValueError
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(
            f"{label}: {text} is not a date in the form YYYY-MM-DD."
        ) from None


def _date_list(spec, value):
    dates = _unique(
        _one_date(spec.label, item) for item in _split(value) if str(item).strip()
    )
    if not dates and not spec.allow_blank:
        raise ValueError(f"{spec.label} needs at least one date.")
    if len(dates) > spec.max_items:
        raise ValueError(f"{spec.label} may list at most {spec.max_items} dates.")
    return dates


def _rate(spec, value):
    text = _as_text(value).lower()
    match = _RATE_PATTERN.fullmatch(text)
    periods = spec.periods or RATE_PERIODS
    if match is None or match.group(2) not in periods:
        raise ValueError(
            f"{spec.label} must be a rate such as 10/hour, using one of: "
            + ", ".join(periods)
            + "."
        )
    if int(match.group(1)) > _RATE_MAX_REQUESTS:
        raise ValueError(
            f"{spec.label} may allow at most {_RATE_MAX_REQUESTS} per period."
        )
    return text


def _choice(spec, value):
    """One of a fixed, named set. Case is normalised, as the old parsers did."""
    text = _as_text(value).lower()
    if not text:
        if spec.allow_blank:
            return ""
        raise ValueError(f"{spec.label} may not be empty.")
    if text not in spec.choices:
        raise ValueError(
            f"{spec.label} must be one of: " + ", ".join(spec.choices) + "."
        )
    return text


def _secret(spec, value):
    """A write-only value.

    Every refusal here is written to be safe to show, log, and store in an
    audit row: it describes the *shape* of what was sent and never the value
    itself. A secret's validation message is read by the same people who may
    not read the secret.
    """
    text = _as_text(value)
    if not text:
        if spec.allow_blank:
            return ""
        raise ValueError(f"{spec.label} may not be empty.")
    if len(text) > spec.max_length:
        raise ValueError(
            f"{spec.label} may be at most {spec.max_length} characters."
        )
    if spec.pattern and re.fullmatch(spec.pattern, text, flags=re.ASCII) is None:
        raise ValueError(spec.format_message or f"{spec.label} is not in a valid form.")
    return text


_COERCERS = {
    BOOLEAN: _boolean,
    INTEGER: _integer,
    TEXT: _text,
    EMAIL: _email,
    DOMAIN: _domain,
    DOMAIN_LIST: _domain_list,
    EMAIL_LIST: _email_list,
    RATE: _rate,
    CHOICE: _choice,
    SECRET: _secret,
    DATE_LIST: _date_list,
}


def coerce(spec, value, *, strict=False):
    """Validate and normalise one value for ``spec``.

    Raises :class:`ValueError` with the sentence the administrator should
    read. Returns the canonical Python value: ``bool``, ``int``, ``str``, or
    a ``tuple`` of strings.

    ``strict`` refuses a string where a ``bool`` or ``int`` is meant. The
    save path passes it, because a caller sending ``"true"`` for a switch is
    a caller who has not understood what it is sending, and a settings store
    is the last place to be generous about that. The read and bootstrap
    paths do not, because JSON round-trips and environment variables both
    legitimately arrive as text.
    """
    try:
        coercer = _COERCERS[spec.type]
    except KeyError as exc:
        raise ValueError(f"Unsupported setting type {spec.type!r}.") from exc
    if spec.type in {BOOLEAN, INTEGER}:
        return coercer(spec, value, strict=strict)
    return coercer(spec, value)


def to_json(spec, value):
    """The canonical value as something ``JSONField`` can store.

    A secret has no such representation on purpose. Its plaintext lives only
    in :class:`~apps.configuration.models.AppSecret`, encrypted, and this
    guard is what makes "a secret ended up in ``AppSetting.value`` or in an
    ``AppSettingChange`` row" a crash rather than a quiet disclosure.
    """
    if spec.type == SECRET:
        raise ValueError(
            f"{spec.key} is a secret; its value is never stored in the "
            "settings store or its history."
        )
    if spec.type in LIST_TYPES:
        return list(value)
    return value
