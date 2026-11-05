"""
Avigilon Alta Video (Aware) adapter: camera-footage preservation holds.

PRESERVATION MODEL. Aware prunes raw footage on a rolling window that differs
by device (read each device's ``pruned_to``; never assume a constant), but
footage inside a BOOKMARK is exempt from pruning until the bookmark is
deleted. A hold is one bookmark per camera covering the requested window:
``expiry_time`` null (kept until deleted by hand) and one ``device_infos``
entry per active sensor head. Creating a bookmark proves nothing about
whether the footage exists, so after the create each head's store timeline
is read and the fraction of the window actually on disk is returned; the
caller decides HELD versus gap from that. Releasing a hold deletes its
bookmark, and only human-gated release or exclusion code calls
:func:`delete_bookmark`.

AUTH. ``POST /api/v1/dologin`` with the service account's username and
password sets the ``va`` session cookie, which lives 24 hours. Aware keeps
three sessions per account and evicts the oldest, so every process (web,
each Celery worker, ``verify_alta``) shares one session through the Django
cache, and a login is serialized by a cache lock. A 401 (empty body) means
the session is gone: sign in again once and retry once. A 403
PERMISSION_DENIED is never retried.

ERROR CONTRACT. Every failure (transport, HTTP status, an undecodable or
unexpected payload, authentication) raises :class:`AltaError`; an empty
result never stands in for an error. Neither the password nor the session
cookie appears in a log record or an exception message.

Check a deployment with ``manage.py verify_alta`` (read-only), then once with
``manage.py verify_alta --smoke-bookmark <device guid>``, which creates and
deletes one 10-minute bookmark.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from secrets import token_hex  # the stdlib module; ``secrets`` below is ours
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import requests
from django.core.cache import cache

from apps.configuration import runtime, secrets

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
SESSION_COOKIE = "va"

#: Aware ends a session 24 hours after login; the shared copy goes first.
SESSION_CACHE_TTL_SECONDS = 20 * 60 * 60
LOGIN_TIMEOUT_SECONDS = 20
#: Outlives one login attempt, so only a dead holder's lock ever expires.
LOGIN_LOCK_TTL_SECONDS = 60
#: Outlives the lock, so a waiter takes over from a holder that died.
LOGIN_WAIT_SECONDS = 75
LOGIN_POLL_SECONDS = 0.25

READ_TIMEOUT_SECONDS = 30
#: The whole device list is one response of several megabytes.
DEVICES_TIMEOUT_SECONDS = 60
WRITE_TIMEOUT_SECONDS = 60
#: Seconds to wait before each retry of an idempotent read on 429 or 5xx.
READ_RETRY_DELAYS = (1, 2, 4)
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Aware refuses timeline windows near 30 days; 6 days is known to work.
TIMELINE_CHUNK = dt.timedelta(days=6)
BOOKMARK_ORIGIN = "ALTA Aware"
SKIPPED_HEAD_ERROR = "head reports no stream; not bookmarked, check by hand"
SMOKE_NAME = "PRR smoke test — delete"
SMOKE_DESCRIPTION = (
    "Created by manage.py verify_alta --smoke-bookmark and deleted by it "
    "within seconds. Safe to delete."
)

# Indirections the tests replace, so no test sleeps or depends on the clock.
_sleep = time.sleep
_monotonic = time.monotonic


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class AltaError(RuntimeError):
    pass


class AltaNotConfigured(AltaError):
    pass


class AltaAuthenticationFailed(AltaError):
    """The account could not sign in, or a fresh session was refused."""


class AltaPermissionDenied(AltaError):
    """403: the account lacks a permission. Never retried."""


class AltaWriteUncertain(AltaError):
    """A create timed out or failed server-side (5xx): it may have landed,
    so the target must be checked by hand before the write is retried."""


class AltaNotFound(AltaError):
    """A JSON 404 NOT_FOUND: the object does not exist.

    Distinct from a text/plain 404, which means the path itself is wrong.
    """


# ---------------------------------------------------------------------------
# The interface the hold and triage code depends on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    id: str                       # Aware device guid
    name: str
    site: str                     # Aware device group name, e.g. "PHS"; "" if none
    device_group_id: str
    server_group_id: str
    status: str                   # live.status, e.g. "CONNECTED", "COMMUNICATION_ERROR"
    pruned_to: dt.datetime | None  # oldest retained footage (aware UTC); None if unknown
    sources: tuple[str, ...]      # active head sids to bookmark (inactive heads skipped)
    # Heads reporting no stream now. Not bookmarked (whether Aware accepts
    # them is unverified), but the window may still hold their footage, so a
    # hold reports each as an uncovered head for a hand check.
    skipped_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceGroup:
    id: str
    name: str                     # PSD site code, e.g. "HHE"


@dataclass(frozen=True)
class HeadCoverage:
    source: str
    covered_fraction: float       # 0.0..1.0 of the requested window found in store
    error: str = ""               # why coverage could not be confirmed, if it could not


@dataclass(frozen=True)
class BookmarkHold:
    id: str                       # Aware bookmark id
    coverage: tuple[HeadCoverage, ...]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Config:
    base: str
    username: str
    password: str = field(repr=False)

    @property
    def api(self) -> str:
        return self.base + API_PREFIX


def _validated_base_url() -> str:
    base_url = (runtime.get("ALTA_API_BASE") or "").strip()
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise AltaError("ALTA_API_BASE must be a valid HTTPS URL.") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AltaError(
            "ALTA_API_BASE must be an HTTPS URL without credentials, a "
            "query string, or a fragment."
        )
    if parsed.path not in ("", "/"):
        raise AltaError(
            "ALTA_API_BASE must be the tenant's HTTPS origin with no path; "
            "the client adds /api/v1 itself."
        )
    normalized_host = parsed.hostname.lower()
    authority = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _config() -> _Config:
    base = (runtime.get("ALTA_API_BASE") or "").strip()
    username = (runtime.get("ALTA_API_USERNAME") or "").strip()
    password = runtime.get_secret("ALTA_API_PASSWORD")
    if not base or not username or not password:
        raise AltaNotConfigured(
            "ALTA_API_BASE, ALTA_API_USERNAME, and ALTA_API_PASSWORD are not "
            "all set; footage holds are created as manual tasks until they are."
        )
    return _Config(_validated_base_url(), username, password)


# ---------------------------------------------------------------------------
# The shared session
# ---------------------------------------------------------------------------


def _cache_id(config: _Config) -> str:
    # Key-bound HMAC of the whole identity: a changed password or tenant is
    # a different session, and the key names neither.
    try:
        return secrets.fingerprint(f"{config.base}\n{config.username}\n{config.password}")
    except Exception:  # noqa: BLE001 - re-raised without the text, which could quote the input
        raise AltaError("The Alta session cache key could not be derived.") from None


def _session_cache_key(config: _Config) -> str:
    return f"prr:alta:session:{_cache_id(config)}"


def _login_lock_key(config: _Config) -> str:
    return f"prr:alta:login-lock:{_cache_id(config)}"


def _cache_op(operation, *args, **kwargs):
    """A cache call; an unusable cache fails closed rather than logging in
    per process, which would evict the other processes' sessions."""
    try:
        return operation(*args, **kwargs)
    except Exception:  # noqa: BLE001 - any cache backend error fails closed
        raise AltaError(
            "The shared Alta session cache is unavailable; not signing in."
        ) from None


def _cached_cookie(config: _Config) -> str:
    sealed = _cache_op(cache.get, _session_cache_key(config))
    if not sealed:
        return ""
    try:
        return secrets.decrypt(sealed)
    except secrets.SecretUnreadable:
        return ""


def _usable(cookie: str, stale: str) -> bool:
    return bool(cookie) and cookie != stale


def _session_cookie(http: requests.Session, config: _Config, *, stale: str = "") -> str:
    """The shared session cookie, signing in only if there is no usable one.

    ``stale`` is a cookie the tenant just refused. Another process may
    already have replaced it; only if the cache still holds it (or nothing)
    does this process sign in, and then only while holding the lock.
    """
    cookie = _cached_cookie(config)
    if _usable(cookie, stale):
        return cookie
    lock = _login_lock_key(config)
    token = token_hex(16)
    deadline = _monotonic() + LOGIN_WAIT_SECONDS
    while True:
        if _cache_op(cache.add, lock, token, LOGIN_LOCK_TTL_SECONDS):
            try:
                cookie = _cached_cookie(config)
                if _usable(cookie, stale):
                    return cookie
                cookie = _login(http, config)
                _cache_op(
                    cache.set,
                    _session_cache_key(config),
                    secrets.encrypt(cookie),
                    timeout=SESSION_CACHE_TTL_SECONDS,
                )
                return cookie
            finally:
                _release_login_lock(lock, token)
        if _monotonic() >= deadline:
            raise AltaError("Timed out waiting for another process to sign in to Alta.")
        _sleep(LOGIN_POLL_SECONDS)
        cookie = _cached_cookie(config)
        if _usable(cookie, stale):
            return cookie


def _release_login_lock(lock: str, token: str) -> None:
    """Delete the login lock only while it still holds this process's token.

    If this holder outlived the lock's TTL, another process may have taken
    the lock since, and deleting it would let a third process sign in beside
    that one. The get and the delete are two cache calls, not one atomic
    compare-and-delete, so the lock can still expire and be retaken in the
    gap between them. That race is accepted: the TTL outlives a login, so it
    needs a holder at the very end of its TTL, and the worst outcome is one
    extra sign-in, which costs a session slot, not a hold.
    """
    try:
        if cache.get(lock) == token:
            cache.delete(lock)
    except Exception:  # noqa: BLE001 - the lock expires on its own
        logger.warning("Could not release the Alta login lock; it expires on its own.")


def _reject_redirect(response) -> None:
    if 300 <= response.status_code < 400:
        raise AltaError("Alta API redirects are not allowed.")


def _login(http: requests.Session, config: _Config) -> str:
    try:
        response = http.request(
            "POST",
            config.api + "/dologin",
            json={"username": config.username, "password": config.password},
            timeout=LOGIN_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # Never chain: a transport error can quote the request.
        raise AltaError(f"Alta sign-in failed ({type(exc).__name__}).") from None
    finally:
        # The cookie is sent explicitly from the shared cache. A copy left
        # in this session's jar would go out beside it, possibly stale.
        http.cookies.clear()
    _reject_redirect(response)
    if response.status_code in (401, 403):
        raise AltaAuthenticationFailed(
            f"Alta refused the service account's sign-in (HTTP "
            f"{response.status_code}). Check ALTA_API_USERNAME and "
            "ALTA_API_PASSWORD."
        )
    if response.status_code != 200:
        raise AltaError(f"Alta sign-in returned HTTP {response.status_code}.")
    cookie = response.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise AltaError("Alta sign-in succeeded but set no session cookie.")
    logger.info("Signed in to Alta; the session is shared through the cache.")
    return cookie


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _error_body(response) -> tuple[str, str]:
    if "json" not in response.headers.get("Content-Type", "").lower():
        return "", ""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    return str(body.get("kind") or ""), str(body.get("message") or "")[:200]


def _status_error(response, where: str) -> AltaError:
    status = response.status_code
    kind, message = _error_body(response)
    if status == 403:
        return AltaPermissionDenied(
            f"{where} was refused: the Alta service account lacks permission "
            f"(HTTP 403{' ' + kind if kind else ''})."
        )
    if status == 404 and kind == "NOT_FOUND":
        return AltaNotFound(f"{where}: not found.")
    if status == 404:
        return AltaError(f"{where} returned HTTP 404 for the path itself, not a JSON NOT_FOUND.")
    detail = f" {kind}" if kind else ""
    if message:
        detail += f": {message}"
    return AltaError(f"{where} returned HTTP {status}{detail}.")


def _decode(response, where: str) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise AltaError(f"{where} returned a body that is not JSON.") from None


class _Api:
    """One operation's requests, all on the shared session."""

    def __init__(self, http: requests.Session, config: _Config):
        self._http = http
        self._config = config
        self._cookie = ""

    @property
    def base(self) -> str:
        return self._config.base

    def get(self, path: str, *, timeout: float = READ_TIMEOUT_SECONDS) -> Any:
        return self._call("GET", path, timeout=timeout, retry_transient=True)

    def read_post(self, path: str, body: dict) -> Any:
        """A POST that only reads, so it is as safe to retry as a GET."""
        return self._call("POST", path, body=body, retry_transient=True)

    def create(self, path: str, body: dict) -> tuple[Any, str]:
        """POST a create; return its decoded body and its Location header.

        Never retried on 5xx or a timeout: the write may have landed, and a
        second create would be a second bookmark. Those failures raise
        :class:`AltaWriteUncertain`."""
        response = self._call(
            "POST",
            path,
            body=body,
            timeout=WRITE_TIMEOUT_SECONDS,
            ok=(200, 201),
            raw=True,
            may_land=True,
        )
        return _decode(response, f"Alta POST {path}"), response.headers.get("Location", "")

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path, timeout=WRITE_TIMEOUT_SECONDS, ok=(200, 204))

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        timeout: float = READ_TIMEOUT_SECONDS,
        retry_transient: bool = False,
        ok: tuple[int, ...] = (200,),
        raw: bool = False,
        may_land: bool = False,
    ) -> Any:
        """``may_land``: the request is a write that a timeout or a 5xx does
        not prove was refused; those raise :class:`AltaWriteUncertain`."""
        where = f"Alta {method} {path}"
        transport_error = AltaWriteUncertain if may_land else AltaError
        if not self._cookie:
            self._cookie = _session_cookie(self._http, self._config)
        delays = iter(READ_RETRY_DELAYS if retry_transient else ())
        signed_in_again = False
        while True:
            try:
                response = self._http.request(
                    method,
                    self._config.api + path,
                    json=body,
                    cookies={SESSION_COOKIE: self._cookie},
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise transport_error(f"{where} failed ({type(exc).__name__}).") from None
            _reject_redirect(response)
            status = response.status_code
            if status == 401:
                # Refused before it ran, so resending cannot duplicate a write.
                if signed_in_again:
                    raise AltaAuthenticationFailed(f"{where} refused a fresh Alta session (HTTP 401).")
                signed_in_again = True
                self._cookie = _session_cookie(self._http, self._config, stale=self._cookie)
                continue
            if status in _TRANSIENT_STATUSES:
                delay = next(delays, None)
                if delay is not None:
                    logger.warning("%s returned HTTP %s; retrying in %ss.", where, status, delay)
                    _sleep(delay)
                    continue
            if status in ok:
                return response if raw else _decode(response, where)
            if may_land and status >= 500:
                raise AltaWriteUncertain(str(_status_error(response, where)))
            raise _status_error(response, where)


@contextmanager
def _client() -> Iterator[_Api]:
    config = _config()
    http = requests.Session()
    # Ignore HTTP(S)_PROXY and .netrc from the host: neither may route or
    # authenticate this traffic, and the password goes over the sign-in.
    http.trust_env = False
    try:
        yield _Api(http, config)
    finally:
        http.close()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def _parse_time(value: Any) -> dt.datetime:
    """An Aware timestamp (ISO 8601 or epoch milliseconds) as aware UTC."""
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("no timestamp")
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.UTC)
    parsed = dt.datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _pruned_to(value: Any, guid: str) -> dt.datetime | None:
    if value is None or value == "":
        return None
    try:
        return _parse_time(value)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Alta device %s has an unreadable pruned_to; treating it as unknown.", guid)
        return None


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _active_heads(device: dict, live: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The device's own head sids to bookmark, and the streamless ones.

    Inactive heads are retired config and appear in neither. A head that
    reports no stream now is returned second: it is not bookmarked, but the
    requested window may still hold its footage, so it must not vanish.
    Numbering differs by unit even within one model, so it is only ever read
    from ``source_ids`` (or the ``sources`` map), never inferred.
    """
    configs = _as_dict(device.get("sources"))
    listed = device.get("source_ids")
    sids = [str(s) for s in listed] if isinstance(listed, list) and listed else [str(s) for s in configs]
    storing = {
        str(entry["source"]): entry.get("storing_info")
        for entry in live.get("source_live") or []
        if isinstance(entry, dict) and entry.get("source") is not None
    }
    heads: list[str] = []
    skipped: list[str] = []
    for sid in sids:
        if sid in heads or sid in skipped:
            continue
        config = _as_dict(_as_dict(configs.get(sid)).get("config"))
        if config.get("inactive") is True:
            continue
        # An Axis "quad" source is a composite view stitched from the
        # device's other heads, which are bookmarked on their own, and no
        # bookmark on the tenant includes one (2026-11-05). Holding it would
        # add storage and nothing the real heads do not already preserve.
        if config.get("source_name") == "quad":
            continue
        if storing.get(sid) == "no_stream_available":
            skipped.append(sid)
            continue
        heads.append(sid)
    return tuple(heads), tuple(skipped)


def _camera(device: dict, group_names: dict[str, str]) -> Camera:
    guid = str(device.get("guid") or "").strip()
    if not guid:
        raise AltaError("An Alta device record has no guid.")
    live = _as_dict(device.get("live"))
    group_id = str(device.get("device_group_id") or "")
    sources, skipped = _active_heads(device, live)
    return Camera(
        id=guid,
        name=str(device.get("name") or ""),
        site=group_names.get(group_id, ""),
        device_group_id=group_id,
        server_group_id=str(device.get("server_group_id") or ""),
        status=str(live.get("status") or ""),
        pruned_to=_pruned_to(device.get("pruned_to"), guid),
        sources=sources,
        skipped_sources=skipped,
    )


def _groups(api: _Api) -> list[DeviceGroup]:
    payload = api.get("/deviceGroups")
    if not isinstance(payload, list):
        raise AltaError("Alta GET /deviceGroups did not return a list.")
    groups = []
    for record in payload:
        if not isinstance(record, dict) or not record.get("id"):
            raise AltaError("An Alta device group record has no id.")
        groups.append(DeviceGroup(id=str(record["id"]), name=str(record.get("name") or "")))
    return groups


def _inventory(api: _Api) -> tuple[list[Camera], list[DeviceGroup], int]:
    """Active cameras, every device group, and how many devices were
    dropped as ``active: false`` (counted so a check can show them)."""
    groups = _groups(api)
    # One plain GET: offset paging on this endpoint silently drops devices.
    payload = api.get("/devices", timeout=DEVICES_TIMEOUT_SECONDS)
    if not isinstance(payload, list):
        raise AltaError("Alta GET /devices did not return a list.")
    names = {group.id: group.name for group in groups}
    cameras = []
    inactive = 0
    for device in payload:
        if not isinstance(device, dict):
            raise AltaError("An Alta device record is not an object.")
        if device.get("active") is False:
            inactive += 1
            continue
        cameras.append(_camera(device, names))
    return cameras, groups, inactive


def list_device_cameras() -> list[Camera]:
    """Every active device (plain GET /devices; never offset paging)."""
    with _client() as api:
        cameras, _groups_unused, _inactive = _inventory(api)
    return cameras


def list_device_groups() -> list[DeviceGroup]:
    """Every device group (site)."""
    with _client() as api:
        return _groups(api)


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


def _hold_window(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The window in UTC, widened to whole seconds (Aware stores no less)."""
    for value in (start, end):
        if value.tzinfo is None or value.utcoffset() is None:
            raise AltaError("A hold window needs timezone-aware datetimes.")
    if end <= start:
        raise AltaError("A hold window must end after it starts.")
    start_utc = start.astimezone(dt.UTC).replace(microsecond=0)
    end_utc = end.astimezone(dt.UTC)
    if end_utc.microsecond:
        end_utc = end_utc.replace(microsecond=0) + dt.timedelta(seconds=1)
    return start_utc, end_utc


def _iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def _post_bookmark(
    api: _Api,
    camera: Camera,
    start: dt.datetime,
    end: dt.datetime,
    *,
    name: str,
    description: str,
) -> str:
    heads = tuple(dict.fromkeys(camera.sources))
    body = {
        "name": name,
        "description": description,
        "expiry_time": None,
        "origin": BOOKMARK_ORIGIN,
        "able_to_download": True,
        "allow_audio": False,
        "device_infos": [
            {
                "device_id": camera.id,
                "server_group_id": camera.server_group_id,
                "source": sid,
                "start_time": _iso(start),
                "end_time": _iso(end),
            }
            for sid in heads
        ],
    }
    try:
        created, location = api.create("/bookmarks", body)
    except AltaWriteUncertain as exc:
        raise AltaWriteUncertain(
            f"{exc} A bookmark may exist anyway: check Alta device {camera.id} "
            "for it before retrying."
        ) from None
    bookmark_id = _created_bookmark_id(created, location, api_base=api.base)
    if not bookmark_id:
        raise AltaError(
            "Alta accepted the bookmark but returned no id; check the device "
            "for an unlisted hold before trying again."
        )
    logger.info(
        "Created Alta bookmark %s on device %s for %d head(s).",
        bookmark_id,
        camera.id,
        len(heads),
    )
    return bookmark_id


def _created_bookmark_id(created: Any, location: str, *, api_base: str) -> str:
    """The new bookmark's id: from a JSON body if one came back, else from
    Location. The live tenant (2026-11-05) answers 201 with an empty body and
    ``Location: /api/v1/bookmarks/<id>``. Anything else in Location (another
    host, another resource, extra segments, a query) yields no id."""
    if isinstance(created, dict) and str(created.get("id") or "").strip():
        return str(created["id"]).strip()
    parts = urlsplit(location.strip())
    if parts.query or parts.fragment:
        return ""
    if (parts.scheme or parts.netloc) and f"{parts.scheme}://{parts.netloc}".lower() != api_base.lower():
        return ""
    prefix = API_PREFIX + "/bookmarks/"
    if not parts.path.startswith(prefix):
        return ""
    bookmark_id = unquote(parts.path[len(prefix):])
    return "" if not bookmark_id.strip() or "/" in bookmark_id else bookmark_id.strip()


def _refuse_unholdable(camera: Camera) -> None:
    if not camera.sources:
        streamless = len(dict.fromkeys(camera.skipped_sources))
        raise AltaError(
            f"Alta device {camera.id} has no recording head to bookmark"
            + (
                f"; {streamless} head(s) report no stream, check them by hand."
                if streamless
                else "."
            )
        )
    if not camera.server_group_id:
        raise AltaError(f"Alta device {camera.id} has no server group.")


def _covered_seconds(intervals: list[tuple[dt.datetime, dt.datetime]]) -> float:
    covered = 0.0
    reach: dt.datetime | None = None
    for low, high in sorted(intervals):
        if reach is not None and high <= reach:
            continue
        low = max(low, reach) if reach is not None else low
        covered += (high - low).total_seconds()
        reach = high
    return covered


def _covered_fraction(
    api: _Api, camera: Camera, sid: str, start: dt.datetime, end: dt.datetime
) -> float:
    now = _now()
    intervals = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(end, chunk_start + TIMELINE_CHUNK)
        after, before = _ms(chunk_start), _ms(chunk_end)
        payload = api.read_post(
            "/store/timelineInformation",
            {
                "deviceSources": [
                    {"deviceId": camera.id, "serverGroupId": camera.server_group_id, "source": sid}
                ],
                "timeAfter": after,
                "timeBefore": before,
                "timeStep": max(60_000, (before - after) // 100),
                "includeStoreInfo": True,
            },
        )
        if not isinstance(payload, dict):
            raise AltaError("the timeline response is not an object")
        # Only an explicit list confirms anything; an empty one is a real 0%.
        # A missing or null key could not be read, so it is not counted as 0%.
        stores = payload.get("stores")
        if stores is None:
            raise AltaError("the timeline response has no stores list")
        if not isinstance(stores, list):
            raise AltaError("the timeline stores are not a list")
        for store in stores:
            if not isinstance(store, dict):
                raise AltaError("a timeline store is not an object")
            try:
                low = _parse_time(store.get("store_start_time"))
                raw_end = store.get("store_end_time")
                # An open segment is still recording: it reaches now, no further.
                high = _parse_time(raw_end) if raw_end not in (None, "") else min(chunk_end, now)
            except (TypeError, ValueError, OverflowError):
                raise AltaError("a timeline store has an unreadable time") from None
            low, high = max(low, chunk_start), min(high, chunk_end)
            if high > low:
                intervals.append((low, high))
        chunk_start = chunk_end
    total = (end - start).total_seconds()
    return max(0.0, min(1.0, _covered_seconds(intervals) / total))


def _head_coverage(
    api: _Api, camera: Camera, sid: str, start: dt.datetime, end: dt.datetime
) -> HeadCoverage:
    """One head's coverage. Never raises: the bookmark already exists."""
    try:
        return HeadCoverage(sid, _covered_fraction(api, camera, sid, start, end))
    except Exception as exc:  # noqa: BLE001 - reported on the result instead
        reason = str(exc) if isinstance(exc, AltaError) else type(exc).__name__
        logger.warning(
            "Coverage of head %s on Alta device %s could not be read: %s",
            sid,
            camera.id,
            reason,
        )
        return HeadCoverage(sid, 0.0, f"Coverage could not be read: {reason}"[:300])


def _coverage(
    api: _Api, camera: Camera, start: dt.datetime, end: dt.datetime
) -> tuple[HeadCoverage, ...]:
    return tuple(
        _head_coverage(api, camera, sid, start, end) for sid in dict.fromkeys(camera.sources)
    )


def create_bookmark_hold(
    camera: Camera,
    start: dt.datetime,
    end: dt.datetime,
    *,
    name: str,
    description: str,
) -> BookmarkHold:
    """One bookmark for one camera, one device_infos entry per active head,
    expiry_time null (kept until manually deleted). Then confirm per-head
    coverage of [start, end); the caller decides HELD vs gap from it. Each of
    ``camera.skipped_sources`` (streamless now, so not bookmarked) is
    reported as an uncovered head, so it reads as a gap, never as nothing."""
    start_utc, end_utc = _hold_window(start, end)
    _refuse_unholdable(camera)
    with _client() as api:
        bookmark_id = _post_bookmark(
            api, camera, start_utc, end_utc, name=name, description=description
        )
        measured = _coverage(api, camera, start_utc, end_utc)
    skipped = tuple(
        HeadCoverage(sid, 0.0, SKIPPED_HEAD_ERROR)
        for sid in dict.fromkeys(camera.skipped_sources)
        if sid not in camera.sources
    )
    return BookmarkHold(bookmark_id, measured + skipped)


def _bookmark_path(bookmark_id: str) -> str:
    bookmark_id = str(bookmark_id or "").strip()
    if not bookmark_id:
        raise AltaError("No Alta bookmark id was given.")
    return f"/bookmarks/{quote(bookmark_id, safe='')}"


def _get_bookmark(api: _Api, bookmark_id: str) -> dict:
    payload = api.get(_bookmark_path(bookmark_id))
    if not isinstance(payload, dict):
        raise AltaError(f"Alta bookmark {bookmark_id} did not read back as an object.")
    return payload


def _delete_bookmark(api: _Api, bookmark_id: str) -> None:
    try:
        api.delete(_bookmark_path(bookmark_id))
    except AltaNotFound:
        logger.info("Alta bookmark %s was already gone.", bookmark_id)
        return
    logger.info("Deleted Alta bookmark %s.", bookmark_id)


def delete_bookmark(bookmark_id: str) -> None:
    """Remove a hold bookmark. ONLY called by human-gated release/exclusion.
    A JSON NOT_FOUND means already gone; any other failure raises AltaError."""
    _bookmark_path(bookmark_id)  # refuse a blank id before signing in
    with _client() as api:
        _delete_bookmark(api, bookmark_id)


# ---------------------------------------------------------------------------
# verify_alta
# ---------------------------------------------------------------------------


def checks() -> dict:
    """Read-only connectivity and permission check for `manage.py verify_alta`."""
    with _client() as api:
        cameras, groups, inactive = _inventory(api)
    used = {camera.device_group_id for camera in cameras}
    return {
        "ok": True,
        "device_count": len(cameras),
        "inactive_devices": inactive,
        "head_count": sum(len(camera.sources) for camera in cameras),
        "devices_without_heads": sum(1 for camera in cameras if not camera.sources),
        "group_count": len(groups),
        "empty_groups": sorted(group.name for group in groups if group.id not in used),
    }


def _reason(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, AltaError) else type(exc).__name__


def _readback_failures(
    stored: dict, camera: Camera, start: dt.datetime, end: dt.datetime
) -> list[str]:
    failures = []
    if stored.get("expiry_time") is not None:
        failures.append("the stored bookmark has an expiry_time; holds need it null")
    expected = {(camera.id, sid, start, end) for sid in camera.sources}
    entries = stored.get("device_infos")
    found = set()
    for entry in entries if isinstance(entries, list) else []:
        entry = _as_dict(entry)
        try:
            found.add(
                (
                    str(entry.get("device_id") or ""),
                    str(entry.get("source") or ""),
                    _parse_time(entry.get("start_time")),
                    _parse_time(entry.get("end_time")),
                )
            )
        except (TypeError, ValueError, OverflowError):
            failures.append("a stored device_infos entry has an unreadable time")
    if found != expected or not isinstance(entries, list) or len(entries) != len(expected):
        failures.append(
            f"the stored device_infos do not match what was sent "
            f"({len(entries) if isinstance(entries, list) else 0} stored, "
            f"{len(expected)} sent)"
        )
    return failures


def smoke_test_bookmark(device_guid: str) -> dict:
    """Create, read back, measure, and delete one 10-minute bookmark.

    The window ends an hour ago, so the footage should be on disk. The
    bookmark is deleted whatever fails after it is created; a failure to
    delete it is reported with its id so it can be removed by hand.
    Returns ids, counts, and times only.
    """
    device_guid = str(device_guid or "").strip()
    end = _now().replace(microsecond=0) - dt.timedelta(hours=1)
    start = end - dt.timedelta(minutes=10)
    with _client() as api:
        cameras, _groups_unused, _inactive = _inventory(api)
        camera = next((c for c in cameras if c.id == device_guid), None)
        if camera is None:
            raise AltaError(f"No active Alta device has guid {device_guid}.")
        _refuse_unholdable(camera)
        heads = tuple(dict.fromkeys(camera.sources))
        camera = dataclasses.replace(camera, sources=heads)
        bookmark_id = _post_bookmark(
            api, camera, start, end, name=SMOKE_NAME, description=SMOKE_DESCRIPTION
        )
        result: dict[str, Any] = {
            "bookmark_id": bookmark_id,
            "device_id": camera.id,
            "heads": len(heads),
            "start": _iso(start),
            "end": _iso(end),
            "stored_entries": 0,
            "coverage": [],
            "deleted": False,
            "confirmed_gone": False,
            "failures": [],
        }
        failures: list[str] = result["failures"]
        try:
            try:
                stored = _get_bookmark(api, bookmark_id)
                entries = stored.get("device_infos")
                result["stored_entries"] = len(entries) if isinstance(entries, list) else 0
                failures.extend(_readback_failures(stored, camera, start, end))
            except Exception as exc:  # noqa: BLE001 - recorded, then cleaned up
                failures.append(f"read-back failed: {_reason(exc)}")
            result["coverage"] = [
                {"source": c.source, "covered_fraction": c.covered_fraction, "error": c.error}
                for c in _coverage(api, camera, start, end)
            ]
        finally:
            try:
                _delete_bookmark(api, bookmark_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"delete failed: {_reason(exc)}; delete bookmark {bookmark_id} by hand"
                )
            else:
                result["deleted"] = True
                try:
                    _get_bookmark(api, bookmark_id)
                except AltaNotFound:
                    result["confirmed_gone"] = True
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"post-delete check failed: {_reason(exc)}")
                else:
                    failures.append(f"bookmark {bookmark_id} is still readable after the delete")
    result["ok"] = not failures
    return result
