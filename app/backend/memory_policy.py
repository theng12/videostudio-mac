"""Persistent opt-in local video pipeline memory policy."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import HTTPException


SETTINGS_FILE = Path(__file__).resolve().parent / "memory_policy.json"
MODES = {
    "performance": {"idle_seconds": None, "label": "Performance"},
    "balanced": {"idle_seconds": 600, "label": "Balanced"},
    "memory_saver": {"idle_seconds": 120, "label": "Memory Saver"},
    "immediate": {"idle_seconds": 0, "label": "Immediate"},
}
DEFAULT_MODE = "performance"
CHECK_INTERVAL_SECONDS = 5

_LOCK = threading.RLock()
_START_LOCK = threading.Lock()
_STARTED = False
_MANAGER = None
_LAST_RELEASE_AT: float | None = None
_LAST_RELEASE_REASON: str | None = None
_LAST_RELEASE_DETAILS: dict | None = None
_LAST_ERROR: str | None = None
_RELEASE_COUNT = 0
_RELEASING = False


def _read() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    mode = raw.get("mode") if isinstance(raw, dict) else None
    return {"mode": mode if mode in MODES else DEFAULT_MODE}


def save(mode: object) -> dict:
    if not isinstance(mode, str) or mode not in MODES:
        raise HTTPException(400, f"mode must be one of: {', '.join(MODES)}")
    value = {"mode": mode}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    partial = SETTINGS_FILE.with_suffix(".json.tmp")
    partial.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, SETTINGS_FILE)
    return value


def _release(reason: str) -> dict:
    global _LAST_RELEASE_AT, _LAST_RELEASE_REASON, _LAST_RELEASE_DETAILS
    global _LAST_ERROR, _RELEASE_COUNT, _RELEASING
    with _LOCK:
        if _RELEASING:
            raise HTTPException(409, "A memory release is already running")
        if _MANAGER is None:
            raise HTTPException(503, "The video generation manager is not ready")
        if _MANAGER.has_active_local_jobs():
            raise HTTPException(409, "A local video render is queued or running; memory was not released")
        _RELEASING = True
    try:
        details = _MANAGER.release_memory(reason=reason)
        with _LOCK:
            _LAST_RELEASE_AT = time.time()
            _LAST_RELEASE_REASON = reason
            _LAST_RELEASE_DETAILS = details
            _LAST_ERROR = None
            _RELEASE_COUNT += 1
            _RELEASING = False
        print(f"[memory] released accelerator memory ({reason}): {details}", flush=True)
        return status()
    except HTTPException:
        raise
    except Exception as exc:
        with _LOCK:
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        raise HTTPException(409, f"Memory release deferred: {exc}") from exc
    finally:
        with _LOCK:
            _RELEASING = False


def release_now() -> dict:
    return _release("manual")


def run_due_release(now: float | None = None) -> dict | None:
    current = time.time() if now is None else float(now)
    with _LOCK:
        mode = _read()["mode"]
        threshold = MODES[mode]["idle_seconds"]
        manager = _MANAGER
        if threshold is None or manager is None or _RELEASING:
            return None
        if manager.has_active_local_jobs() or not manager.has_loaded_pipeline():
            return None
        idle = manager.idle_seconds(now=current)
        if idle is None or idle < threshold:
            return None
        last_activity = manager.last_activity_at()
        if _LAST_RELEASE_AT is not None and last_activity is not None and _LAST_RELEASE_AT >= last_activity:
            return None
    return _release(f"automatic:{mode}")


def status() -> dict:
    with _LOCK:
        mode = _read()["mode"]
        threshold = MODES[mode]["idle_seconds"]
        manager = _MANAGER
        loaded_key = manager.loaded_pipeline_key() if manager else None
        idle = manager.idle_seconds() if manager else None
        due_at = None
        if threshold is not None and loaded_key and idle is not None:
            due_at = time.time() + max(0, threshold - idle)
        active = manager.has_active_local_jobs() if manager else False
        return {
            "mode": mode,
            "default_mode": DEFAULT_MODE,
            "idle_seconds": threshold,
            "options": [{"mode": key, **value} for key, value in MODES.items()],
            "loaded_pipeline": list(loaded_key) if loaded_key else None,
            "pipeline_idle_seconds": idle,
            "active_local_jobs": bool(active),
            "busy": bool(active or _RELEASING),
            "next_release_at": due_at,
            "last_release_at": _LAST_RELEASE_AT,
            "last_release_reason": _LAST_RELEASE_REASON,
            "last_release_details": _LAST_RELEASE_DETAILS,
            "last_error": _LAST_ERROR,
            "release_count": _RELEASE_COUNT,
        }


def start_background(manager) -> None:
    global _MANAGER, _STARTED
    _MANAGER = manager
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True

    def loop() -> None:
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            try:
                run_due_release()
            except HTTPException as exc:
                print(f"[memory] automatic release deferred: {exc.detail}", flush=True)
            except Exception as exc:
                print(f"[memory] automatic release failed: {exc}", flush=True)

    threading.Thread(target=loop, name="memory-policy", daemon=True).start()
