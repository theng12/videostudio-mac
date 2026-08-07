import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend import memory_policy, video
from backend.main import FLEET_TOKEN, app
from backend.process_title import PROCESS_TITLE


class Manager:
    def __init__(self, loaded=True, idle=0.0, active=False):
        self.loaded = loaded
        self.idle = idle
        self.active = active
        self.releases = 0
        self.activity = 100.0

    def has_active_local_jobs(self):
        return self.active

    def has_loaded_pipeline(self):
        return self.loaded

    def loaded_pipeline_key(self):
        return ("local/test-video", "txt2video") if self.loaded else None

    def idle_seconds(self, now=None):
        return self.idle if self.loaded else None

    def last_activity_at(self):
        return self.activity

    def release_memory(self, reason="manual"):
        self.releases += 1
        was_loaded, self.loaded = self.loaded, False
        return {"released": was_loaded, "pipeline": ["local/test-video", "txt2video"], "actions": ["test cache cleared"]}


def _reset(monkeypatch, tmp_path, manager=None):
    monkeypatch.setattr(memory_policy, "SETTINGS_FILE", tmp_path / "memory_policy.json")
    monkeypatch.setattr(memory_policy, "_MANAGER", manager or Manager())
    monkeypatch.setattr(memory_policy, "_LAST_RELEASE_AT", None)
    monkeypatch.setattr(memory_policy, "_LAST_RELEASE_REASON", None)
    monkeypatch.setattr(memory_policy, "_LAST_RELEASE_DETAILS", None)
    monkeypatch.setattr(memory_policy, "_LAST_ERROR", None)
    monkeypatch.setattr(memory_policy, "_RELEASE_COUNT", 0)
    monkeypatch.setattr(memory_policy, "_RELEASING", False)


def test_explicit_performance_mode_keeps_pipeline_loaded(tmp_path, monkeypatch):
    """`performance` must still pin the pipeline when an operator asks for it.
    What changed in this fix is that it is no longer the *default* — see
    test_default_mode_is_no_longer_the_one_that_never_releases below."""
    manager = Manager(idle=99_999)
    _reset(monkeypatch, tmp_path, manager)
    memory_policy.save("performance")
    assert memory_policy.status()["mode"] == "performance"
    assert memory_policy.run_due_release(now=100_000) is None
    assert manager.releases == 0


def test_default_mode_is_no_longer_the_one_that_never_releases(tmp_path, monkeypatch):
    """With no operator choice on disk, an idle pipeline must eventually be
    freed. Previously the default was `performance` (idle_seconds=None), so
    the release thread ran forever and did nothing."""
    manager = Manager(idle=99_999)
    _reset(monkeypatch, tmp_path, manager)
    assert memory_policy.status()["mode"] != "performance"
    assert memory_policy.run_due_release(now=100_000) is not None
    assert manager.releases == 1


def test_balanced_releases_at_ten_minutes(tmp_path, monkeypatch):
    manager = Manager(idle=599)
    _reset(monkeypatch, tmp_path, manager)
    memory_policy.save("balanced")
    assert memory_policy.run_due_release(now=699) is None
    manager.idle = 600
    released = memory_policy.run_due_release(now=700)
    assert released["last_release_reason"] == "automatic:balanced"
    assert released["busy"] is False
    assert manager.releases == 1


def test_active_local_render_blocks_manual_release(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path, Manager(active=True))
    client = TestClient(app, headers={"X-Studio-Token": FLEET_TOKEN})
    response = client.post("/api/memory/release")
    assert response.status_code == 409


def test_successful_render_retains_pipeline_but_failure_releases(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", output / ".history.json")
    monkeypatch.setattr(video, "ENGINE_AVAILABLE", True)
    releases = []
    monkeypatch.setattr(video, "_free_cached_pipeline", lambda: releases.append(True) or {"released": True})

    manager = video.VideoManager()
    good = video.VideoJob("good", "txt2video", {})
    manager._jobs[good.job_id] = good
    manager._local_queue.append(good.job_id)
    monkeypatch.setattr(manager, "_generate", lambda _job, path: path.write_bytes(b"video"))
    manager._run(good)
    assert good.state == "done"
    assert releases == []

    bad = video.VideoJob("bad", "txt2video", {})
    manager._jobs[bad.job_id] = bad
    manager._local_queue.append(bad.job_id)
    monkeypatch.setattr(manager, "_generate", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    manager._run(bad)
    assert bad.state == "error"
    assert releases == [True]


def test_memory_api_frontend_and_process_title(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    client = TestClient(app, headers={"X-Studio-Token": FLEET_TOKEN})
    saved = client.put("/api/memory-policy", json={"mode": "memory_saver"})
    assert saved.status_code == 200
    assert saved.json()["idle_seconds"] == 120
    released = client.post("/api/memory/release")
    assert released.status_code == 200
    assert released.json()["last_release_details"]["released"] is True

    root = Path(__file__).parents[1]
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (root / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "Release Memory / Unload Model" in html
    # The "· default" badge is no longer hardcoded onto Performance: since
    # v0.10.6 the default follows the host's memory, so the label is bound
    # to whatever the backend reports.
    assert "Performance" in html
    assert "memoryPolicy.default_mode==='performance'" in html
    assert 'fetch("/api/memory-policy"' in script
    assert 'fetch("/api/memory/release"' in script
    assert PROCESS_TITLE == "Video Studio Mac"


def test_shipped_default_actually_releases_on_idle(monkeypatch) -> None:
    """The idle-release thread ran on every fleet machine and did nothing,
    because the shipped default was "performance" (idle_seconds=None). Each
    Studio ships this same skeleton, so on a shared 8 GB Mac 3-5 of them each
    independently pinned a pipeline forever: 16 of 19 machines could not start
    a job. A default that never releases is not a default."""
    assert memory_policy.MODES[memory_policy.DEFAULT_MODE]["idle_seconds"] is not None
    assert (
        memory_policy.MODES[memory_policy.SMALL_MACHINE_DEFAULT_MODE]["idle_seconds"]
        is not None
    )

    # Small machines get the tighter budget; roomy ones keep a pipeline warm longer.
    monkeypatch.setattr(
        memory_policy, "_SMALL_MACHINE_GB", 12, raising=False
    )
    import psutil

    monkeypatch.setattr(
        psutil, "virtual_memory",
        lambda: SimpleNamespace(total=int(8.6e9), available=int(4e9),
                                used=int(4.6e9), percent=53.0),
    )
    assert memory_policy.default_mode() == "memory_saver"

    monkeypatch.setattr(
        psutil, "virtual_memory",
        lambda: SimpleNamespace(total=int(25.8e9), available=int(18e9),
                                used=int(7.8e9), percent=30.0),
    )
    assert memory_policy.default_mode() == "balanced"


def test_operator_choice_still_wins_over_the_machine_default(monkeypatch, tmp_path) -> None:
    """An explicit mode is persisted and must survive; the memory-aware default
    only applies when nobody has chosen."""
    settings = tmp_path / "memory_policy.json"
    settings.write_text('{"mode": "performance"}\n', encoding="utf-8")
    monkeypatch.setattr(memory_policy, "SETTINGS_FILE", settings)
    assert memory_policy._read()["mode"] == "performance"


def test_ui_does_not_hardcode_performance_as_the_default() -> None:
    """The mode picker said "Performance · default". Since v0.10.6 the default is
    chosen from the host's memory, so a hardcoded label is simply wrong on every
    8 GB machine. The badge must follow whatever the backend reports."""
    markup = (Path(__file__).resolve().parents[1]
              / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "Performance · default" not in markup
    for mode in ("performance", "balanced", "memory_saver", "immediate"):
        assert f"memoryPolicy.default_mode==='{mode}'" in markup


def test_psutil_is_declared_in_the_base_requirements() -> None:
    """memory_policy.default_mode() imports psutil unconditionally, on every
    install — not just installs that also pull in the optional generation
    stack. psutil was once listed only in requirements-generation.lock.txt;
    a base-only install would then hit the caught ImportError inside
    default_mode() and silently fall back to DEFAULT_MODE regardless of host
    RAM, defeating the machine-aware default on exactly the small machines it
    targets. Pin it here so that gap can't regress silently."""
    app_root = Path(__file__).resolve().parents[1]
    requirements = (app_root / "requirements.txt").read_text(encoding="utf-8")
    lock = (app_root / "requirements.lock.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^psutil(==|>=)", requirements), (
        "psutil must be declared in requirements.txt (the base install), "
        "not only in requirements-generation.lock.txt"
    )
    assert re.search(r"(?m)^psutil==", lock), (
        "psutil must be pinned in requirements.lock.txt (the base install), "
        "not only in requirements-generation.lock.txt"
    )
