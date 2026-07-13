"""Replicate video adapter using its durable Predictions HTTP API."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import settings as app_settings
from .base import CloudVideoModel, JobStatus, SubmitResult, VideoProvider

_API = "https://api.replicate.com/v1"
_MODELS_FILE = Path(__file__).resolve().parent / "replicate_models.json"


def _key() -> Optional[str]:
    value = os.environ.get("VIDEOSTUDIO_REPLICATE_KEY") or os.environ.get("REPLICATE_API_TOKEN")
    if value and value.strip():
        return value.strip()
    cfg = ((app_settings.get("providers") or {}).get("replicate") or {})
    value = cfg.get("key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _request(method: str, url: str, body: Optional[dict] = None) -> dict:
    token = _key()
    if not token:
        raise RuntimeError("Replicate API token not set")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise RuntimeError(f"Replicate HTTP {exc.code}: {detail or exc.reason}") from exc


def _model(raw: dict, curated: Optional[dict] = None) -> CloudVideoModel:
    curated = curated or {}
    path = curated.get("provider_model") or f"{raw.get('owner')}/{raw.get('name')}"
    return CloudVideoModel(
        id=f"replicate:{path}", provider="replicate", provider_model=path,
        label=curated.get("label") or path,
        capabilities=tuple(curated.get("capabilities") or ("txt2video",)),
        summary=curated.get("summary") or raw.get("description") or "Replicate video model.",
        max_duration_s=curated.get("max_duration_s"),
        resolutions=tuple(curated.get("resolutions") or ()),
        aspect_ratios=tuple(curated.get("aspect_ratios") or ()),
        price_unit=curated.get("price_unit"), price_usd=curated.get("price_usd"),
    )


class ReplicateProvider(VideoProvider):
    key = "replicate"
    name = "Replicate"
    docs_url = "https://replicate.com/collections/text-to-video"

    def has_key(self) -> bool:
        return _key() is not None

    def list_models(self) -> list[CloudVideoModel]:
        curated_rows = json.loads(_MODELS_FILE.read_text()).get("models", [])
        curated = {row["provider_model"]: row for row in curated_rows}
        models = {path: _model({}, row) for path, row in curated.items()}
        if not self.has_key():
            return list(models.values())
        # Replicate maintains this public collection as its live video list.
        data = _request("GET", f"{_API}/collections/text-to-video")
        for raw in data.get("models", []):
            path = f"{raw.get('owner')}/{raw.get('name')}"
            if path and path != "None/None":
                models[path] = _model(raw, curated.get(path))
        return list(models.values())

    def _input(self, mode: str, params: dict) -> dict:
        body = {"prompt": params.get("prompt", "")}
        for key in ("duration", "resolution", "aspect_ratio", "seed"):
            if params.get(key) not in (None, ""):
                body[key] = params[key]
        if mode == "img2video":
            image = params.get("image_url") or params.get("image_data_uri")
            if image:
                body["image"] = image
        extra = params.get("provider_params")
        if isinstance(extra, dict):
            reserved = {"prompt", "duration", "resolution", "aspect_ratio", "seed", "image",
                        "estimate_usd"}
            body.update({k: v for k, v in extra.items() if k not in reserved})
        return body

    def submit(self, model: CloudVideoModel, mode: str, params: dict) -> SubmitResult:
        response = _request("POST", f"{_API}/models/{model.provider_model}/predictions",
                            {"input": self._input(mode, params)})
        prediction_id = response.get("id")
        if not prediction_id:
            raise RuntimeError(f"Replicate returned no prediction id: {str(response)[:300]}")
        return SubmitResult(provider_job_id=prediction_id, raw=response)

    def poll(self, model: CloudVideoModel, submit_raw: dict) -> JobStatus:
        url = (submit_raw.get("urls") or {}).get("get")
        if not url:
            url = f"{_API}/predictions/{submit_raw.get('id')}"
        result = _request("GET", url)
        state = result.get("status")
        if state in ("starting", "processing"):
            return JobStatus(state="running", raw=result)
        if state in ("failed", "canceled"):
            return JobStatus(state="error", error=str(result.get("error") or state), raw=result)
        if state != "succeeded":
            return JobStatus(state="running", raw=result)
        return JobStatus(state="done", result_url=_extract_url(result.get("output")), raw=result)

    def cancel(self, model: CloudVideoModel, submit_raw: dict) -> bool:
        url = (submit_raw.get("urls") or {}).get("cancel")
        if not url:
            return False
        try:
            _request("POST", url)
            return True
        except Exception:
            return False


def _extract_url(output) -> Optional[str]:
    if isinstance(output, str) and output.startswith("http"):
        return output
    if isinstance(output, list):
        for item in output:
            url = _extract_url(item)
            if url:
                return url
    if isinstance(output, dict):
        for key in ("url", "video", "output"):
            url = _extract_url(output.get(key))
            if url:
                return url
    return None
