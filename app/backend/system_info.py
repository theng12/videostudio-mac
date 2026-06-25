"""
System hardware introspection.

Detects the Apple Silicon chip name + unified memory size so the UI can show
per-model "fits / tight / won't fit" hints. Mac-only — this whole app is
darwin-arm64 per pinokio.json, so we just shell out to sysctl which is always
present.

All probes are cheap and best-effort: if anything fails (sandboxed environment,
non-Mac dev machine, etc.) we return None and let the UI render an "unknown"
state instead of crashing.

Used by:
- /api/system endpoint — full hardware snapshot
- /api/catalog response — per-model `fit` field
- Frontend Models tab — color-coded fit chip per model card
"""
from __future__ import annotations

import platform
import subprocess
from functools import lru_cache
from typing import Optional


def _sysctl(key: str) -> Optional[str]:
    """Run `sysctl -n <key>` and return stdout, or None on any failure.
    2-second timeout so a hung sysctl can't block startup."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", key],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def detect_chip() -> Optional[str]:
    """Apple Silicon chip name, e.g. 'Apple M2 Pro'. None if unavailable.
    Cached because the chip doesn't change while the process is running."""
    return _sysctl("machdep.cpu.brand_string")


@lru_cache(maxsize=1)
def detect_memory_gb() -> Optional[int]:
    """Unified memory in GB (rounded). None if unavailable.
    Cached — RAM is fixed once the kernel booted."""
    raw = _sysctl("hw.memsize")
    if raw:
        try:
            return round(int(raw) / (1024 ** 3))
        except ValueError:
            pass
    return None


def _chip_tier(chip: Optional[str]) -> Optional[int]:
    """A simplified ordinal for the chip's tier within its generation.
    1 = base (M1, M2, M3, M4)
    2 = Pro
    3 = Max
    4 = Ultra
    Useful when we want a rough "is this an enthusiast machine?" check
    without parsing the full chip name."""
    if not chip:
        return None
    c = chip.lower()
    if "ultra" in c: return 4
    if "max"   in c: return 3
    if "pro"   in c: return 2
    if "apple m" in c: return 1
    return None


def system_info() -> dict:
    """Full hardware snapshot for /api/system + per-model fit calculation."""
    chip = detect_chip()
    memory_gb = detect_memory_gb()
    return {
        "platform": platform.system().lower(),       # 'darwin' / 'linux' / 'windows'
        "arch": platform.machine(),                  # 'arm64' on Apple Silicon
        "chip": chip,                                # 'Apple M4' / 'Apple M2 Pro' / None
        "chip_tier": _chip_tier(chip),               # 1..4 per the ladder above
        "unified_memory_gb": memory_gb,              # 8 / 16 / 32 / 64 etc.
    }


def fit_for(model_min_memory_gb: int) -> dict:
    """
    Compute a fit verdict for the current machine vs a model's memory floor.

    States (frontend renders as colored chips):
    - "ok"      → green:  ≥1.5× headroom — runs without thinking about it
    - "tight"   → yellow: meets the floor but no headroom — close other apps
    - "risky"   → red:    below the floor — will swap heavily or OOM
    - "unknown" → grey:   couldn't probe sysctl, surface model's own hint instead

    The 1.5× threshold is empirical: on Apple Silicon, the model weights + the OS itself +
    a browser usually eats 4-6 GB before the model loads, so a 16 GB Mac
    running a "≥8 GB" model is actually fine, but a 16 GB Mac running a
    "≥16 GB" model is genuinely tight.
    """
    actual = detect_memory_gb()
    if actual is None:
        return {
            "state": "unknown",
            "label": "?",
            "hint": "Couldn't detect RAM — falling back to the model's recommended_hardware.",
            "actual_gb": None,
            "required_gb": model_min_memory_gb,
        }
    headroom = actual / max(model_min_memory_gb, 1)
    if headroom >= 1.5:
        return {
            "state": "ok",
            "label": "fits comfortably",
            "hint": f"Your {actual} GB ≥ 1.5× the {model_min_memory_gb} GB floor — plenty of headroom.",
            "actual_gb": actual,
            "required_gb": model_min_memory_gb,
        }
    if headroom >= 1.0:
        return {
            "state": "tight",
            "label": "tight — close other apps",
            "hint": (
                f"Your {actual} GB just clears the {model_min_memory_gb} GB floor. "
                "Quit Chrome / IDE / other heavy apps before generating, otherwise "
                "you may swap to disk and slow generations dramatically."
            ),
            "actual_gb": actual,
            "required_gb": model_min_memory_gb,
        }
    return {
        "state": "risky",
        "label": "may not fit",
        "hint": (
            f"Your {actual} GB is below the {model_min_memory_gb} GB floor. "
            "The model will swap heavily to disk (very slow) or fail with OOM. "
            "Pick a smaller quantization or a different family."
        ),
        "actual_gb": actual,
        "required_gb": model_min_memory_gb,
    }
