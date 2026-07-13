"""fal.ai adapter.

Queue API (verified against fal's docs, 2026-07):
  submit : POST https://queue.fal.run/{provider_model}          Authorization: Key <FAL_KEY>
           → { request_id, status_url, response_url, cancel_url, ... }
  status : GET  {status_url}                                    → { status: IN_QUEUE|IN_PROGRESS|COMPLETED }
  result : GET  {response_url}                                  → model-specific; video at result["video"]["url"]

We reuse the URLs fal returns from submit (its status URL uses the base app id,
not the full sub-path, so reconstructing them by hand is unreliable).

HTTP uses stdlib urllib so the cloud gateway works after a plain git pull +
restart, with no dependency reinstall. Model list is curated in
fal_models.json (fal has no clean public model-list API — see SPEC §6/§13).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import settings as app_settings
from .base import CloudVideoModel, JobStatus, SubmitResult, VideoProvider

_QUEUE_BASE = "https://queue.fal.run"
_MODELS_FILE = Path(__file__).resolve().parent / "fal_models.json"
_HTTP_TIMEOUT = 30


def _fal_key() -> Optional[str]:
    """FAL key from env override or saved provider settings."""
    import os
    env = os.environ.get("VIDEOSTUDIO_FAL_KEY") or os.environ.get("FAL_KEY")
    if env and env.strip():
        return env.strip()
    providers = app_settings.get("providers") or {}
    cfg = providers.get("fal") or {}
    key = cfg.get("key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _paid_on() -> bool:
    providers = app_settings.get("providers") or {}
    return bool((providers.get("fal") or {}).get("paid", False))


def _request(method: str, url: str, *, body: Optional[dict] = None) -> dict:
    key = _fal_key()
    if not key:
        raise RuntimeError("fal API key not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Key {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:500]
        except Exception:
            pass
        raise RuntimeError(f"fal HTTP {e.code}: {detail or e.reason}") from e


class FalProvider(VideoProvider):
    key = "fal"
    name = "fal.ai"
    docs_url = "https://fal.ai/models"

    def __init__(self) -> None:
        self._cache: Optional[list[CloudVideoModel]] = None

    def has_key(self) -> bool:
        return _fal_key() is not None

    def paid_on(self) -> bool:
        return _paid_on()

    def list_models(self) -> list[CloudVideoModel]:
        if self._cache is not None:
            return self._cache
        out: list[CloudVideoModel] = []
        try:
            data = json.loads(_MODELS_FILE.read_text())
            for m in data.get("models", []):
                pm = m["provider_model"]
                out.append(CloudVideoModel(
                    id=f"fal:{pm}",
                    provider="fal",
                    provider_model=pm,
                    label=m.get("label", pm),
                    capabilities=tuple(m.get("capabilities", ["txt2video"])),
                    summary=m.get("summary", ""),
                    max_duration_s=m.get("max_duration_s"),
                    resolutions=tuple(m.get("resolutions", [])),
                    aspect_ratios=tuple(m.get("aspect_ratios", [])),
                    price_unit=m.get("price_unit"),
                    price_usd=m.get("price_usd"),
                    status=m.get("status", "available"),
                ))
        except Exception as e:  # a broken file must not take the whole app down
            print(f"[fal] failed to load model list: {e}", flush=True)
        self._cache = out
        return out

    # ── generation ──
    def _build_input(self, model: CloudVideoModel, mode: str, params: dict) -> dict:
        """Map Video Studio's generic params to a fal input body. Unknown extra
        keys are passed through (opaque pass-through per SPEC §9)."""
        body: dict = {}
        if params.get("prompt"):
            body["prompt"] = params["prompt"]
        if params.get("negative_prompt"):
            body["negative_prompt"] = params["negative_prompt"]
        for k in ("duration", "aspect_ratio", "resolution", "seed", "cfg_scale"):
            if params.get(k) not in (None, ""):
                body[k] = params[k]
        # image-to-video: fal wants an image URL; a data: URI works without hosting.
        if mode == "img2video":
            img = params.get("image_url") or params.get("image_data_uri")
            if img:
                body["image_url"] = img
        # Pass through any explicitly-provided provider params bag.
        extra = params.get("provider_params")
        if isinstance(extra, dict):
            body.update(extra)
        return body

    def submit(self, model: CloudVideoModel, mode: str, params: dict) -> SubmitResult:
        if not self.paid_on():
            raise RuntimeError("Paid cloud generation is off for fal. Enable it in Settings first.")
        url = f"{_QUEUE_BASE}/{model.provider_model}"
        resp = _request("POST", url, body=self._build_input(model, mode, params))
        req_id = resp.get("request_id") or resp.get("requestId") or ""
        if not req_id:
            raise RuntimeError(f"fal submit returned no request_id: {str(resp)[:300]}")
        return SubmitResult(provider_job_id=req_id, raw=resp)

    def poll(self, model: CloudVideoModel, submit_raw: dict) -> JobStatus:
        status_url = submit_raw.get("status_url")
        response_url = submit_raw.get("response_url")
        if not status_url:
            return JobStatus(state="error", error="missing status_url from fal submit")
        s = _request("GET", status_url)
        state = (s.get("status") or "").upper()
        if state in ("IN_QUEUE", "IN_PROGRESS"):
            return JobStatus(state="running", raw=s)
        if state != "COMPLETED":
            return JobStatus(state="running", raw=s)  # unknown → keep polling
        # completed → fetch result
        result = _request("GET", response_url) if response_url else s
        video_url = _extract_video_url(result)
        if not video_url:
            return JobStatus(state="error", error=f"no video in fal result: {str(result)[:300]}", raw=result)
        return JobStatus(state="done", result_url=video_url, raw=result)

    def cancel(self, model: CloudVideoModel, submit_raw: dict) -> bool:
        cancel_url = submit_raw.get("cancel_url")
        if not cancel_url:
            return False
        try:
            _request("PUT", cancel_url)
            return True
        except Exception:
            return False


def _extract_video_url(result: dict) -> Optional[str]:
    """Pull the output video URL out of a fal result. Video models return a
    `video` object ({url:...}); some return `videos`/`output` variants."""
    if not isinstance(result, dict):
        return None
    v = result.get("video")
    if isinstance(v, dict) and v.get("url"):
        return v["url"]
    if isinstance(v, str) and v.startswith("http"):
        return v
    for k in ("videos", "output", "outputs"):
        val = result.get(k)
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
            if isinstance(first, str) and first.startswith("http"):
                return first
        if isinstance(val, dict) and val.get("url"):
            return val["url"]
    return None
