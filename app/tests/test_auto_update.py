from __future__ import annotations

import datetime as dt
from pathlib import Path
import subprocess
import sys

import pytest

from backend.auto_update import AutoUpdater, UpdateDeferred, UpdateError, _redact


@pytest.fixture
def updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AutoUpdater:
    root = tmp_path / "voicestudio-mac.git"
    (root / ".git").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "conda_env" / "bin").mkdir(parents=True)
    (root / "VERSION").write_text("1.0.0\n")
    (root / "app" / "requirements.txt").write_text("fastapi\n")
    python = root / "conda_env" / "bin" / "python"
    python.symlink_to(sys.executable)
    spec = {
        "root": str(root), "title": "Voice Studio KH", "slug": "voicestudio-test",
        "expected_remote": "https://github.com/theng12/voicestudio-mac.git",
        "branch": "main", "port": 47870, "default_hour": 2,
        "server_label": "com.kh.voicestudio.server",
        "watchdog_label": "com.kh.voicestudio.watchdog",
    }
    item = AutoUpdater(spec)
    monkeypatch.setattr(item, "scheduler_status", lambda: {
        "installed": item.settings()["mode"] != "off", "supported": True,
        "label": item.agent_label,
    })
    monkeypatch.setattr(item, "apply_scheduler", lambda force_pending=False: {
        "installed": item.settings()["mode"] != "off" or force_pending,
        "supported": True, "label": item.agent_label,
    })
    monkeypatch.setattr(item, "_notify", lambda *args: None)
    return item


def _save(updater: AutoUpdater, mode: str) -> dict:
    return updater.save_settings({
        "mode": mode, "frequency": "daily", "maintenance_hour": 2,
        "idle_only": True,
    })


def test_default_is_off_and_idle_only(updater: AutoUpdater):
    assert updater.settings() == {
        "mode": "off", "frequency": "daily", "maintenance_hour": 2,
        "idle_only": True, "weekday": 6,
    }
    assert updater.public_status()["scheduler"]["installed"] is False


def test_settings_modes_install_and_remove_schedule(updater: AutoUpdater):
    assert _save(updater, "notify")["scheduler"]["installed"] is True
    assert _save(updater, "auto")["scheduler"]["installed"] is True
    status = _save(updater, "off")
    assert status["scheduler"]["installed"] is False
    assert status["next_check"] is None


def test_invalid_settings_are_rejected(updater: AutoUpdater):
    with pytest.raises(UpdateError):
        updater.save_settings({"mode": "always", "frequency": "daily",
                               "maintenance_hour": 2, "idle_only": True})
    with pytest.raises(UpdateError):
        updater.save_settings({"mode": "auto", "frequency": "daily",
                               "maintenance_hour": 24, "idle_only": True})


def test_notify_only_checks_but_does_not_install(updater: AutoUpdater, monkeypatch):
    _save(updater, "notify")
    updater._write_status(next_check="2000-01-01T00:00:00Z")
    monkeypatch.setattr(updater, "check", lambda: {"update_available": True, "latest_version": "2.0.0"})
    called = []
    monkeypatch.setattr(updater, "update", lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(updater, "_notify", lambda *args: called.append("notify"))
    updater.scheduled()
    assert called == ["notify"]


def test_auto_mode_installs_available_update(updater: AutoUpdater, monkeypatch):
    _save(updater, "auto")
    updater._write_status(next_check="2000-01-01T00:00:00Z")
    monkeypatch.setattr(updater, "check", lambda: {"update_available": True, "latest_version": "2.0.0"})
    called = []
    monkeypatch.setattr(updater, "update", lambda **kwargs: called.append(kwargs) or {"state": "succeeded"})
    updater.scheduled()
    assert called == [{"automatic": True}]


def test_active_work_defers_and_records_reason(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: ["voice generation is running"])
    with pytest.raises(UpdateDeferred):
        updater.update(automatic=True)
    status = updater.public_status()
    assert status["state"] == "deferred"
    assert "voice generation" in status["defer_reason"]
    assert status["next_retry"]


def test_update_after_work_creates_pending_retry(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: ["download active"])
    status = updater.trigger_update(after_current=True)
    assert status["pending_manual"] is True
    assert status["state"] == "deferred"


def test_concurrent_update_lock_is_refused(updater: AutoUpdater):
    with updater._exclusive_lock():
        with pytest.raises(UpdateError, match="already running"):
            with updater._exclusive_lock():
                pass


@pytest.mark.parametrize("case, message", [
    ("remote", "Unexpected Git remote"),
    ("branch", "configured main branch"),
    ("dirty", "local changes"),
    ("diverged", "diverged"),
])
def test_git_safety_refusals(updater: AutoUpdater, monkeypatch, case, message):
    def fake_git(*args, **kwargs):
        command = tuple(args)
        if command == ("remote", "get-url", "origin"):
            return "https://github.com/attacker/wrong.git" if case == "remote" else updater.spec["expected_remote"]
        if command[:3] == ("symbolic-ref", "--quiet", "--short"):
            return "feature" if case == "branch" else "main"
        if command[:2] == ("status", "--porcelain"):
            return " M local.txt" if case == "dirty" else ""
        if command[:1] == ("fetch",):
            return ""
        if command == ("rev-parse", "HEAD"):
            return "a" * 40
        if command == ("rev-parse", "origin/main"):
            return "b" * 40
        if command[:1] == ("show",):
            return "2.0.0"
        raise AssertionError(command)
    monkeypatch.setattr(updater, "_git", fake_git)
    def fake_run(args, **kwargs):
        rc = 1 if case == "diverged" and "merge-base" in args else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(updater, "_run", fake_run)
    with pytest.raises(UpdateError, match=message):
        updater._git_preflight()


def test_disk_space_failure_happens_before_files_change(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr("backend.auto_update.shutil.disk_usage", lambda _p: type("D", (), {"free": 1})())
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: pytest.fail("Git update must not start"))
    with pytest.raises(UpdateError, match="disk space"):
        updater.update()


@pytest.mark.parametrize("failure", ["dependencies", "health"])
def test_install_or_health_failure_attempts_rollback(updater: AutoUpdater, monkeypatch, failure):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: {
        "local": "a" * 40, "remote": "b" * 40, "latest": "2.0.0", "available": True,
    })
    monkeypatch.setattr(updater, "_stop_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(updater, "_verify_import", lambda expected: None)
    monkeypatch.setattr(updater, "_start_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_verify_health", lambda mode, version: failure != "health")
    if failure == "dependencies":
        monkeypatch.setattr(updater, "_install_dependencies", lambda: (_ for _ in ()).throw(UpdateError("dependency install failed")))
    else:
        monkeypatch.setattr(updater, "_install_dependencies", lambda: None)
    rollbacks = []
    monkeypatch.setattr(updater, "_rollback", lambda *args: rollbacks.append(args) or True)
    with pytest.raises(UpdateError):
        updater.update()
    assert len(rollbacks) == 1
    assert updater.public_status()["rollback"] == "succeeded"


def test_rollback_failure_is_reported(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: {
        "local": "a" * 40, "remote": "b" * 40, "latest": "2.0.0", "available": True,
    })
    monkeypatch.setattr(updater, "_stop_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(updater, "_install_dependencies", lambda: (_ for _ in ()).throw(UpdateError("boom")))
    monkeypatch.setattr(updater, "_rollback", lambda *args: False)
    with pytest.raises(UpdateError):
        updater.update()
    assert updater.public_status()["rollback"] == "failed"


def test_service_and_pinokio_modes_restart_only_their_owner(updater: AutoUpdater, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_run", lambda args, **kwargs: calls.append(tuple(args)) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(updater, "_pterm", lambda action: calls.append(("pterm", action)))
    updater._start_mode("service")
    updater._start_mode("pinokio")
    assert calls == [("/bin/bash", "install_service.sh"), ("pterm", "start")]


def test_secrets_are_redacted():
    value = _redact({"hf_token": "hf_secret", "details": "Authorization: Bearer-abc"})
    assert value["hf_token"] == "[redacted]"
    assert "Bearer-abc" not in value["details"]


def test_next_daily_and_weekly_checks_are_future(updater: AutoUpdater):
    now = dt.datetime(2026, 7, 15, 10, tzinfo=dt.timezone.utc)
    updater.now = lambda: now
    daily = updater._next_regular({**updater.defaults, "frequency": "daily", "maintenance_hour": 2})
    weekly = updater._next_regular({**updater.defaults, "frequency": "weekly", "maintenance_hour": 2})
    assert daily > now
    assert weekly > daily


def test_build_suffix_version_matching(updater: AutoUpdater):
    updater.spec["allow_build_suffix"] = True
    assert updater._version_matches("1.22.0.abcdef0", "1.22.0")
    assert not updater._version_matches("1.21.9.abcdef0", "1.22.0")

