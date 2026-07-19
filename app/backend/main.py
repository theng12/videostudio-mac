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
- `/api/generate/availability`       → is a local MLX/Diffusers engine installed?
- `/api/generate/diagnostics`        → per-package + per-engine readiness checklist
- `/api/generate/txt2video`          → start a text-to-video generation
- `/api/generate/video2video`        → start an image-to-video or video-to-video generation
- `/api/generate/jobs`               → list generation jobs
- `/api/generate/jobs/{id}`          → poll one job
- `/api/generate/jobs/{id}/repair`   → re-attach a saved cloud provider task
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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import cache, catalog, memory_policy, settings as app_settings, storage_policy
from . import spend, cloud_jobs
from .providers import registry as providers_registry
from .downloads import manager
from .video import OUTPUT_DIR, manager as gen_manager, diagnostics as gen_diagnostics, model_pipeline_available
from .imports import import_path, scan_for_candidates
from .fleet_auth import load_token as load_fleet_token, make_middleware as fleet_middleware, manifest
from .auto_update import UpdateError
from .auto_update_config import create_updater
from .process_title import PROCESS_TITLE, apply_process_title


PROCESS_TITLE_APPLIED = apply_process_title()


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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    providers_registry.start_catalog_sync()
    resumed = cloud_jobs.resume_inflight()
    cloud_jobs.start_repair_watchdog()
    if resumed:
        print(f"[cloud] resumed {resumed} in-flight job(s)", flush=True)
    yield


app = FastAPI(title="Video Studio KH", version=APP_VERSION, lifespan=lifespan)

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
FLEET_TOKEN = load_fleet_token()
app.middleware("http")(fleet_middleware(FLEET_TOKEN))
storage_policy.start_background(gen_manager, OUTPUT_DIR)
memory_policy.start_background(gen_manager)


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


class AutoUpdateSettingsBody(BaseModel):
    mode: str
    frequency: str
    maintenance_hour: int
    idle_only: bool = True


class AutoUpdateRequestBody(BaseModel):
    after_current: bool = False


class MemoryPolicyBody(BaseModel):
    mode: str


class TokenTestBody(BaseModel):
    hf_token: Optional[str] = None   # if omitted, tests the currently-saved token


class ProviderKeyBody(BaseModel):
    key: Optional[str] = Field(None, max_length=500)   # "" / null clears


class ProviderPaidBody(BaseModel):
    paid: bool


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
    # Cloud-provider knobs (ignored by the local engine). Cloud models bill by
    # duration, so `duration` also drives the cost estimate/guardrail.
    duration: Optional[float] = Field(None, ge=0.1, le=60)
    aspect_ratio: Optional[str] = Field(None, max_length=16)
    resolution: Optional[str] = Field(None, max_length=16)
    provider_params: Optional[dict] = None


def _automatic_update_blockers() -> list[str]:
    reasons: list[str] = []
    generation_states = {str(job.state) for job in gen_manager.list_jobs()}
    if generation_states & {"queued", "running", "cancelling"}:
        reasons.append("a local or cloud video generation is queued or running")
    download_states = {str(job.state) for job in manager.list_jobs()}
    if download_states & {"queued", "running", "paused", "cancelling"}:
        reasons.append("a model download is active")
    return reasons


auto_updater = create_updater(readiness=_automatic_update_blockers)


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


@app.get("/api/capabilities")
def capabilities() -> dict:
    return manifest(modality="video", title=app.title, version=APP_VERSION,
                    operations=["txt2video", "img2video", "video2video"],
                    diagnostics="/api/generate/diagnostics")


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


@app.get("/api/auto-update/status")
def automatic_update_status() -> dict:
    return auto_updater.public_status()


@app.get("/api/auto-update/readiness")
def automatic_update_readiness() -> dict:
    return auto_updater.readiness_status()


@app.post("/api/auto-update/settings")
def automatic_update_settings(body: AutoUpdateSettingsBody) -> dict:
    try:
        return auto_updater.save_settings(body.model_dump())
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auto-update/check")
def automatic_update_check() -> dict:
    try:
        return auto_updater.trigger_check()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto-update/update")
def automatic_update_run(body: AutoUpdateRequestBody) -> dict:
    try:
        return auto_updater.trigger_update(after_current=body.after_current)
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto-update/retry")
def automatic_update_retry() -> dict:
    try:
        return auto_updater.retry()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    families.update(providers_registry.cloud_families())
    models = []
    for m in catalog.CATALOG:
        d = catalog.serialize_model(m)
        d["cache"] = cache.status_snapshot(m.repo, m.aliases)
        active = manager.active_for_repo(m.repo)
        d["active_download"] = active.serialize() if active else None
        d["is_cloud"] = False
        models.append(d)
    # Cloud models (fal, …) appear in the same unified catalog. They carry
    # is_cloud=true + hub_modality=video so the Hub slots them into its cloud lane.
    models.extend(providers_registry.cloud_models_serialized())
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
    model = catalog.get_model(body.repo)
    if model is None:
        raise HTTPException(status_code=400, detail="Only models in the audited local catalog can be downloaded.")
    job = manager.start(model.repo, token=body.token)
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


# ───────────── API: cloud providers + spend ─────────────

@app.get("/api/providers")
def list_providers() -> dict:
    """Linked cloud video providers: key-set state, paid toggle, model count,
    and per-provider spend vs caps. Never returns raw API keys."""
    return {"providers": providers_registry.providers_status()}


@app.post("/api/providers/{key}/key")
def set_provider_key(key: str, body: ProviderKeyBody) -> dict:
    """Set (or clear, with "") a provider's API key. Owner-only; stored in the
    chmod-0600 settings file and never returned."""
    try:
        providers_registry.set_key(key, body.key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown provider: {key}")
    return {"providers": providers_registry.providers_status()}


@app.post("/api/providers/{key}/paid")
def set_provider_paid(key: str, body: ProviderPaidBody) -> dict:
    """Enable/disable paid (real-money) generation for a provider. Off by
    default — nothing bills until this is on AND a key is set."""
    try:
        providers_registry.set_paid(key, body.paid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown provider: {key}")
    return {"providers": providers_registry.providers_status()}


@app.post("/api/providers/{key}/refresh")
def refresh_provider(key: str) -> dict:
    """Refresh a provider's live/curated model catalog and persistent cache."""
    try:
        n = providers_registry.refresh(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown provider: {key}")
    return {"provider": key, "model_count": n}


@app.get("/api/spend")
def get_spend() -> dict:
    """Cloud spend today/this-month vs caps (global + per provider), plus recent records."""
    return spend.summary()


@app.post("/api/spend/caps")
def set_spend_caps(body: dict) -> dict:
    """Set spend caps. Body: {"global":{"daily","monthly"},"per_provider":{prov:{...}}}.
    0 = no cap. Enforced together with the tighter cap winning."""
    spend.set_caps(body)
    return spend.summary()


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
    """Shared guard for every generation route: the selected local engine must
    be installed and the model fully downloaded."""
    if not gen_manager.is_available():
        raise HTTPException(
            status_code=503,
            detail="Video engine not installed. Run the 'Install Generation' menu item.",
        )
    model = catalog.get_model(repo)
    if model is None:
        raise HTTPException(status_code=400, detail=f"Unknown repo: {repo}")
    if cache.status_snapshot(model.repo, model.aliases)["state"] != "cached":
        raise HTTPException(
            status_code=409,
            detail=f"Model {repo} is not fully cached. Download it from the Models tab first.",
        )
    return model


def _validate_uploaded_media(path: Path, mode: str) -> None:
    """Decode uploaded media and enforce bounded dimensions/duration.

    Extensions and browser MIME types are hints only. Pillow/ffprobe are the
    authority so malformed or renamed files never reach a model loader.
    """
    if mode == "img2video":
        from PIL import Image, UnidentifiedImageError
        Image.MAX_IMAGE_PIXELS = 40_000_000
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(status_code=400, detail=f"Input is not a safe decodable image: {exc}")
        if width < 64 or height < 64 or width > 8192 or height > 8192 or width * height > 40_000_000:
            raise HTTPException(status_code=422, detail="Input image must be 64–8192 px per side and at most 40 megapixels.")
        return

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate,nb_frames:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=True,
        )
        metadata = json.loads(result.stdout)
        stream = (metadata.get("streams") or [])[0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float((metadata.get("format") or {}).get("duration") or 0)
        rate = str(stream.get("avg_frame_rate") or "0/1").split("/", 1)
        fps = float(rate[0]) / max(1.0, float(rate[1]))
        raw_frames = stream.get("nb_frames")
        frames = int(raw_frames) if str(raw_frames).isdigit() else int(duration * fps + 0.5)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Input is not a safe decodable video: {exc}")
    if width < 64 or height < 64 or width > 4096 or height > 4096 or width * height > 16_777_216:
        raise HTTPException(status_code=422, detail="Input video dimensions exceed the 4096 px / 16.8 megapixel limit.")
    if not 0 < duration <= 60 or not 0 < fps <= 60 or not 0 < frames <= 3600:
        raise HTTPException(status_code=422, detail="Input video must be at most 60 seconds, 60 FPS, and 3600 frames.")


def _start_cloud(params: dict, mode: str) -> dict:
    """Shared cloud dispatch for both generate routes. Translates the gateway's
    exceptions into HTTP: no key → 400, cap exceeded → 402, unknown model → 400."""
    try:
        job, est = cloud_jobs.start_cloud_generation(mode, params)
    except cloud_jobs.NoProviderKey as e:
        raise HTTPException(status_code=400, detail=str(e))
    except cloud_jobs.PaidUseDisabled as e:
        raise HTTPException(status_code=403, detail=str(e))
    except spend.CapExceeded as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    out = job.serialize()
    out["estimate_usd"] = est
    return {"job": out}


@app.post("/api/generate/txt2video")
def start_txt2video(body: Txt2VideoBody) -> dict:
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if providers_registry.is_cloud_id(body.repo):
        return _start_cloud(body.model_dump(), "txt2video")
    model = _require_engine_and_cache(body.repo)
    if "txt2video" not in model.capabilities:
        raise HTTPException(
            status_code=400,
            detail=f"{model.label} does not support text-to-video. Pick a t2v-capable model.",
        )
    if not model_pipeline_available(model, "txt2video"):
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
    duration: Optional[float] = Form(None),
    resolution: Optional[str] = Form(None),
    aspect_ratio: Optional[str] = Form(None),
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
    is_cloud = providers_registry.is_cloud_id(repo)
    if not is_cloud:
        model = _require_engine_and_cache(repo)
        if mode not in model.capabilities:
            raise HTTPException(
                status_code=400,
                detail=f"{model.label} does not support {mode}. Supported: {', '.join(model.capabilities)}.",
            )
        if not model_pipeline_available(model, mode):
            raise HTTPException(status_code=409, detail="This video pipeline is missing. Run Update or reinstall Generation.")
    if len(repo) > 500 or len(prompt) > 20000 or len(negative_prompt) > 20000:
        raise HTTPException(status_code=422, detail="repo and prompts exceed the supported length")

    numeric_limits = {
        "width": (width, 256, 2048), "height": (height, 256, 2048),
        "frames": (frames, 1, 513), "fps": (fps, 1, 60),
        "steps": (steps, 1, 200), "guidance": (guidance, 0.0, 30.0),
        "seed": (seed, -1, 4294967295), "strength": (strength, 0.0, 1.0),
        "duration": (duration, 0.1, 60.0),
    }
    for name, (value, low, high) in numeric_limits.items():
        if value is not None and not low <= value <= high:
            raise HTTPException(status_code=422, detail=f"{name} must be between {low} and {high}")
    if resolution is not None and len(resolution) > 16:
        raise HTTPException(status_code=422, detail="resolution exceeds the supported length")
    if aspect_ratio is not None and len(aspect_ratio) > 16:
        raise HTTPException(status_code=422, detail="aspect_ratio exceeds the supported length")

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
    try:
        _validate_uploaded_media(saved, mode)
    except HTTPException:
        saved.unlink(missing_ok=True)
        raise

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
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
    }
    if mode == "img2video":
        params["image_path"] = str(saved.resolve())
    else:
        params["video_path"] = str(saved.resolve())

    if is_cloud:
        # Cloud providers take an image URL — pass the upload as a data URI so
        # we don't need to host it. (video2video via cloud isn't wired yet.)
        if mode == "img2video":
            import base64
            ext = suffix.lstrip(".") or "png"
            b = saved.read_bytes()
            params["image_data_uri"] = f"data:image/{ext};base64," + base64.b64encode(b).decode()
        return _start_cloud(params, mode)

    job = gen_manager.start_video2video(params)
    return {"job": job.serialize()}


@app.get("/api/generate/jobs")
def list_generation_jobs() -> dict:
    return {"jobs": gen_manager.serialized_jobs()}


@app.delete("/api/generate/jobs")
def clear_generation_history() -> dict:
    return {"cleared": gen_manager.clear_history()}


@app.get("/api/generate/jobs/{job_id}")
def get_generation_job(job_id: str) -> dict:
    job = gen_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job.serialize()}


@app.post("/api/generate/jobs/{job_id}/repair")
def repair_generation_job(job_id: str) -> dict:
    """Re-attach polling to a saved provider task. This never submits a new task."""
    try:
        job, attached = cloud_jobs.repair_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job": job.serialize(), "attached": attached,
            "message": "Repair attached to the saved provider task." if attached
                       else "The saved provider task is already being monitored."}


@app.get("/api/generate/jobs/{job_id}/video")
def get_generation_video(job_id: str) -> FileResponse:
    job = gen_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.output_path:
        raise HTTPException(status_code=425, detail="video not ready yet")
    output = Path(job.output_path).resolve()
    try:
        output.relative_to((Path(__file__).resolve().parent.parent / "output").resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="job output is outside the managed output folder")
    if not output.is_file():
        raise HTTPException(status_code=404, detail="video file is missing")
    return FileResponse(output, media_type="video/mp4")


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


@app.get("/api/storage-policy")
def get_storage_policy() -> dict:
    return storage_policy.status(gen_manager, OUTPUT_DIR)


@app.put("/api/storage-policy")
def put_storage_policy(body: dict) -> dict:
    storage_policy.save(body.get("enabled"), body.get("retention_days"), body.get("max_gb"))
    return storage_policy.status(gen_manager, OUTPUT_DIR)


@app.post("/api/storage-policy/cleanup")
def cleanup_storage_policy(body: dict | None = None) -> dict:
    body = body or {}; target = body.get("target_bytes")
    if target is not None and (not isinstance(target, int) or target < 0):
        raise HTTPException(400, "target_bytes must be a non-negative integer")
    return storage_policy.enforce(gen_manager, OUTPUT_DIR, target)


@app.get("/api/memory-policy")
def get_memory_policy() -> dict:
    return {
        **memory_policy.status(),
        "process_title": PROCESS_TITLE,
        "process_title_applied": PROCESS_TITLE_APPLIED,
    }


@app.put("/api/memory-policy")
def put_memory_policy(body: MemoryPolicyBody) -> dict:
    memory_policy.save(body.mode)
    return get_memory_policy()


@app.post("/api/memory/release")
def release_memory() -> dict:
    return {
        **memory_policy.release_now(),
        "process_title": PROCESS_TITLE,
        "process_title_applied": PROCESS_TITLE_APPLIED,
    }


@app.get("/api/generate/stream")
async def stream_generation():
    async def gen():
        try:
            while True:
                payload = {"jobs": gen_manager.serialized_jobs()}
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
