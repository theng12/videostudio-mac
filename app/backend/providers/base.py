"""Provider interface + shared types for the cloud video gateway.

A provider is a thin adapter over a heterogeneous async video API (fal, kie,
replicate, …). The registry dispatches a generation whose model id is
provider-prefixed (``fal:…``) to the matching adapter; the cloud job runner
drives submit → poll → download through this interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CloudVideoModel:
    """One cloud model, provider-agnostic. `id` is the gateway id used as the
    catalog `repo` and in generation requests, e.g. ``fal:fal-ai/kling-video/v2``."""
    id: str
    provider: str
    label: str
    capabilities: tuple[str, ...] = ("txt2video",)   # txt2video | img2video | video2video
    provider_model: str = ""          # the provider's own model path/slug
    summary: str = ""
    max_duration_s: Optional[float] = None
    resolutions: tuple[str, ...] = ()
    aspect_ratios: tuple[str, ...] = ()
    price_unit: Optional[str] = None   # "per_second" | "per_video"
    price_usd: Optional[float] = None  # USD for that unit
    status: str = "available"          # available | new | deprecated
    first_seen: float = 0.0
    deprecated_at: Optional[float] = None


@dataclass
class SubmitResult:
    provider_job_id: str
    raw: dict = field(default_factory=dict)


@dataclass
class JobStatus:
    state: str                         # queued | running | done | error
    result_url: Optional[str] = None   # the finished video URL (when done)
    error: Optional[str] = None
    progress: Optional[float] = None    # 0..1 if the provider reports it
    raw: dict = field(default_factory=dict)


class VideoProvider:
    """Adapter contract. Concrete providers (fal.py) implement the network bits;
    everything else in the gateway is provider-agnostic."""

    key: str = ""            # slug used in ids + settings, e.g. "fal"
    name: str = ""           # display name, e.g. "fal.ai"
    docs_url: str = ""

    def has_key(self) -> bool:
        raise NotImplementedError

    def list_models(self) -> list[CloudVideoModel]:
        raise NotImplementedError

    def get_model(self, model_id: str) -> Optional[CloudVideoModel]:
        for m in self.list_models():
            if m.id == model_id:
                return m
        return None

    def estimate_cost(self, model: CloudVideoModel, params: dict) -> Optional[float]:
        """USD estimate for one generation, or None if not determinable. Default:
        price_usd × requested duration for per_second, or price_usd for per_video."""
        if model.price_usd is None:
            return None
        if model.price_unit == "per_second":
            dur = _requested_duration_s(model, params)
            return round(model.price_usd * dur, 4)
        if model.price_unit == "per_video":
            return round(model.price_usd, 4)
        return None

    def submit(self, model: CloudVideoModel, mode: str, params: dict) -> SubmitResult:
        raise NotImplementedError

    def poll(self, model: CloudVideoModel, submit_raw: dict) -> JobStatus:
        """`submit_raw` is the dict from SubmitResult.raw (carries the provider's
        own status/result URLs), so we never reconstruct fragile URLs."""
        raise NotImplementedError

    def cancel(self, model: CloudVideoModel, submit_raw: dict) -> bool:
        return False


def _requested_duration_s(model: CloudVideoModel, params: dict) -> float:
    """Best-effort output duration for cost estimation. Prefers an explicit
    `duration`/`seconds` param, else frames/fps, else the model's max (or 5 s)."""
    for k in ("duration", "duration_s", "seconds"):
        v = params.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    frames = params.get("frames")
    fps = params.get("fps")
    if frames and fps:
        try:
            return max(0.1, float(frames) / float(fps))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return float(model.max_duration_s or 5.0)


def serialize_cloud_model(m: CloudVideoModel, *, key_set: bool, paid_on: bool) -> dict:
    """Emit a dict shaped like the local catalog's serialize_model() PLUS the
    cloud/Hub fields — so the existing frontend renders cloud models and the Hub
    slots them into its cloud lane (it filters on `is_cloud` / `hub_modality`)."""
    price = None
    if m.price_usd is not None:
        price = {"unit": m.price_unit, "usd": m.price_usd}
    return {
        # ── local-catalog-compatible surface ──
        "repo": m.id,
        "label": m.label,
        "family": f"cloud-{m.provider}",
        "family_label": f"{m.provider} · cloud",
        "variant_label": "",
        "role": "cloud",
        "size_gb": 0,                       # no download
        "gated": False,
        "quantization": None,
        "min_unified_memory_gb": 0,         # runs on the provider, not this Mac
        "recommended_hardware": "Runs in the cloud — no local GPU/RAM needed.",
        "apple_optimized": False,
        "aliases": [],
        "capabilities": list(m.capabilities),
        "best_for": m.summary,
        "use_cases": [],
        "fit": {"state": "ok", "label": "cloud", "hint": "Runs on the provider.",
                "actual_gb": None, "required_gb": 0},
        "video_defaults": {},
        # ── cloud / Hub fields ──
        "is_cloud": True,
        "hub_modality": "video",
        "provider": m.provider,
        "provider_model": m.provider_model,
        "cost_tier": "paid-cloud",
        "price": price,
        "status": m.status,
        "max_duration_s": m.max_duration_s,
        "resolutions": list(m.resolutions),
        "aspect_ratios": list(m.aspect_ratios),
        "key_set": key_set,
        "paid_on": paid_on,
        "deprecated_at": m.deprecated_at,
        # No download/cache concept for cloud — mirror the local shape so the UI
        # doesn't choke on a missing key.
        "cache": {"state": "cloud"},
        "active_download": None,
    }
