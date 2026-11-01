# ABOUTME: Serves the built React staff app: frontend/dist/index.html for
# ABOUTME: every non-API/admin/static route, with a fallback shell pre-build.
from html import escape
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_safe

from apps.configuration import runtime
from apps.records.public_tokens import set_public_form_cookie

FALLBACK = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PRR Staff</title></head>
<body><div id="root"></div>
<p>Frontend build not found. Run: cd frontend &amp;&amp; bun run build</p>
</body></html>"""


@require_safe
@ensure_csrf_cookie
def index(request, path=""):
    built = Path(settings.BASE_DIR) / "frontend" / "dist" / "index.html"
    shell = built.read_text() if built.exists() else FALLBACK
    auth_attributes = (
        f'data-auth-mode="{escape(runtime.get("PRR_AUTH_MODE"))}" '
        'data-google-login-url="/api/staff/auth/oidc/authenticate/"'
    )
    shell = shell.replace('<div id="root"', f'<div id="root" {auth_attributes}', 1)
    response = HttpResponse(shell, content_type="text/html")
    if path.rstrip("/") == "request":
        set_public_form_cookie(response)
        response.headers["Cache-Control"] = "no-store"
    return response
