"""Spend guardrails + records for cloud generation (SPEC §7).

Cloud video costs real money per job, so this is first-class. A small SQLite
store (`spend.db`, gitignored, at the launcher root) records every cloud job's
estimated and actual cost. Two cap scopes are enforced *together* (the tighter
wins): per-provider and global, each with a daily and a monthly USD limit.
Resets are calendar-based in the host's local timezone (daily at local midnight,
monthly on the 1st). A pre-submit gate blocks a job whose estimate would push
any relevant window over its cap — before a cent is spent.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import settings as app_settings

DB_FILE = Path(__file__).resolve().parent.parent.parent / "spend.db"
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  job_id TEXT,
  est_usd REAL,
  actual_usd REAL,
  duration_s REAL,
  state TEXT NOT NULL            -- submitted | done | error | cancelled
);
CREATE INDEX IF NOT EXISTS idx_spend_ts ON spend(ts DESC);
CREATE INDEX IF NOT EXISTS idx_spend_provider ON spend(provider);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ── calendar windows (local time) ──

def _day_start(now: float | None = None) -> float:
    d = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    return _dt.datetime(d.year, d.month, d.day).timestamp()


def _month_start(now: float | None = None) -> float:
    d = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    return _dt.datetime(d.year, d.month, 1).timestamp()


def _window_total(provider: str | None, since: float) -> float:
    """Cost booked in [since, now). Prefers actual_usd, falls back to est_usd for
    still-running jobs. Excludes error/cancelled rows."""
    where = "ts >= ? AND state IN ('submitted','done')"
    args: list = [since]
    if provider is not None:
        where += " AND provider = ?"
        args.append(provider)
    with _conn() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(actual_usd, est_usd, 0)), 0) FROM spend WHERE {where}",
            args).fetchone()
    return float(row[0] or 0.0)


# ── caps ──

def _caps() -> dict:
    c = app_settings.get("spend_caps") or {}
    return {
        "global": {"daily": float((c.get("global") or {}).get("daily") or 0),
                   "monthly": float((c.get("global") or {}).get("monthly") or 0)},
        "per_provider": c.get("per_provider") or {},
    }


def _provider_cap(provider: str) -> dict:
    pp = (_caps()["per_provider"].get(provider) or {})
    return {"daily": float(pp.get("daily") or 0), "monthly": float(pp.get("monthly") or 0)}


def set_caps(caps: dict) -> None:
    """Persist caps. Shape: {"global":{"daily","monthly"}, "per_provider":{prov:{...}}}.
    Values are coerced to non-negative floats; 0 = no cap."""
    def _n(x):
        try:
            return max(0.0, float(x or 0))
        except (TypeError, ValueError):
            return 0.0
    g = caps.get("global") or {}
    clean = {
        "global": {"daily": _n(g.get("daily")), "monthly": _n(g.get("monthly"))},
        "per_provider": {},
    }
    for prov, pv in (caps.get("per_provider") or {}).items():
        pv = pv or {}
        clean["per_provider"][prov] = {"daily": _n(pv.get("daily")), "monthly": _n(pv.get("monthly"))}
    app_settings.set_value("spend_caps", clean)


class CapExceeded(Exception):
    """Raised by check_gate when a projected total would exceed a cap."""


def check_gate(provider: str, est_usd: float | None) -> None:
    """Block if adding `est_usd` would push any relevant window over its cap.
    A None estimate can't be gated numerically — it's allowed through, but the
    actual cost still books and can trip the cap for the *next* job."""
    if not est_usd or est_usd <= 0:
        return
    now = time.time()
    checks = [
        ("global daily", None, _caps()["global"]["daily"], _day_start(now)),
        ("global monthly", None, _caps()["global"]["monthly"], _month_start(now)),
        (f"{provider} daily", provider, _provider_cap(provider)["daily"], _day_start(now)),
        (f"{provider} monthly", provider, _provider_cap(provider)["monthly"], _month_start(now)),
    ]
    for label, prov, cap, since in checks:
        if cap <= 0:
            continue  # no cap
        current = _window_total(prov, since)
        if current + est_usd > cap + 1e-9:
            raise CapExceeded(
                f"{label} cap would be exceeded: ${current:.2f} spent + ${est_usd:.2f} "
                f"estimate > ${cap:.2f} limit. Raise the cap in Settings or wait for the window to reset."
            )


# ── records ──

def record_submit(provider: str, model: str, job_id: str, est_usd: float | None) -> str:
    sid = uuid.uuid4().hex[:12]
    with _LOCK, _conn() as conn:
        conn.execute(
            "INSERT INTO spend (id, ts, provider, model, job_id, est_usd, state) "
            "VALUES (?,?,?,?,?,?, 'submitted')",
            (sid, time.time(), provider, model, job_id, est_usd))
    return sid


def record_finish(spend_id: str, *, actual_usd: float | None, duration_s: float | None,
                  state: str = "done") -> None:
    with _LOCK, _conn() as conn:
        conn.execute(
            "UPDATE spend SET actual_usd = ?, duration_s = ?, state = ? WHERE id = ?",
            (actual_usd, duration_s, state, spend_id))


# ── summaries (for /api/spend + the UI) ──

def provider_summary(provider: str) -> dict:
    now = time.time()
    cap = _provider_cap(provider)
    return {
        "today": round(_window_total(provider, _day_start(now)), 4),
        "month": round(_window_total(provider, _month_start(now)), 4),
        "cap_daily": cap["daily"],
        "cap_monthly": cap["monthly"],
    }


def summary() -> dict:
    now = time.time()
    g = _caps()["global"]
    with _conn() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT ts, provider, model, est_usd, actual_usd, duration_s, state "
            "FROM spend ORDER BY ts DESC LIMIT 50").fetchall()]
        history_rows = conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, provider, "
            "SUM(COALESCE(actual_usd, est_usd, 0)) AS usd "
            "FROM spend WHERE ts >= ? AND state IN ('submitted','done') "
            "GROUP BY day, provider ORDER BY day",
            (_day_start(now) - 13 * 86400,),
        ).fetchall()
    history: dict[str, dict] = {}
    today = _dt.datetime.fromtimestamp(now).date()
    for offset in range(13, -1, -1):
        day = (today - _dt.timedelta(days=offset)).isoformat()
        history[day] = {"day": day, "total": 0.0, "providers": {}}
    for row in history_rows:
        if row["day"] not in history:
            continue
        usd = round(float(row["usd"] or 0), 4)
        history[row["day"]]["providers"][row["provider"]] = usd
        history[row["day"]]["total"] = round(history[row["day"]]["total"] + usd, 4)
    providers = {}
    for prov in (app_settings.get("providers") or {}).keys():
        providers[prov] = provider_summary(prov)
    return {
        "global": {
            "today": round(_window_total(None, _day_start(now)), 4),
            "month": round(_window_total(None, _month_start(now)), 4),
            "cap_daily": g["daily"],
            "cap_monthly": g["monthly"],
        },
        "per_provider": providers,
        "caps": _caps(),
        "recent": recent,
        "daily_history": list(history.values()),
    }
