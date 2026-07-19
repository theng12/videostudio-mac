import os
import time
from fastapi.testclient import TestClient
from backend import storage_policy
from backend.main import FLEET_TOKEN, app


class Job:
    def __init__(self, state): self.state = state


class Manager:
    def __init__(self, output_dir, jobs=None): self.output_dir, self.jobs = output_dir, jobs or {}
    def get(self, job_id): return self.jobs.get(job_id)
    def delete_job(self, job_id):
        if self.jobs.pop(job_id, None) is None: return False
        (self.output_dir / f"{job_id}.mp4").unlink(missing_ok=True); return True


def _file(root, name, size, age_days=0):
    path = root / name; path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400; os.utime(path, (stamp, stamp)); return path


def test_age_and_cap_only_remove_mp4_outputs(tmp_path, monkeypatch):
    output = tmp_path / "output"; output.mkdir(); uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(storage_policy, "SETTINGS_FILE", tmp_path / "config" / "policy.json")
    manager = Manager(output, {"old": Job("done"), "new": Job("done")})
    _file(output, "old.mp4", 4, 4); _file(output, "new.mp4", 5)
    history = _file(output, ".history.json", 100, 20); upload = _file(uploads, "source.mp4", 100, 20)
    storage_policy.save(True, 3, 80)
    result = storage_policy.enforce(manager, output, target_bytes=4)
    assert result["deleted"] == 2 and result["used_bytes"] == 0
    assert history.exists() and upload.exists()


def test_active_output_is_never_deleted(tmp_path, monkeypatch):
    output = tmp_path / "output"; output.mkdir()
    monkeypatch.setattr(storage_policy, "SETTINGS_FILE", tmp_path / "config" / "policy.json")
    manager = Manager(output, {"running": Job("running")}); active = _file(output, "running.mp4", 10, 10)
    storage_policy.save(True, 1, 1)
    assert storage_policy.enforce(manager, output, target_bytes=0)["deleted"] == 0 and active.exists()


def test_policy_api_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_policy, "SETTINGS_FILE", tmp_path / "config" / "policy.json")
    client = TestClient(app, headers={"X-Studio-Token": FLEET_TOKEN})
    response = client.put("/api/storage-policy", json={"enabled": True, "retention_days": 3, "max_gb": 80})
    assert response.status_code == 200 and response.json()["retention_days"] == 3
