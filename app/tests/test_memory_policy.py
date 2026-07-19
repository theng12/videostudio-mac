from pathlib import Path

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


def test_performance_default_keeps_pipeline_loaded(tmp_path, monkeypatch):
    manager = Manager(idle=99_999)
    _reset(monkeypatch, tmp_path, manager)
    assert memory_policy.status()["mode"] == "performance"
    assert memory_policy.run_due_release(now=100_000) is None
    assert manager.releases == 0


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
    assert "Performance · default" in html
    assert 'fetch("/api/memory-policy"' in script
    assert 'fetch("/api/memory/release"' in script
    assert PROCESS_TITLE == "Video Studio Mac"
