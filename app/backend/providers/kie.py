"""Kie.ai Market adapter using createTask + unified recordInfo polling."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .. import settings as app_settings
from .base import CloudVideoModel, JobStatus, SubmitResult, VideoProvider

_API = "https://api.kie.ai/api/v1"
_MODELS_FILE = Path(__file__).resolve().parent / "kie_models.json"


def _key() -> Optional[str]:
    value = os.environ.get("VIDEOSTUDIO_KIE_KEY") or os.environ.get("KIE_API_KEY")
    if value and value.strip():
        return value.strip()
    cfg = ((app_settings.get("providers") or {}).get("kie") or {})
    value = cfg.get("key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _request(method: str, url: str, body: Optional[dict] = None) -> dict:
    token = _key()
    if not token:
        raise RuntimeError("Kie API key not set")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise RuntimeError(f"Kie HTTP {exc.code}: {detail or exc.reason}") from exc
    if data.get("code") not in (None, 200):
        raise RuntimeError(f"Kie API {data.get('code')}: {data.get('msg') or data}")
    return data


class KieProvider(VideoProvider):
    key = "kie"
    name = "Kie.ai"
    docs_url = "https://docs.kie.ai/market"

    def has_key(self) -> bool:
        return _key() is not None

    def list_models(self) -> list[CloudVideoModel]:
        rows = json.loads(_MODELS_FILE.read_text()).get("models", [])
        return [CloudVideoModel(
            id=f"kie:{row['provider_model']}", provider="kie",
            provider_model=row["provider_model"], label=row.get("label", row["provider_model"]),
            capabilities=tuple(row.get("capabilities") or ("txt2video",)),
            summary=row.get("summary", ""), max_duration_s=row.get("max_duration_s"),
            resolutions=tuple(row.get("resolutions") or ()),
            aspect_ratios=tuple(row.get("aspect_ratios") or ()),
            price_unit=row.get("price_unit"), price_usd=row.get("price_usd"),
        ) for row in rows]

    def submit(self, model: CloudVideoModel, mode: str, params: dict) -> SubmitResult:
        inp = {"prompt": params.get("prompt", "")}
        for key in ("duration", "aspect_ratio", "seed"):
            if params.get(key) not in (None, ""):
                inp[key] = str(params[key]) if key == "duration" else params[key]
        if model.provider_model == "kling-3.0/video":
            inp["mode"] = {
                "720p": "std", "1080p": "pro", "4k": "4K",
            }.get(str(params.get("resolution") or "720p"), "std")
        if mode == "img2video":
            image = params.get("image_url") or params.get("image_data_uri")
            if image:
                inp["image_urls"] = [image]
        extra = params.get("provider_params")
        if isinstance(extra, dict):
            reserved = {"prompt", "duration", "resolution", "aspect_ratio", "seed", "mode",
                        "image_urls", "estimate_usd"}
            inp.update({k: v for k, v in extra.items() if k not in reserved})
        response = _request("POST", f"{_API}/jobs/createTask",
                            {"model": model.provider_model, "input": inp})
        task_id = (response.get("data") or {}).get("taskId")
        if not task_id:
            raise RuntimeError(f"Kie returned no taskId: {str(response)[:300]}")
        return SubmitResult(provider_job_id=task_id, raw={"task_id": task_id})

    def poll(self, model: CloudVideoModel, submit_raw: dict) -> JobStatus:
        task_id = submit_raw.get("task_id")
        url = f"{_API}/jobs/recordInfo?{urllib.parse.urlencode({'taskId': task_id})}"
        result = _request("GET", url)
        data = result.get("data") or {}
        state = str(data.get("state") or "").lower()
        if state in ("waiting", "queuing", "generating", ""):
            return JobStatus(state="running", progress=_progress(data.get("progress")), raw=result)
        if state == "fail":
            return JobStatus(state="error", error=data.get("failMsg") or data.get("failCode") or "Kie task failed", raw=result)
        if state != "success":
            return JobStatus(state="running", raw=result)
        payload = data.get("resultJson") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        urls = payload.get("resultUrls") or []
        return JobStatus(state="done", result_url=urls[0] if urls else None, raw=result)


def _progress(value) -> Optional[float]:
    try:
        return min(1.0, max(0.0, float(value) / 100.0))
    except (TypeError, ValueError):
        return None
