# ABOUTME: Hosted MCP auth pass-through and in-process Django REST adapter.
# ABOUTME: The caller's token remains the only authority and is never logged.
from __future__ import annotations

import logging

import anyio
import httpx
from django.conf import settings
from django.core.wsgi import get_wsgi_application
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from .client import PRRClient, StaffAPIError
from .tools import create_mcp_server

logger = logging.getLogger(__name__)
_SCOPE_CLIENT_KEY = "prr.hosted_mcp_client"
_django_wsgi_application = get_wsgi_application()


def _token_from_authorization(authorization):
    """Parse the token scheme exactly as Django's token auth does."""
    parts = authorization.split()
    if (
        len(parts) != 2
        or parts[0].lower() != "token"
        or not parts[1]
    ):
        return None
    return parts[1]


class DjangoPRRClient(PRRClient):
    """Run PRRClient calls through Django's real URL/middleware/auth stack.

    Calling the in-process WSGI handler avoids a public loopback request (and
    four-worker self-deadlock) while preserving the REST API as the sole
    authorization and serialization boundary.
    """

    def __init__(
        self,
        *,
        authorization,
        host,
        secure,
        remote_addr,
        trusted_proxy_headers,
    ):
        self.authorization = authorization
        token = _token_from_authorization(authorization)
        self._uses_personal_token = bool(
            token and token.startswith("prr_pat_")
        )
        self.host = host
        self.secure = secure
        self.remote_addr = remote_addr
        internal_headers = {
            "Accept": "application/json",
            "Authorization": authorization,
            "Host": host,
        }
        internal_headers.update(trusted_proxy_headers)
        self.client = httpx.Client(
            transport=httpx.WSGITransport(
                app=_django_wsgi_application,
                remote_addr=remote_addr or "127.0.0.1",
            ),
            base_url=(
                "https://internal-prr.invalid"
                if secure
                else "http://internal-prr.invalid"
            ),
            headers=internal_headers,
            follow_redirects=False,
        )

    @staticmethod
    def _security_from_scope(scope, headers):
        """Mirror Django's explicitly configured proxy-scheme trust."""
        secure = scope.get("scheme") == "https"
        trusted_proxy_headers = {}
        proxy_ssl_header = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
        if proxy_ssl_header is not None:
            meta_name, secure_value = proxy_ssl_header
            header_name = meta_name.removeprefix("HTTP_").replace("_", "-")
            header_value = headers.get(header_name)
            if header_value is not None:
                first_value = header_value.split(",", 1)[0].strip()
                secure = first_value == secure_value
                trusted_proxy_headers[header_name] = header_value
        return secure, trusted_proxy_headers

    @classmethod
    def from_scope(cls, scope):
        existing = scope.get(_SCOPE_CLIENT_KEY)
        if existing is not None:
            return existing
        headers = Headers(scope=scope)
        client = scope.get("client")
        remote_addr = client[0] if client else None
        secure, trusted_proxy_headers = cls._security_from_scope(
            scope,
            headers,
        )
        instance = cls(
            authorization=headers.get("authorization", ""),
            host=headers.get("host", ""),
            secure=secure,
            remote_addr=remote_addr,
            trusted_proxy_headers=trusted_proxy_headers,
        )
        scope[_SCOPE_CLIENT_KEY] = instance
        return instance

    def _get(self, path, params=None):
        response = self.client.get(
            path,
            params=params,
        )
        if not 200 <= response.status_code < 300:
            raise StaffAPIError(response.status_code)
        return response.json()

    def _post(self, path, body):
        response = self.client.post(
            path,
            json=body,
        )
        if not 200 <= response.status_code < 300:
            raise StaffAPIError(response.status_code)
        return response.json()

    def close(self):
        self.client.close()


def _transport_security():
    allowed_hosts = []
    allowed_origins = []
    for configured in settings.ALLOWED_HOSTS:
        configured = configured.strip().lower()
        if not configured or configured == "*":
            continue
        if configured.startswith("."):
            domain = configured[1:]
            host_patterns = (domain, f"{domain}:*", f"*.{domain}", f"*.{domain}:*")
        else:
            host_patterns = (configured, f"{configured}:*")
        allowed_hosts.extend(host_patterns)
        schemes = ("https", "http") if settings.DEBUG else ("https",)
        allowed_origins.extend(
            f"{scheme}://{host_pattern}"
            for scheme in schemes
            for host_pattern in host_patterns
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


def _client_for_context(ctx):
    request = ctx.request_context.request
    if request is None:
        raise RuntimeError("Hosted MCP tools require an HTTP request.")
    return DjangoPRRClient.from_scope(request.scope)


def create_hosted_mcp():
    """Create the hosted server: shared reads plus the gated write tools."""
    return create_mcp_server(_client_for_context, include_write_tools=True)


def create_hosted_http_app(mcp):
    """Its stateless JSON transport, suitable for multi-worker ASGI mounting."""
    return mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(),
    )


class StaffTokenAuthenticationMiddleware:
    """Require a Django-authorized read credential before MCP processing."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _unauthorized():
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": "Authentication required.",
                },
                "id": None,
            },
            status_code=401,
            headers={
                "WWW-Authenticate": "Token",
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @staticmethod
    def _unavailable():
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": "Authentication service unavailable.",
                },
                "id": None,
            },
            status_code=503,
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        if _token_from_authorization(authorization) is None:
            logger.warning("Hosted MCP authentication rejected.")
            await self._unauthorized()(scope, receive, send)
            return
        client = DjangoPRRClient.from_scope(scope)
        try:
            # This safe REST primitive is available to attributed staff tokens
            # and the separately scoped read-only service identity. Discarding
            # its response validates auth before the MCP handshake or tool list.
            await anyio.to_thread.run_sync(client.list_capabilities)
        except StaffAPIError as exc:
            client.close()
            if exc.status_code >= 500:
                logger.error(
                    "Hosted MCP authentication service unavailable "
                    "(status=%s).",
                    exc.status_code,
                )
                await self._unavailable()(scope, receive, send)
            else:
                logger.warning(
                    "Hosted MCP authentication rejected by the staff API "
                    "(status=%s).",
                    exc.status_code,
                )
                await self._unauthorized()(scope, receive, send)
            return
        except Exception:  # noqa: BLE001 - an auth-service fault denies the request
            logger.error("Hosted MCP authentication service failed.")
            client.close()
            await self._unavailable()(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Robots-Tag"] = "noindex, nofollow"
                headers["Permissions-Policy"] = (
                    "camera=(), geolocation=(), microphone=(), "
                    "payment=(), usb=()"
                )
            await send(message)

        try:
            await self.app(scope, receive, secure_send)
        finally:
            client.close()
