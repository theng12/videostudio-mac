from types import SimpleNamespace

from backend import main


def test_video_generation_blocks_automatic_update(monkeypatch):
    monkeypatch.setattr(main.gen_manager, "list_jobs", lambda: [SimpleNamespace(state="running")])
    monkeypatch.setattr(main.manager, "list_jobs", lambda: [])
    assert main._automatic_update_blockers() == [
        "a local or cloud video generation is queued or running"
    ]


def test_model_download_blocks_automatic_update(monkeypatch):
    monkeypatch.setattr(main.gen_manager, "list_jobs", lambda: [])
    monkeypatch.setattr(main.manager, "list_jobs", lambda: [SimpleNamespace(state="paused")])
    assert main._automatic_update_blockers() == ["a model download is active"]
