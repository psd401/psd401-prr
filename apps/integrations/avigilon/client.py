"""
Avigilon Alta (Ava Aware) adapter — footage preservation.

PRESERVATION MODEL: Alta's rolling retention (district: 10 days) deletes raw
footage, but SAVED CLIPS persist independently of retention. So a "hold" =
create a saved clip per camera covering the requested window. Releasing a
camera from the hold = delete its clip (only via the exclusion service,
which requires a human + reason).

⚠️ ENDPOINT VERIFICATION REQUIRED: Aware's Open API is documented behind the
org portal (docs.avigilon.com / support.avigilon.com, org-specific base URL
and bearer token from Aware admin settings). The paths below follow Aware's
public API conventions but MUST be verified against the district's tenant
before enabling AUTO_FOOTAGE_HOLD in production. Everything else in this
module (auth pattern, workflow, error contract) is stable. First task on a
laptop with the org token: `python manage.py verify_alta` (see checks()).

If clip creation is unavailable on the district's plan, the fallback is the
export/download mechanism (Aware supports direct download and backup to
S3/FTP) — swap create_clip_hold() internals, keep the same interface.
"""
from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import urlsplit

import requests

from apps.configuration import runtime

logger = logging.getLogger(__name__)


class AltaError(RuntimeError):
    pass


class AltaNotConfigured(AltaError):
    pass


def _validated_base_url() -> str:
    base_url = runtime.get("ALTA_API_BASE").strip()
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise AltaError("ALTA_API_BASE must be a valid HTTPS URL.") from exc
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
    normalized_host = parsed.hostname.lower()
    authority = (
        f"[{normalized_host}]"
        if ":" in normalized_host
        else normalized_host
    )
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}{parsed.path.rstrip('/')}"


def _session() -> requests.Session:
    if not runtime.get("ALTA_API_BASE") or not runtime.get_secret("ALTA_API_TOKEN"):
        raise AltaNotConfigured(
            "ALTA_API_BASE / ALTA_API_TOKEN not set — footage holds will be "
            "created as MANUAL tasks until the Aware API is configured."
        )
    _validated_base_url()
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {runtime.get_secret('ALTA_API_TOKEN')}",
        "Accept": "application/json",
    })
    return s


def _url(path: str) -> str:
    return _validated_base_url() + path


def _reject_redirect(response) -> None:
    if 300 <= response.status_code < 400:
        raise AltaError("Alta API redirects are not allowed.")


def list_cameras() -> list[dict]:
    """Return [{'id': ..., 'name': ..., 'site': ...}, ...] for all cameras."""
    s = _session()
    resp = s.get(
        _url("/api/v1/cameras"),
        timeout=30,
        allow_redirects=False,
    )   # VERIFY path on tenant
    _reject_redirect(resp)
    resp.raise_for_status()
    data = resp.json()
    cams = data.get("cameras", data if isinstance(data, list) else [])
    return [
        {"id": c.get("id"), "name": c.get("name", ""), "site": c.get("siteName", c.get("site", ""))}
        for c in cams
    ]


def create_clip_hold(camera_id: str, start: dt.datetime, end: dt.datetime, label: str) -> str:
    """Create a saved clip (persists beyond rolling retention). Returns clip id."""
    s = _session()
    resp = s.post(
        _url("/api/v1/clips"),
        json={                # VERIFY path
            "cameraId": camera_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "name": label,
            "description": (
                "Public Records Act preservation hold — do not delete. "
                "Managed by the PRR system."
            ),
        },
        timeout=60,
        allow_redirects=False,
    )
    _reject_redirect(resp)
    resp.raise_for_status()
    return str(resp.json().get("id", ""))


def delete_clip(clip_id: str) -> None:
    """Remove a saved clip. ONLY called by the exclusion service (human+reason)."""
    s = _session()
    resp = s.delete(
        _url(f"/api/v1/clips/{clip_id}"),
        timeout=30,
        allow_redirects=False,
    )   # VERIFY path
    _reject_redirect(resp)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


def checks() -> dict:
    """Connectivity/permission smoke test for `manage.py verify_alta`."""
    cams = list_cameras()
    return {"ok": True, "camera_count": len(cams), "sample": cams[:3]}
