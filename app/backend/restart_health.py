"""Read-only watchdog restart-rate evidence for operators and Studio Hub."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re


LAUNCHER_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_LOG = LAUNCHER_ROOT / "logs" / "service" / "watchdog.log"
_MAX_LOG_BYTES = 256 * 1024
_RESTART_LINE = re.compile(
    r"^\[watchdog\] (?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?:no /api/health\b|health probe failed \d+ consecutive times\b)"
)


def _tail_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _MAX_LOG_BYTES))
            raw = handle.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def restart_rate_snapshot(
    path: Path = WATCHDOG_LOG,
    *,
    now: datetime | None = None,
) -> dict:
    observed_at = now or datetime.now()
    events: list[datetime] = []
    for line in _tail_text(path).splitlines():
        match = _RESTART_LINE.match(line.strip())
        if not match:
            continue
        try:
            event = datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S")
            if event <= observed_at:
                events.append(event)
        except ValueError:
            continue
    restarts_24h = sum(event >= observed_at - timedelta(hours=24) for event in events)
    restarts_7d = sum(event >= observed_at - timedelta(days=7) for event in events)
    if restarts_24h >= 6 or restarts_7d >= 20:
        status = "critical"
    elif restarts_24h >= 2 or restarts_7d >= 5:
        status = "warning"
    else:
        status = "healthy"
    return {
        "status": status,
        "alert": status in {"warning", "critical"},
        "restarts_24h": restarts_24h,
        "restarts_7d": restarts_7d,
        "last_restart_at": max(events).isoformat(timespec="seconds") if events else None,
        "observed_at": observed_at.isoformat(timespec="seconds"),
    }
