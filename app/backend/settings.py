"""
Persistent app settings.

Stored as JSON at `app/backend/settings.json` (gitignored). Currently holds
the Hugging Face token; structured as a dict so we can add more keys later
without rev-bumping the file format.

The token is read/written via the get_hf_token / set_hf_token helpers; the
download manager falls back to this token whenever the user doesn't pass an
explicit per-download token. Atomic writes (tmp → rename) so a crash mid-save
can't corrupt the file.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


_PATH = Path(__file__).resolve().parent / "settings.json"
_LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "hf_token": "",
}

_cache: dict[str, Any] = {}
_loaded = False


def _secure_permissions(path: Path) -> None:
    """Keep the saved Hugging Face token readable only by its owner."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_if_needed() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        if _PATH.exists():
            _secure_permissions(_PATH)
            data = json.loads(_PATH.read_text())
            if isinstance(data, dict):
                _cache = {**DEFAULTS, **data}
            else:
                _cache = dict(DEFAULTS)
        else:
            _cache = dict(DEFAULTS)
    except Exception:
        _cache = dict(DEFAULTS)
    _loaded = True


def get(key: str) -> Any:
    with _LOCK:
        _load_if_needed()
        return _cache.get(key, DEFAULTS.get(key))


def set_value(key: str, value: Any) -> None:
    with _LOCK:
        _load_if_needed()
        _cache[key] = value
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_cache, indent=2))
        _secure_permissions(tmp)
        os.replace(tmp, _PATH)
        _secure_permissions(_PATH)


def get_hf_token() -> Optional[str]:
    token = get("hf_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def set_hf_token(token: Optional[str]) -> None:
    set_value("hf_token", (token or "").strip())


def serialize_public() -> dict:
    """
    Caller-safe view: never includes the raw token. Returns a masked preview
    (first 3 + last 4 chars) so users can confirm the right token is saved.
    """
    token = get_hf_token()
    if not token:
        return {"hf_token_set": False, "hf_token_masked": ""}
    masked = token[:3] + "…" + token[-4:] if len(token) >= 10 else "•" * len(token)
    return {"hf_token_set": True, "hf_token_masked": masked}
