import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_direct_cloud_routes_and_modules_are_absent():
    from backend.main import app

    paths = {route.path for route in app.routes}
    assert not any(path.startswith("/api/providers") for path in paths)
    assert not any(path.startswith("/api/spend") for path in paths)
    assert not any(path.endswith("/repair") for path in paths)

    for relative in (
        "app/backend/catalog_sync.py",
        "app/backend/cloud_jobs.py",
        "app/backend/spend.py",
    ):
        assert not (ROOT / relative).exists()
    assert not list((ROOT / "app/backend/providers").glob("*.py"))
    assert not list((ROOT / "app/backend/providers").glob("*.json"))

    frontend = (ROOT / "app/frontend/app.js").read_text()
    assert "filter((model) => !model.is_cloud)" in frontend
    assert 'fetch("/api/providers")' not in frontend
    assert 'fetch("/api/spend")' not in frontend


def test_obsolete_provider_settings_are_not_loaded(monkeypatch, tmp_path):
    from backend import settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "hf_token": "hf_example_token",
        "providers": {"fal": {"api_key": "secret"}},
        "spend_caps": {"global": {"daily": 10}},
    }))
    monkeypatch.setattr(settings, "_PATH", path)
    monkeypatch.setattr(settings, "_cache", {})
    monkeypatch.setattr(settings, "_loaded", False)

    assert settings.get_hf_token() == "hf_example_token"
    assert set(settings._cache) == {"hf_token"}
    assert "secret" not in json.dumps(settings.serialize_public())


def test_disposable_legacy_gateway_jobs_are_ignored(monkeypatch, tmp_path):
    from backend import video

    output = tmp_path / "output"
    output.mkdir()
    history = output / ".history.json"
    history.write_text(json.dumps({"jobs": [{
        "job_id": "legacy-cloud-job",
        "mode": "txt2video",
        "state": "done",
        "params": {"cloud": True},
    }]}))
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", history)

    manager = video.VideoManager()

    assert manager.list_jobs() == []
