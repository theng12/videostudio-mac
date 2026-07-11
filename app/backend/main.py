"""
Video Studio (Mac) — backend.

Serves:
- `/`                                → single-page UI
- `/api/health`                      → liveness check
- `/api/catalog`                     → model catalog + families with live cache state
- `/api/cache/{repo}`                → cache state for one repo
- `/api/downloads`                   → list/start/cancel downloads
- `/api/downloads/stream`            → SSE stream of per-job progress
- `/api/imports/scan`                → list candidates from VIDEOSTUDIO_EXTRA_MODEL_DIRS
- `/api/imports`                     → symlink/move an existing folder into HF_HOME
- `/api/reveal`                      → open a path in the OS file manager (macOS Finder)
- `/api/generate/availability`       → is the torch/diffusers engine installed?
- `/api/generate/diagnostics`        → per-package + per-engine readiness checklist
- `/api/generate/txt2video`          → start a text-to-video generation
- `/api/generate/video2video`        → start an image-to-video or video-to-video generation
- `/api/generate/jobs`               → list generation jobs
- `/api/generate/jobs/{id}`          → poll one job
- `/api/generate/jobs/{id}/video`    → fetch the rendered mp4
- `/api/generate/jobs/{id}/cancel`   → cancel a running job
- `/api/generate/history/{id}`       → delete one finished clip + its file
- `/api/generate/stream`             → SSE stream of generation jobs
- `/api/output/stats`                → outputs-folder size + clip count
- `/api/output/prune`                → reclaim disk (keep newest N / delete old)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import cache, catalog, settings as app_settings
from .downloads import manager
from .video import manager as gen_manager, diagnostics as gen_diagnostics, pipeline_available
from .imports import import_path, scan_for_candidates


# ───────────── App release version ─────────────
# Read once at module load — `VERSION` lives at the project root (a sibling
# of `app/`). Surfaced via `/api/version` for the WebUI footer and the
# (future) update-available check. Independent of FastAPI's `app.version`,
# which is the internal API version.

def _read_app_version() -> str:
    try:
        version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
        return version_file.read_text().strip()
    except Exception:
        return "unknown"

APP_VERSION = _read_app_version()


# ───────────── FastAPI setup ─────────────

app = FastAPI(title="Video Studio KH", version="0.1.0")

# Permissive CORS so the main mac can call the mac mini over LAN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Force the Pinokio webview (and any browser) to always re-fetch the
    static frontend. Pinokio's embedded webview caches index.html / app.js /
    style.css very aggressively, so we explicitly disable caching for the
    frontend files and any /assets/* path."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/assets") or path.endswith(
            (".html", ".js", ".css")
        ):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheStaticMiddleware)


# ───────────── request models ─────────────

class StartDownloadBody(BaseModel):
    repo: str
    token: Optional[str] = None


class ImportBody(BaseModel):
    source_path: str
    repo: Optional[str] = None
    mode: str = "link"   # "link" | "move"


class RevealBody(BaseModel):
    path: str


class PruneBody(BaseModel):
    keep_last: int = 0            # keep the newest N clips, delete the rest
    older_than_days: float = 0.0  # or: delete clips older than this many days


class SettingsBody(BaseModel):
    hf_token: Optional[str] = None   # pass "" to clear; omit field to leave unchanged


class TokenTestBody(BaseModel):
    hf_token: Optional[str] = None   # if omitted, tests the currently-saved token


class Txt2VideoBody(BaseModel):
    repo: str = Field(max_length=500)
    prompt: str = Field(max_length=20000)
    negative_prompt: str = Field("", max_length=20000)
    width: Optional[int] = Field(None, ge=256, le=2048)
    height: Optional[int] = Field(None, ge=256, le=2048)
    frames: Optional[int] = Field(None, ge=1, le=513)
    fps: Optional[int] = Field(None, ge=1, le=60)
    steps: Optional[int] = Field(None, ge=1, le=200)
    guidance: Optional[float] = Field(None, ge=0.0, le=30.0)
    seed: Optional[int] = Field(None, ge=-1, le=4294967295)


# ───────────── API: meta ─────────────

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "version": app.version,
        "app_version": APP_VERSION,
        "hf_home": str(cache.hf_home()),
        "hub_dir": str(cache.hub_dir()),
    }


# ── Update / generation health (auto-check surfaced by the web-UI banner) ──
# Detect-in-app, apply-via-sidebar: the frontend banner reads this and points
# the user at the single "Update" (or "Install Generation") button in the
# Pinokio sidebar. We never git-pull from here — a sandboxed web page can't
# reliably drive Pinokio's script runner, and the backend restarting itself
# mid-request is fragile.
import importlib.util as _ilu
import threading as _threading
import time as _time
import urllib.request as _urlreq

_UPDATE_REPO = "theng12/videostudio-mac"
_GEN_MODULE = "diffusers"
_update_state = {"checked_at": 0.0, "latest": None}


def _parse_ver(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0,)


def _refresh_latest_version():
    try:
        url = f"https://raw.githubusercontent.com/{_UPDATE_REPO}/main/VERSION"
        with _urlreq.urlopen(url, timeout=5) as r:
            _update_state["latest"] = r.read().decode("utf-8").strip()
    except Exception:
        pass
    finally:
        _update_state["checked_at"] = _time.time()


@app.get("/api/update-status")
def update_status() -> dict:
    """What the web-UI banner needs: are we behind the published version, and is
    the generation stack actually installed? The remote version is fetched from
    the repo's raw VERSION file at most every ~6h, in a background thread, so a
    slow or unreachable GitHub never blocks the request."""
    if _time.time() - _update_state["checked_at"] > 6 * 3600:
        _threading.Thread(target=_refresh_latest_version, daemon=True).start()
    latest = _update_state["latest"]
    behind = bool(latest and _parse_ver(latest) > _parse_ver(APP_VERSION))
    gen_required = _GEN_MODULE is not None
    gen_ok = (_ilu.find_spec(_GEN_MODULE) is not None) if gen_required else None
    return {
        "app_version": APP_VERSION,
        "latest_version": latest,
        "update_available": behind,
        "generation_required": gen_required,
        "generation_ok": gen_ok,
    }


@app.get("/api/version")
def app_release_version() -> dict:
    """Application release version + title. Read from the VERSION file at the
    project root. Frontend renders this in the footer and (eventually) compares
    against a remote `latest.json` for update-available signaling."""
    return {
        "app_version": APP_VERSION,
        "title": app.title,
    }


@app.get("/api/system")
def system_hardware() -> dict:
    """Apple Silicon chip + unified memory snapshot of the host. Frontend uses
    this for the Models tab per-model fit chip and the 'Your Mac' settings
    panel. Mac-only — the underlying sysctl probes return None elsewhere."""
    from . import system_info
    return system_info.system_info()


# ───────────── API: catalog ─────────────

@app.get("/api/catalog")
def get_catalog() -> dict:
    families = {fid: catalog.serialize_family(f) for fid, f in catalog.FAMILIES.items()}
    models = []
    for m in catalog.CATALOG:
        d = catalog.serialize_model(m)
        d["cache"] = cache.status_snapshot(m.repo)
        active = manager.active_for_repo(m.repo)
        d["active_download"] = active.serialize() if active else None
        models.append(d)
    return {"families": families, "models": models}


@app.get("/api/cache/{repo:path}")
def get_cache(repo: str) -> dict:
    return cache.status_snapshot(repo)


# ───────────── API: downloads ─────────────

@app.get("/api/downloads")
def list_downloads() -> dict:
    return {"jobs": [j.serialize() for j in manager.list_jobs()]}


@app.delete("/api/downloads")
def clear_downloads() -> dict:
    """Remove all terminal-state download jobs from memory."""
    return {"cleared": manager.clear_finished()}


@app.post("/api/downloads")
def start_download(body: StartDownloadBody) -> dict:
    if not body.repo or "/" not in body.repo:
        raise HTTPException(status_code=400, detail="repo must be 'owner/name'")
    job = manager.start(body.repo, token=body.token)
    return {"job": job.serialize()}


@app.delete("/api/downloads/{job_id}")
def cancel_download(job_id: str) -> dict:
    ok = manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found or already finished")
    job = manager.get(job_id)
    return {"job": job.serialize() if job else None}


@app.get("/api/downloads/stream")
async def stream_downloads():
    """
    Server-Sent Events stream.

    The browser keeps this connection open; we push the full job table every
    second (small JSON). Polling-style — the disk is the source of truth for
    progress, and downloads.py doesn't have native callbacks.
    """
    async def gen():
        try:
            while True:
                payload = {"jobs": [j.serialize() for j in manager.list_jobs()]}
                yield {"event": "snapshot", "data": json.dumps(payload)}
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
    return EventSourceResponse(gen())


# ───────────── API: imports ─────────────

@app.get("/api/imports/scan")
def imports_scan() -> dict:
    return {"candidates": [c.serialize() for c in scan_for_candidates()]}


@app.post("/api/imports")
def imports_link(body: ImportBody) -> dict:
    result = import_path(body.source_path, repo=body.repo, mode=body.mode)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "import failed"))
    return result


# ───────────── API: connectivity (where can this server be reached?) ─────────────

def _classify_ip(ip: str) -> str:
    """Heuristic label so the UI can recommend the right address for the use case."""
    if ip.startswith("127."):
        return "loopback"
    # Tailscale assigns CGNAT range 100.64.0.0 – 100.127.255.255
    try:
        octets = [int(x) for x in ip.split(".")]
        if len(octets) == 4 and octets[0] == 100 and 64 <= octets[1] <= 127:
            return "tailscale"
    except (ValueError, IndexError):
        pass
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
        return "lan"
    return "other"


def _list_local_ips() -> list[dict]:
    """All IPv4 addresses for this machine, classified by likely role."""
    ips: set[str] = set()
    # Hostname resolution — fast and works for most machines.
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except (socket.error, OSError):
        pass
    # Primary outbound IP via the connect-to-public-IP trick. No packets actually
    # leave the machine — UDP connect() just picks the route.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    out: list[dict] = []
    for ip in ips:
        if ":" in ip:           # skip ipv6 fragments
            continue
        out.append({"ip": ip, "kind": _classify_ip(ip)})
    # Sort: tailscale → lan → other → loopback, then by IP.
    rank = {"tailscale": 0, "lan": 1, "other": 2, "loopback": 3}
    out.sort(key=lambda d: (rank.get(d["kind"], 9), d["ip"]))
    return out


def _detect_bind_port(default: int = 47872) -> int:
    """
    Find the port uvicorn was actually started with by scanning sys.argv for
    `--port N`. This is the source of truth — request.url.port can lie when
    we're being proxied (e.g. through Pinokio's UI at 42000), but our own
    command-line args don't change.
    """
    args = sys.argv
    try:
        i = args.index("--port")
        return int(args[i + 1])
    except (ValueError, IndexError):
        pass
    env_port = os.environ.get("UVICORN_PORT", "").strip()
    if env_port.isdigit():
        return int(env_port)
    return default


def _detect_bind_host(default: str = "127.0.0.1") -> str:
    args = sys.argv
    try:
        i = args.index("--host")
        return args[i + 1]
    except (ValueError, IndexError):
        pass
    return default


_BIND_PORT = _detect_bind_port()
_BIND_HOST = _detect_bind_host()


@app.get("/api/connectivity")
def connectivity(request: Request) -> dict:
    """
    Return the addresses this server can be reached at, plus Pinokio share-proxy
    config from env. The UI uses this to render a "where can I reach the API?"
    panel in Settings so users don't have to dig through terminal output.

    `bind_port` is what uvicorn was actually launched with (the source of truth).
    `request_port` is whatever URL the *browser* used to reach us — they can
    differ if the request came through a proxy like Pinokio's UI on port 42000.
    The frontend uses `bind_port` when constructing remote-access URLs so it
    doesn't confuse users with the wrong number.
    """
    request_port = request.url.port
    if request_port is None:
        request_port = 443 if request.url.scheme == "https" else 80
    return {
        # Backwards-compat alias — older UI rendered `listen_port`. Now equals
        # the true bind port too.
        "listen_port": _BIND_PORT,
        "bind_port": _BIND_PORT,
        "bind_host": _BIND_HOST,
        "request_port": request_port,
        "scheme": request.url.scheme,
        "client_url": str(request.base_url).rstrip("/"),
        "addresses": _list_local_ips(),
        "share_local_enabled": (os.environ.get("PINOKIO_SHARE_LOCAL", "").strip().lower() == "true"),
        "share_local_port_fixed": os.environ.get("PINOKIO_SHARE_LOCAL_PORT", "").strip() or None,
        "share_passcode_set": bool(os.environ.get("PINOKIO_SHARE_PASSCODE", "").strip()),
        "pinokio_ui_port": 42000,   # Pinokio's own UI default; not detected, just informational
    }


# ───────────── API: settings ─────────────

@app.get("/api/settings")
def get_settings_endpoint() -> dict:
    """Returns a caller-safe (masked) view of the current settings."""
    return app_settings.serialize_public()


@app.post("/api/settings")
def update_settings_endpoint(body: SettingsBody) -> dict:
    """Update settings. Passing hf_token="" clears it."""
    if body.hf_token is not None:
        app_settings.set_hf_token(body.hf_token)
    return app_settings.serialize_public()


@app.post("/api/settings/test-hf-token")
def test_hf_token_endpoint(body: TokenTestBody) -> dict:
    """
    Validate a Hugging Face token by calling whoami(). If body.hf_token is
    omitted/empty, tests the saved token instead. Returns the user's display
    info on success, or 400 with the upstream error message on failure.
    """
    token = (body.hf_token or "").strip() or app_settings.get_hf_token()
    if not token:
        raise HTTPException(status_code=400, detail="No token provided and none saved in settings.")
    try:
        from huggingface_hub import HfApi  # imported here so it's lazy
        info = HfApi().whoami(token=token)
        return {
            "ok": True,
            "name": info.get("name") or info.get("fullname") or info.get("email"),
            "type": info.get("type"),
            "orgs": [o.get("name") for o in (info.get("orgs") or []) if o.get("name")],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token validation failed: {e}")


# ───────────── API: reveal in OS file manager ─────────────

_APP_ROOT = Path(__file__).resolve().parent.parent      # .../app
_LAUNCHER_ROOT = _APP_ROOT.parent                       # .../videostudio-mac

# Whitelist of root paths we'll open. Anything under these is fair game;
# anything outside is rejected so this endpoint can't be turned into an
# arbitrary-path opener from a malicious page on the LAN.
def _reveal_allowed_roots() -> list[Path]:
    return [
        cache.hf_home().resolve(),
        (_APP_ROOT / "output").resolve(),
        (_APP_ROOT / "uploads").resolve(),
        _LAUNCHER_ROOT.resolve(),
    ]


def _is_path_allowed(target: Path) -> bool:
    target = target.resolve()
    for root in _reveal_allowed_roots():
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


@app.post("/api/reveal")
def reveal_path(body: RevealBody) -> dict:
    """
    Open the given path in the OS file manager. macOS only for now
    (Finder via `open -R` for files, `open` for directories).
    """
    if sys.platform != "darwin":
        raise HTTPException(status_code=501, detail="Reveal is only implemented on macOS.")
    if not body.path:
        raise HTTPException(status_code=400, detail="path is required")
    target = Path(body.path).expanduser()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"path does not exist: {target}")
    if not _is_path_allowed(target):
        raise HTTPException(
            status_code=403,
            detail="path is outside the allowed roots (HF cache, app/output, app/uploads, launcher folder)"
        )
    # -R reveals (selects) the item in Finder; if `target` is a directory it
    # opens that directory directly.
    args = ["open", "-R", str(target.resolve())] if target.is_file() else ["open", str(target.resolve())]
    try:
        subprocess.Popen(args)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="`open` command not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reveal failed: {e}")
    return {"ok": True, "opened": str(target.resolve())}


# ───────────── API: generation ─────────────

@app.get("/api/generate/availability")
def generation_availability() -> dict:
    return gen_manager.availability()


@app.get("/api/generate/diagnostics")
def generation_diagnostics() -> dict:
    """Per-package + per-engine readiness checklist, surfaced in the Generate
    tab so users see what's installed and which engines are ready. Includes
    `app_version` so the frontend doesn't need an extra round-trip."""
    data = gen_diagnostics()
    data["app_version"] = APP_VERSION
    return data


def _require_engine_and_cache(repo: str) -> catalog.ModelEntry:
    """Shared guard for every generation route: the torch/diffusers engine must
    be installed and the model fully downloaded."""
    if not gen_manager.is_available():
        raise HTTPException(
            status_code=503,
            detail="Video engine not installed. Run the 'Install Generation' menu item.",
        )
    model = catalog.get_model(repo)
    if model is None:
        raise HTTPException(status_code=400, detail=f"Unknown repo: {repo}")
    if cache.cache_state(repo) != "cached":
        raise HTTPException(
            status_code=409,
            detail=f"Model {repo} is not fully cached. Download it from the Models tab first.",
        )
    return model


@app.post("/api/generate/txt2video")
def start_txt2video(body: Txt2VideoBody) -> dict:
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    model = _require_engine_and_cache(body.repo)
    if "txt2video" not in model.capabilities:
        raise HTTPException(
            status_code=400,
            detail=f"{model.label} does not support text-to-video. Pick a t2v-capable model.",
        )
    if not pipeline_available(model.family, "txt2video"):
        raise HTTPException(status_code=409, detail="This video pipeline is missing. Run Update or reinstall Generation.")
    job = gen_manager.start_txt2video(body.model_dump())
    return {"job": job.serialize()}


@app.post("/api/generate/video2video")
async def start_video2video(
    file: UploadFile = File(...),
    repo: str = Form(...),
    mode: str = Form("video2video"),          # "img2video" | "video2video"
    prompt: str = Form(""),
    negative_prompt: str = Form(""),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    frames: Optional[int] = Form(None),
    fps: Optional[int] = Form(None),
    steps: Optional[int] = Form(None),
    guidance: Optional[float] = Form(None),
    seed: Optional[int] = Form(None),
    strength: Optional[float] = Form(None),   # video2video only — distance from the input
) -> dict:
    """
    Image-to-video or video-to-video. multipart/form-data: a `file` (a still
    image for img2video, or a clip for video2video) plus the same numeric knobs
    as txt2video. `mode` selects which pipeline runs.
    """
    if mode not in ("img2video", "video2video"):
        raise HTTPException(status_code=400, detail="mode must be 'img2video' or 'video2video'")
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="an input file is required")
    model = _require_engine_and_cache(repo)
    if mode not in model.capabilities:
        raise HTTPException(
            status_code=400,
            detail=f"{model.label} does not support {mode}. Supported: {', '.join(model.capabilities)}.",
        )
    if not pipeline_available(model.family, mode):
        raise HTTPException(status_code=409, detail="This video pipeline is missing. Run Update or reinstall Generation.")
    if len(repo) > 500 or len(prompt) > 20000 or len(negative_prompt) > 20000:
        raise HTTPException(status_code=422, detail="repo and prompts exceed the supported length")

    numeric_limits = {
        "width": (width, 256, 2048), "height": (height, 256, 2048),
        "frames": (frames, 1, 513), "fps": (fps, 1, 60),
        "steps": (steps, 1, 200), "guidance": (guidance, 0.0, 30.0),
        "seed": (seed, -1, 4294967295), "strength": (strength, 0.0, 1.0),
    }
    for name, (value, low, high) in numeric_limits.items():
        if value is not None and not low <= value <= high:
            raise HTTPException(status_code=422, detail=f"{name} must be between {low} and {high}")

    # Persist the upload so the worker can load it by path.
    uploads_dir = Path(__file__).resolve().parent.parent / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    import uuid
    if mode == "img2video":
        allowed, default_suffix = (".png", ".jpg", ".jpeg", ".webp"), ".png"
    else:
        allowed, default_suffix = (".mp4", ".mov", ".webm", ".gif", ".mkv"), ".mp4"
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported input type. Allowed: {', '.join(allowed)}")
    saved = uploads_dir / (uuid.uuid4().hex[:12] + suffix)
    max_bytes = 20 * 1024 * 1024 if mode == "img2video" else 500 * 1024 * 1024
    written = 0
    try:
        with saved.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="Input image must be 20 MB or smaller." if mode == "img2video" else "Input video must be 500 MB or smaller.",
                    )
                output.write(chunk)
    except Exception:
        saved.unlink(missing_ok=True)
        raise
    if written == 0:
        saved.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="input file is empty")

    params = {
        "repo": repo,
        "mode": mode,
        "prompt": prompt.strip(),
        "negative_prompt": negative_prompt.strip(),
        "width": width,
        "height": height,
        "frames": frames,
        "fps": fps,
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        "strength": strength,
    }
    if mode == "img2video":
        params["image_path"] = str(saved.resolve())
    else:
        params["video_path"] = str(saved.resolve())
    job = gen_manager.start_video2video(params)
    return {"job": job.serialize()}


@app.get("/api/generate/jobs")
def list_generation_jobs() -> dict:
    return {"jobs": [j.serialize() for j in gen_manager.list_jobs()]}


@app.delete("/api/generate/jobs")
def clear_generation_history() -> dict:
    return {"cleared": gen_manager.clear_history()}


@app.get("/api/generate/jobs/{job_id}")
def get_generation_job(job_id: str) -> dict:
    job = gen_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job.serialize()}


@app.get("/api/generate/jobs/{job_id}/video")
def get_generation_video(job_id: str) -> FileResponse:
    job = gen_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.output_path:
        raise HTTPException(status_code=425, detail="video not ready yet")
    return FileResponse(job.output_path, media_type="video/mp4")


@app.delete("/api/generate/jobs/{job_id}")
def cancel_generation_job(job_id: str) -> dict:
    ok = gen_manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found or already finished")
    job = gen_manager.get(job_id)
    return {"job": job.serialize() if job else None}


@app.delete("/api/generate/history/{job_id}")
def delete_one_generation(job_id: str) -> dict:
    """Delete a single FINISHED clip: remove it from history and delete its .mp4
    from disk. (DELETE .../jobs/{id} only cancels active jobs.)"""
    if not gen_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


@app.get("/api/output/stats")
def output_stats() -> dict:
    """Size + count of generated clips on disk, for the disk-usage display."""
    return gen_manager.output_stats()


@app.post("/api/output/prune")
def prune_outputs(body: PruneBody) -> dict:
    """Reclaim disk: keep the newest N (keep_last) OR delete clips older than
    older_than_days. History entries for deleted files are trimmed too."""
    return gen_manager.prune_outputs(keep_last=body.keep_last, older_than_days=body.older_than_days)


@app.get("/api/generate/stream")
async def stream_generation():
    async def gen():
        try:
            while True:
                payload = {"jobs": [j.serialize() for j in gen_manager.list_jobs()]}
                yield {"event": "snapshot", "data": json.dumps(payload)}
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
    return EventSourceResponse(gen())


# ───────────── static frontend ─────────────

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

if FRONTEND_DIR.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIR), html=False),
        name="assets",
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))
