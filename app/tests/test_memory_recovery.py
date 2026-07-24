from datetime import datetime
import os
from pathlib import Path
import subprocess

import pytest

from backend import video
from backend.restart_health import restart_rate_snapshot


ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / "videostudio-watchdog.sh"


def _job(job_id="video-test"):
    return video.VideoJob(
        job_id,
        "txt2video",
        {"repo": "owner/model", "seed": None},
    )


def test_verified_memory_failure_retries_once_with_same_seed(tmp_path, monkeypatch):
    manager = video.VideoManager()
    job = _job()
    attempts = []
    releases = []

    monkeypatch.setattr(
        video,
        "_free_cached_pipeline",
        lambda: releases.append(True) or {"released": True},
    )
    monkeypatch.setattr(manager, "_service_installed", lambda: False)

    def generate(current, output):
        attempts.append(current.params.get("seed"))
        if len(attempts) == 1:
            current.resolved_seed = 20260724
            output.write_bytes(b"partial")
            raise RuntimeError("MPS backend out of memory")
        output.write_bytes(b"video")

    monkeypatch.setattr(manager, "_generate", generate)
    output = tmp_path / "video.mp4"
    manager._generate_with_memory_recovery(job, output)

    assert attempts == [None, 20260724]
    assert output.read_bytes() == b"video"
    assert releases == [True]
    assert manager.memory_status()["consecutive_failures"] == 0
    assert manager.memory_status()["last_event"]["error_type"] == "RuntimeError"
    assert "job_id" not in manager.memory_status()["last_event"]


def test_second_memory_failure_schedules_restart_only_after_job_persist(
        tmp_path, monkeypatch):
    manager = video.VideoManager()
    started = []

    class FakeTimer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.daemon = False

        def start(self):
            started.append(self.interval)

    monkeypatch.setattr(video, "_free_cached_pipeline", lambda: {})
    monkeypatch.setattr(manager, "_service_installed", lambda: True)
    monkeypatch.setattr(video.threading, "Timer", FakeTimer)
    monkeypatch.setattr(
        manager,
        "_generate",
        lambda *_args: (_ for _ in ()).throw(MemoryError()),
    )

    with pytest.raises(RuntimeError, match="restarting automatically"):
        manager._generate_with_memory_recovery(_job(), tmp_path / "video.mp4")

    assert manager.memory_status()["consecutive_failures"] == 2
    assert manager.memory_status()["restart_scheduled"] is True
    assert started == []
    manager._start_scheduled_restart()
    assert started == [0.75]


def test_normal_failures_do_not_retry_or_trigger_memory_restart(tmp_path, monkeypatch):
    manager = video.VideoManager()
    attempts = 0
    monkeypatch.setattr(
        manager,
        "_record_memory_failure",
        lambda *_args: pytest.fail("normal errors must not count as OOM"),
    )

    def fail(*_args):
        nonlocal attempts
        attempts += 1
        raise ValueError("width must be divisible by 16")

    monkeypatch.setattr(manager, "_generate", fail)
    with pytest.raises(ValueError, match="divisible"):
        manager._generate_with_memory_recovery(_job(), tmp_path / "video.mp4")

    assert attempts == 1
    assert video._is_memory_failure(MemoryError())
    assert video._is_memory_failure(RuntimeError("std::bad_alloc"))
    assert not video._is_memory_failure(RuntimeError("not enough free disk space"))
    assert not video._is_memory_failure(RuntimeError("provider connection timed out"))


def test_queued_local_jobs_resume_after_restart_but_running_job_does_not(
        tmp_path, monkeypatch):
    output = tmp_path / "output"
    history = output / ".history.json"
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", history)

    original = video.VideoManager()
    original._jobs["queued-a"] = _job("queued-a")
    original._jobs["queued-b"] = _job("queued-b")
    original._jobs["running"] = video.VideoJob(
        "running",
        "txt2video",
        {"repo": "owner/model"},
        state="running",
        stage="generating",
    )
    original.persist_state()

    resumed = []
    monkeypatch.setattr(
        video.VideoManager,
        "_run",
        lambda _self, job: resumed.append(job.job_id),
    )
    restored = video.VideoManager()
    for job_id in ("queued-a", "queued-b"):
        restored.get(job_id).thread.join(timeout=1)

    assert sorted(resumed) == ["queued-a", "queued-b"]
    assert restored._local_queue == ["queued-a", "queued-b"]
    assert restored.get("queued-a").state == "queued"
    assert restored.get("running").state == "error"
    assert restored.get("running").stage == "interrupted"


def test_restart_rate_snapshot_reports_warning(tmp_path):
    log = tmp_path / "watchdog.log"
    log.write_text(
        "[watchdog] 2026-07-24 08:00:00 health probe failed 3 consecutive times — restarting\n"
        "[watchdog] 2026-07-24 09:00:00 no /api/health — restarting\n",
        encoding="utf-8",
    )
    data = restart_rate_snapshot(log, now=datetime(2026, 7, 24, 10, 0, 0))
    assert data["status"] == "warning"
    assert data["restarts_24h"] == 2


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _watchdog_env(tmp_path, *, healthy: bool):
    curl = tmp_path / "curl"
    launchctl = tmp_path / "launchctl"
    state = tmp_path / "watchdog-state"
    launches = tmp_path / "launches.log"
    _write_executable(curl, f"#!/bin/sh\nexit {0 if healthy else 1}\n")
    _write_executable(
        launchctl,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$WATCHDOG_LAUNCH_LOG\"\n",
    )
    return {
        **os.environ,
        "VIDEOSTUDIO_WATCHDOG_CURL_BIN": str(curl),
        "VIDEOSTUDIO_WATCHDOG_LAUNCHCTL_BIN": str(launchctl),
        "VIDEOSTUDIO_WATCHDOG_STATE_FILE": str(state),
        "VIDEOSTUDIO_WATCHDOG_FAILURES_REQUIRED": "3",
        "WATCHDOG_LAUNCH_LOG": str(launches),
    }, state, launches


def _run_watchdog(env):
    return subprocess.run(
        ["/bin/bash", str(WATCHDOG)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_watchdog_requires_three_failures_and_success_resets(tmp_path):
    env, state, launches = _watchdog_env(tmp_path, healthy=False)
    assert "(1/3)" in _run_watchdog(env).stdout
    assert "(2/3)" in _run_watchdog(env).stdout
    assert not launches.exists()
    assert "failed 3 consecutive times" in _run_watchdog(env).stdout
    assert "kickstart -k" in launches.read_text(encoding="utf-8")

    healthy, _, _ = _watchdog_env(tmp_path, healthy=True)
    _run_watchdog(healthy)
    assert not state.exists()
