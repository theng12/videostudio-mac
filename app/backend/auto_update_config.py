"""Video Studio's fixed, non-user-editable updater identity."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .auto_update import AutoUpdater


ROOT = Path(__file__).resolve().parents[2]
SPEC = {
    "root": str(ROOT),
    "title": "Video Studio KH",
    "slug": "videostudio",
    "expected_remote": "https://github.com/theng12/videostudio-mac.git",
    "branch": "main",
    "port": 47872,
    "server_label": "com.kh.videostudio.server",
    "watchdog_label": "com.kh.videostudio.watchdog",
    "default_hour": 6,
    "default_weekday": 6,
    "verify_module": "backend.main",
    "generation_marker": "diffusers",
    "generation_requirements": "requirements-generation.txt",
}


def create_updater(readiness: Optional[Callable[[], list[str]]] = None, **kwargs) -> AutoUpdater:
    return AutoUpdater(SPEC, readiness=readiness, **kwargs)
