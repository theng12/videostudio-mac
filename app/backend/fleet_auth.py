"""Shared fleet authentication for KH Studio APIs."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

LAUNCHER_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = LAUNCHER_ROOT.parent
HUB_TOKEN_FILE = API_ROOT / "studiohub-mac" / ".fleet_token"
SHARED_TOKEN_FILE = API_ROOT / ".kh_studio_token"
COOKIE_NAME = "kh_studio_token"
PUBLIC_PATHS = {"/", "/api/health", "/api/version", "/api/capabilities", "/api/update-status"}


def _read_private(path: Path) -> str | None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
            return path.read_text().strip() or None
    except OSError:
        return None
    return None


def load_token() -> str:
    env = os.environ.get("KH_STUDIO_TOKEN") or os.environ.get("STUDIOHUB_FLEET_TOKEN")
    if env and env.strip():
        return env.strip()
    for path in (HUB_TOKEN_FILE, SHARED_TOKEN_FILE):
        token = _read_private(path)
        if token:
            return token
    token = secrets.token_urlsafe(24)
    SHARED_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(SHARED_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
    except FileExistsError:
        token = _read_private(SHARED_TOKEN_FILE) or token
    os.chmod(SHARED_TOKEN_FILE, 0o600)
    return token


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _presented(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-studio-token") or
            request.headers.get("x-hub-token") or
            request.cookies.get(COOKIE_NAME) or
            request.query_params.get("token"))


def make_middleware(token: str):
    async def middleware(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                return JSONResponse({"detail": "Cross-origin browser writes are not allowed."}, 403)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/assets/") or request.method == "OPTIONS" or is_loopback(request):
            return await call_next(request)
        offered = _presented(request)
        current_token = load_token()
        if offered and secrets.compare_digest(offered, current_token):
            response = await call_next(request)
            if request.cookies.get(COOKIE_NAME) != current_token:
                response.set_cookie(COOKIE_NAME, current_token, httponly=True, samesite="strict")
            return response
        return JSONResponse({"detail": "Fleet token required for remote Studio access."}, 401)
    return middleware


def manifest(*, modality: str, title: str, version: str, operations: list[str], diagnostics: str) -> dict:
    return {
        "schema_version": 1,
        "studio": {"modality": modality, "title": title, "app_version": version},
        "auth": {"mode": "fleet_token", "header": "X-Studio-Token", "loopback_exempt": True},
        "operations": operations,
        "catalog_endpoint": "/api/catalog",
        "diagnostics_endpoint": diagnostics,
        "update": {"script": "update.js", "supports_drain": True},
    }
