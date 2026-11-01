# ABOUTME: Top-level ASGI composition for Django plus hosted streamable HTTP MCP.
# ABOUTME: This lifespan owns FastMCP's session manager; mounted app lifespans do not run.
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Match, Mount

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_application = get_asgi_application()

from integrations.mcp.hosted import (  # noqa: E402
    StaffTokenAuthenticationMiddleware,
    create_hosted_mcp,
)


class ExactMCPRoute(BaseRoute):
    """Delegate both `/mcp` and `/mcp/` without an auth-header redirect."""

    def __init__(self, app):
        self.app = app

    def matches(self, scope):
        if (
            scope["type"] == "http"
            and scope.get("path") in {"/mcp", "/mcp/"}
        ):
            child_scope = {
                "path": "/",
                "raw_path": b"/",
                "root_path": scope.get("root_path", "") + "/mcp",
            }
            return Match.FULL, child_scope
        return Match.NONE, {}

    async def handle(self, scope, receive, send):
        await self.app(scope, receive, send)


def create_application():
    """Compose a fresh top-level application (also useful for isolation tests)."""
    mcp = create_hosted_mcp()

    @asynccontextmanager
    async def lifespan(_application):
        async with mcp.session_manager.run():
            yield

    application = Starlette(
        routes=[
            ExactMCPRoute(
                StaffTokenAuthenticationMiddleware(
                    mcp.streamable_http_app(),
                )
            ),
            Mount("/", app=django_application),
        ],
        lifespan=lifespan,
    )
    application.state.mcp = mcp
    return application


application = create_application()
