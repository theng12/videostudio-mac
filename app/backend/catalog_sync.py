"""Persistent cloud catalog freshness, status diffing, and TTL refresh."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Iterable

from .providers.base import CloudVideoModel, VideoProvider

CACHE_FILE = Path(__file__).resolve().parent / "cloud_catalog.json"
TTL_S = 30 * 60
NEW_FOR_S = 14 * 24 * 60 * 60
DEPRECATED_FOR_S = 30 * 24 * 60 * 60
_LOCK = threading.RLock()
_started = False


def _load() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, CACHE_FILE)


def _from_dict(raw: dict) -> CloudVideoModel:
    fields = dict(raw)
    for key in ("capabilities", "resolutions", "aspect_ratios"):
        fields[key] = tuple(fields.get(key) or ())
    return CloudVideoModel(**fields)


def models_for(provider: VideoProvider, *, force: bool = False) -> list[CloudVideoModel]:
    """Return the persisted provider snapshot, refreshing when stale/forced.

    A failed network refresh never deprecates everything: the last good snapshot
    remains available and the failure is retried on the next TTL/on-demand call.
    """
    now = time.time()
    with _LOCK:
        store = _load()
        state = store.get(provider.key) or {}
        cached = [_from_dict(x) for x in state.get("models", [])]
        stale = now - float(state.get("refreshed_at") or 0) >= TTL_S
        if cached and not force and not stale:
            return _visible(cached, now)

        try:
            fresh = provider.list_models()
        except Exception as exc:
            if cached:
                print(f"[catalog-sync] {provider.key} refresh failed; keeping cache: {exc}", flush=True)
                return _visible(cached, now)
            raise

        old = {m.id: m for m in cached}
        merged: list[CloudVideoModel] = []
        seen: set[str] = set()
        for model in fresh:
            seen.add(model.id)
            previous = old.get(model.id)
            first_seen = previous.first_seen if previous and previous.first_seen else now
            status = "new" if now - first_seen < NEW_FOR_S else "available"
            if model.status == "deprecated":
                status = "deprecated"
            merged.append(replace(
                model,
                first_seen=first_seen,
                status=status,
                deprecated_at=(previous.deprecated_at if previous else None)
                    if status == "deprecated" else None,
            ))

        for model_id, previous in old.items():
            if model_id in seen:
                continue
            deprecated_at = previous.deprecated_at or now
            if now - deprecated_at <= DEPRECATED_FOR_S:
                merged.append(replace(previous, status="deprecated", deprecated_at=deprecated_at))

        store[provider.key] = {
            "refreshed_at": now,
            "models": [asdict(m) for m in merged],
        }
        _save(store)
        return _visible(merged, now)


def _visible(models: list[CloudVideoModel], now: float) -> list[CloudVideoModel]:
    return [m for m in models
            if m.status != "deprecated" or not m.deprecated_at
            or now - m.deprecated_at <= DEPRECATED_FOR_S]


def start_background(provider_source: Callable[[], Iterable[VideoProvider]]) -> None:
    global _started
    with _LOCK:
        if _started:
            return
        _started = True

    def worker() -> None:
        while True:
            for provider in provider_source():
                try:
                    models_for(provider)
                except Exception as exc:
                    print(f"[catalog-sync] {provider.key}: {exc}", flush=True)
            time.sleep(TTL_S)

    threading.Thread(target=worker, name="cloud-catalog-sync", daemon=True).start()
