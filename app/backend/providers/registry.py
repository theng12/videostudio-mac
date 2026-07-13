"""Provider registry: which providers exist, key/paid config, id dispatch, and
the helpers that merge cloud models into /api/catalog."""
from __future__ import annotations

from typing import Optional

from .. import settings as app_settings
from .base import CloudVideoModel, VideoProvider, serialize_cloud_model
from .fal import FalProvider

# Linked providers. Add new adapters here (Phase 2: kie, replicate).
PROVIDERS: dict[str, VideoProvider] = {
    "fal": FalProvider(),
}


def is_cloud_id(model_id: str) -> bool:
    """True if `model_id` addresses a cloud model (``<provider>:…``) whose
    provider is registered."""
    if ":" not in model_id:
        return False
    return model_id.split(":", 1)[0] in PROVIDERS


def provider_for_id(model_id: str) -> Optional[tuple[VideoProvider, CloudVideoModel]]:
    if not is_cloud_id(model_id):
        return None
    prov = PROVIDERS[model_id.split(":", 1)[0]]
    model = prov.get_model(model_id)
    if model is None:
        return None
    return prov, model


def _key_set(prov: VideoProvider) -> bool:
    try:
        return prov.has_key()
    except Exception:
        return False


def _paid_on(prov: VideoProvider) -> bool:
    return bool((app_settings.get("providers") or {}).get(prov.key, {}).get("paid", False))


# ── catalog merge (consumed by /api/catalog) ──

def cloud_models_serialized() -> list[dict]:
    out: list[dict] = []
    for prov in PROVIDERS.values():
        ks, paid = _key_set(prov), _paid_on(prov)
        try:
            models = prov.list_models()
        except Exception:
            models = []
        for m in models:
            out.append(serialize_cloud_model(m, key_set=ks, paid_on=paid))
    return out


def cloud_families() -> dict:
    """Family entries for the cloud providers, shaped like serialize_family()."""
    fams: dict = {}
    for prov in PROVIDERS.values():
        fid = f"cloud-{prov.key}"
        fams[fid] = {
            "id": fid,
            "label": f"{prov.name} · cloud",
            "monogram": prov.name[:2].upper(),
            "accent": "#58a6ff",
            "summary": f"Cloud video models served through {prov.name}. "
                       f"No local download or GPU — generation runs on the provider "
                       f"and is billed per use.",
            "how_to_use": "Add your API key in Settings, enable paid use, then pick a "
                          "model and generate like any local one.",
            "is_cloud": True,
            "provider": prov.key,
        }
    return fams


# ── provider management (consumed by /api/providers) ──

def providers_status() -> list[dict]:
    from .. import spend
    out = []
    for prov in PROVIDERS.values():
        try:
            n_models = len(prov.list_models())
        except Exception:
            n_models = 0
        out.append({
            "key": prov.key,
            "name": prov.name,
            "docs_url": prov.docs_url,
            "key_set": _key_set(prov),
            "paid": _paid_on(prov),
            "model_count": n_models,
            "spend": spend.provider_summary(prov.key),
        })
    return out


def set_key(provider: str, key: Optional[str]) -> None:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    providers = dict(app_settings.get("providers") or {})
    cfg = dict(providers.get(provider) or {})
    cfg["key"] = (key or "").strip()
    providers[provider] = cfg
    app_settings.set_value("providers", providers)


def set_paid(provider: str, on: bool) -> None:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    providers = dict(app_settings.get("providers") or {})
    cfg = dict(providers.get(provider) or {})
    cfg["paid"] = bool(on)
    providers[provider] = cfg
    app_settings.set_value("providers", providers)


def refresh(provider: str) -> int:
    """Force a re-read of the provider's model list. Returns the model count.
    (Phase 1: re-reads the curated file; Phase 2 will re-fetch live.)"""
    if provider not in PROVIDERS:
        raise KeyError(provider)
    prov = PROVIDERS[provider]
    if hasattr(prov, "_cache"):
        prov._cache = None  # type: ignore[attr-defined]
    return len(prov.list_models())
