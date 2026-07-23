"""Automatic retention and hard-cap cleanup for completed video outputs."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import HTTPException

SETTINGS_FILE = Path(__file__).resolve().parent / "storage_policy.json"
POLICY_VERSION = 2
DEFAULTS = {"enabled": True, "retention_days": 30, "max_gb": 80.0}
_LOCK = threading.RLock()
_START_LOCK = threading.Lock()
_STARTED = False


def _write(value: dict) -> None:
    payload = {**value, "policy_version": POLICY_VERSION}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    partial = SETTINGS_FILE.with_suffix(".json.tmp")
    partial.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, SETTINGS_FILE)


def _read() -> dict:
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    saved = value if isinstance(value, dict) else {}
    out = {**DEFAULTS, **saved}
    if not isinstance(out["enabled"], bool): out["enabled"] = True
    try:
        out["retention_days"] = int(out["retention_days"])
        out["max_gb"] = float(out["max_gb"])
    except (TypeError, ValueError):
        out.update(
            retention_days=DEFAULTS["retention_days"],
            max_gb=DEFAULTS["max_gb"],
        )
    if not 1 <= out["retention_days"] <= 3650:
        out["retention_days"] = DEFAULTS["retention_days"]
    if not 1 <= out["max_gb"] <= 1000: out["max_gb"] = 80.0
    try:
        policy_version = int(saved.get("policy_version", 1))
    except (TypeError, ValueError):
        policy_version = 1
    if policy_version < POLICY_VERSION and out["retention_days"] == 3:
        out["retention_days"] = DEFAULTS["retention_days"]
        _write(out)
    return {key: out[key] for key in DEFAULTS}


def save(enabled: object, retention_days: object, max_gb: object) -> dict:
    if not isinstance(enabled, bool): raise HTTPException(400, "enabled must be true or false")
    try:
        days, maximum = int(retention_days), float(max_gb)
    except (TypeError, ValueError):
        raise HTTPException(400, "retention_days and max_gb must be numbers")
    if not 1 <= days <= 3650: raise HTTPException(400, "retention_days must be between 1 and 3650")
    if not 1 <= maximum <= 1000: raise HTTPException(400, "max_gb must be between 1 and 1000")
    value = {"enabled": enabled, "retention_days": days, "max_gb": maximum}
    _write(value)
    return value


def _snapshot(output_dir: Path) -> dict:
    rows = []
    if output_dir.exists():
        for path in output_dir.glob("*.mp4"):
            try:
                if path.is_symlink() or not path.is_file(): continue
                stat = path.stat(); rows.append((path, stat.st_size, stat.st_mtime))
            except OSError: continue
    rows.sort(key=lambda row: row[2])
    return {"used_bytes": sum(row[1] for row in rows), "count": len(rows),
            "oldest_at": rows[0][2] if rows else None,
            "newest_at": rows[-1][2] if rows else None, "rows": rows}


def status(manager, output_dir: Path) -> dict:
    policy, snap = _read(), _snapshot(output_dir)
    maximum = round(policy["max_gb"] * 1024 ** 3)
    return {**policy, **{k: v for k, v in snap.items() if k != "rows"},
            "max_bytes": maximum,
            "over_limit": policy["enabled"] and snap["used_bytes"] > maximum,
            "scope": "generated video outputs only"}


def _remove(manager, path: Path) -> int:
    job = manager.get(path.stem)
    if job is not None and str(getattr(job, "state", "")) not in {"done", "error", "cancelled"}: return 0
    try: size = path.stat().st_size
    except OSError: return 0
    if job is not None:
        if not manager.delete_job(path.stem): return 0
    else:
        try: path.unlink()
        except OSError: return 0
    return size


def enforce(manager, output_dir: Path, target_bytes: int | None = None) -> dict:
    with _LOCK:
        policy, before = _read(), _snapshot(output_dir)
        result = {"enabled": policy["enabled"], "retention_days": policy["retention_days"],
                  "max_gb": policy["max_gb"], "used_before_bytes": before["used_bytes"],
                  "used_bytes": before["used_bytes"], "deleted": 0, "freed_bytes": 0}
        if not policy["enabled"] and target_bytes is None: return result
        maximum = max(0, int(target_bytes)) if target_bytes is not None else round(policy["max_gb"] * 1024 ** 3)
        cutoff = time.time() - policy["retention_days"] * 86400
        for path, _size, modified in list(before["rows"]):
            if modified < cutoff:
                freed = _remove(manager, path)
                if freed: result["deleted"] += 1; result["freed_bytes"] += freed
        snap = _snapshot(output_dir)
        for path, _size, _modified in list(snap["rows"]):
            if snap["used_bytes"] <= maximum: break
            freed = _remove(manager, path)
            if freed:
                result["deleted"] += 1; result["freed_bytes"] += freed
                snap["used_bytes"] = max(0, snap["used_bytes"] - freed)
        final = _snapshot(output_dir)
        result.update(used_bytes=final["used_bytes"], count=final["count"], max_bytes=maximum,
                      over_limit=final["used_bytes"] > maximum)
        return result


def start_background(manager, output_dir: Path) -> None:
    global _STARTED
    with _START_LOCK:
        if _STARTED: return
        _STARTED = True
    def loop() -> None:
        time.sleep(60)
        while True:
            try: enforce(manager, output_dir)
            except Exception as exc: print(f"[storage] automatic cleanup failed: {exc}", flush=True)
            time.sleep(3600)
    threading.Thread(target=loop, name="output-storage-cleanup", daemon=True).start()
