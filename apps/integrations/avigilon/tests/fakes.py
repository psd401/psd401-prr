"""A fake Aware tenant at the ``requests.Session`` layer, and the test base
that wires it in.

Every ``requests.Session()`` the client builds is a new ``FakeSession`` bound
to one shared ``FakeAware``, which is how a test stands in for several
processes (web, Celery workers, ``verify_alta``) talking to one tenant: the
tenant remembers which session cookies it issued, and nothing else is shared
between sessions except the Django cache the client itself uses.

Responses are real ``requests.Response`` objects, so status codes, headers,
cookies, and ``.json()`` decoding behave as they do against the network.
"""

import itertools
import json
import logging
import traceback
from typing import Any
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from requests.cookies import RequestsCookieJar

from apps.integrations.avigilon import client

BASE = "https://example.us4.aware.avasecurity.com"
API = f"{BASE}/api/v1"
USERNAME = "prr-holds@example.org"
PASSWORD = "correct-horse-battery-staple-9"

ALTA_SETTINGS = {
    "ALTA_API_BASE": BASE,
    "ALTA_API_USERNAME": USERNAME,
    "ALTA_API_PASSWORD": PASSWORD,
}

GROUP_A = "00000000-0000-4000-8000-0000000000a1"
GROUP_B = "00000000-0000-4000-8000-0000000000b2"
GROUP_EMPTY = "00000000-0000-4000-8000-0000000000e3"
SERVER_GROUP = "00000000-0000-4000-8000-00000000c10d"


def response(status, body=None, *, content_type=None, cookies=None, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp.encoding = "utf-8"
    if body is None:
        resp._content = b""
    elif isinstance(body, bytes):
        resp._content = body
    elif isinstance(body, str):
        resp._content = body.encode("utf-8")
        resp.headers["Content-Type"] = content_type or "text/plain; charset=utf-8"
    else:
        resp._content = json.dumps(body).encode("utf-8")
        resp.headers["Content-Type"] = content_type or "application/json"
    if content_type and body is None:
        resp.headers["Content-Type"] = content_type
    for name, value in (headers or {}).items():
        resp.headers[name] = value
    for name, value in (cookies or {}).items():
        resp.cookies.set(name, value)
    return resp


def json_not_found():
    return response(404, {"kind": "NOT_FOUND", "code": "NO_RESULTS"})


def text_not_found():
    return response(404, "404 page not found")


def head(sid, *, inactive=False, storing: str | None = "smart_storing"):
    """One sensor head: its ``sources`` config and its live record."""
    return sid, {"inactive": inactive}, {"source": sid, "storing_info": storing}


def device(
    guid,
    name,
    *,
    group=GROUP_A,
    heads=None,
    active=True,
    status="CONNECTED",
    pruned_to: str | None = "2026-10-26T20:37:42.086Z",
):
    heads = heads if heads is not None else [head("1")]
    return {
        "guid": guid,
        "name": name,
        "device_group_id": group,
        "server_group_id": SERVER_GROUP,
        "active": active,
        "pruned_to": pruned_to,
        "source_ids": [sid for sid, _config, _live in heads],
        "sources": {
            sid: {"config": {"id": f"cfg-{sid}", "source_id": sid, **config}}
            for sid, config, _live in heads
        },
        "live": {
            "status": status,
            "source_live": [live for _sid, _config, live in heads],
        },
    }


def groups():
    return [
        {"id": GROUP_A, "name": "SITE-A", "parent_id": None},
        {"id": GROUP_B, "name": "SITE-B", "parent_id": None},
        {"id": GROUP_EMPTY, "name": "SITE-EMPTY", "parent_id": None},
    ]


class FakeAware:
    """The tenant: issued cookies, routes, and a log of every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.logins = 0
        self.valid_cookies = set()
        self.login_response: Any = None
        self.sessions = []
        self._cookie_numbers = itertools.count(1)

    # -- wiring -------------------------------------------------------------

    def new_session(self, *args, **kwargs):
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def on(self, method, path, *responses):
        """Answer ``method path`` with each response in turn; the last repeats.

        A response may be a ``requests.Response``, a callable taking the call
        record and returning one, or a JSON-able body (status 200).
        """
        self.routes[(method, path)] = list(responses)

    # -- inspection ---------------------------------------------------------

    def calls_to(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]

    def api_calls(self):
        return [c for c in self.calls if c["path"] != "/dologin"]

    # -- the tenant ---------------------------------------------------------

    def handle(self, method, url, kwargs):
        if not url.startswith(API + "/"):
            raise AssertionError(f"request outside the API prefix: {url}")
        path = url[len(API):]
        record = {
            "method": method,
            "path": path,
            "json": kwargs.get("json"),
            "cookie": (kwargs.get("cookies") or {}).get("va"),
            "kwargs": kwargs,
        }
        self.calls.append(record)
        if path == "/dologin":
            return self._login(record)
        if record["cookie"] not in self.valid_cookies:
            return response(401)
        queue = self.routes.get((method, path))
        if not queue:
            return text_not_found()
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(answer):
            answer = answer(record)
        if isinstance(answer, BaseException):
            raise answer
        if not isinstance(answer, requests.Response):
            answer = response(200, answer)
        return answer

    def issue_cookie(self, value=None):
        value = value or f"session-cookie-{next(self._cookie_numbers)}"
        self.valid_cookies.add(value)
        return value

    def expire_all_sessions(self):
        self.valid_cookies.clear()

    def _login(self, record):
        self.logins += 1
        if self.login_response is not None:
            answer = self.login_response
            if isinstance(answer, BaseException):
                raise answer
            return answer
        body = record["json"] or {}
        if body.get("username") != USERNAME or body.get("password") != PASSWORD:
            return response(401)
        return response(200, cookies={"va": self.issue_cookie()})


class FakeSession:
    def __init__(self, tenant):
        self.tenant = tenant
        self.cookies = RequestsCookieJar()
        self.closed = False

    def request(self, method, url, **kwargs):
        if kwargs.get("allow_redirects") is not False:
            raise AssertionError(f"{method} {url} did not disable redirects")
        resp = self.tenant.handle(method, url, kwargs)
        # As requests does: a Set-Cookie lands in the session's own jar.
        self.cookies.update(resp.cookies)
        return resp

    def close(self):
        self.closed = True


class CapturedLogs(logging.Handler):
    """Every record any logger emits while installed, at every level."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        formatter = logging.Formatter()
        parts = []
        for record in self.records:
            parts.append(record.getMessage())
            if record.exc_info:
                parts.append(formatter.formatException(record.exc_info))
        return "\n".join(parts)


def exception_text(exc):
    return "\n".join([str(exc), repr(exc), *traceback.format_exception(exc)])


@override_settings(**ALTA_SETTINGS)
class TenantTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.tenant = FakeAware()
        patcher = mock.patch.object(
            client.requests, "Session", side_effect=self.tenant.new_session
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(client, "_sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.tenant.on("GET", "/deviceGroups", groups())
        self.tenant.on(
            "GET", "/devices", [device("dev-1", "Front door", heads=[head("1")])]
        )

    def capture_logs(self):
        handler = CapturedLogs()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        def restore():
            root.removeHandler(handler)
            root.setLevel(previous)

        self.addCleanup(restore)
        return handler
