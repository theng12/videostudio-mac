"""Phase 2 cloud adapters, catalog freshness, safety, and restart recovery."""
from __future__ import annotations

import json
import time

import pytest


def test_mp4_duration_reader(tmp_path):
    from backend.cloud_jobs import _mp4_duration_s
    # Version-0 mvhd: marker + flags/creation/modification + timescale/duration.
    payload = b"\x00" * 12 + b"mvhd" + b"\x00\x00\x00\x00" + b"\x00" * 8
    payload += (1000).to_bytes(4, "big") + (5042).to_bytes(4, "big") + b"\x00" * 20
    path = tmp_path / "clip.mp4"
    path.write_bytes(payload)
    assert _mp4_duration_s(path) == 5.042


def test_per_second_spend_reconciles_downloaded_duration(isolated, monkeypatch, tmp_path):
    from backend import cloud_jobs, video
    from backend.providers.base import CloudVideoModel, JobStatus, SubmitResult

    class Provider:
        key = "fake"

        def submit(self, *_args):
            return SubmitResult("task-1", {"id": "task-1"})

        def poll(self, *_args):
            return JobStatus(state="done", result_url="https://cdn.test/video.mp4")

    def download(_url, dest):
        payload = b"\x00" * 12 + b"mvhd" + b"\x00\x00\x00\x00" + b"\x00" * 8
        payload += (1000).to_bytes(4, "big") + (5042).to_bytes(4, "big") + b"\x00" * 20
        dest.write_bytes(payload)

    finishes = []
    model = CloudVideoModel(
        id="fake:model", provider="fake", provider_model="model", label="Model",
        price_unit="per_second", price_usd=0.01)
    job = video.VideoJob("duration-cost", "txt2video", {"cloud": True})
    monkeypatch.setattr(cloud_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(cloud_jobs, "_download", download)
    monkeypatch.setattr(cloud_jobs, "_probe_output", lambda _path: {
        "codec": "h264", "pixel_format": "yuv420p", "width": 640, "height": 360,
        "fps": 24, "frames": 121, "duration_s": 5.042, "size_bytes": 64,
    })
    monkeypatch.setattr(cloud_jobs.gen_manager, "persist_state", lambda: None)
    monkeypatch.setattr(cloud_jobs.spend, "record_finish",
                        lambda *args, **kwargs: finishes.append((args, kwargs)))

    cloud_jobs._drive(job, Provider(), model, "txt2video", job.params, "spend-1", 0.05)
    assert finishes[-1][1] == {"actual_usd": 0.0504, "duration_s": 5.042, "state": "done"}


def test_registry_exposes_all_phase2_providers(isolated, monkeypatch, tmp_path):
    from backend import catalog_sync
    from backend.providers import registry

    monkeypatch.setattr(catalog_sync, "CACHE_FILE", tmp_path / "catalog.json")
    assert set(registry.PROVIDERS) == {"fal", "kie", "replicate"}
    assert {p["key"] for p in registry.providers_status()} == {"fal", "kie", "replicate"}
    assert registry.provider_for_id("replicate:lightricks/ltx-2-fast") is not None
    assert registry.provider_for_id("kie:kling-3.0/video") is not None


def test_replicate_submit_poll_and_result_mapping(isolated, monkeypatch):
    from backend.providers import replicate

    calls = []

    def fake_request(method, url, body=None):
        calls.append((method, url, body))
        if method == "POST":
            return {"id": "pred-1", "urls": {"get": "https://status/pred-1"}}
        return {"id": "pred-1", "status": "succeeded",
                "output": {"video": "https://cdn.test/result.mp4"}}

    monkeypatch.setattr(replicate, "_request", fake_request)
    provider = replicate.ReplicateProvider()
    model = provider.list_models()[0]
    submitted = provider.submit(model, "txt2video", {
        "prompt": "a lighthouse", "duration": 5, "resolution": "1080p",
    })
    status = provider.poll(model, submitted.raw)
    assert calls[0][1].endswith("/models/lightricks/ltx-2-fast/predictions")
    assert calls[0][2]["input"]["duration"] == 5
    assert status.state == "done"
    assert status.result_url == "https://cdn.test/result.mp4"


def test_kie_submit_poll_and_result_mapping(isolated, monkeypatch):
    from backend.providers import kie

    responses = iter([
        {"code": 200, "data": {"taskId": "task-1"}},
        {"code": 200, "data": {"state": "success", "resultJson": json.dumps({
            "resultUrls": ["https://cdn.test/kie.mp4"]
        })}},
    ])
    monkeypatch.setattr(kie, "_request", lambda *args, **kwargs: next(responses))
    provider = kie.KieProvider()
    model = provider.list_models()[0]
    submitted = provider.submit(model, "txt2video", {"prompt": "a calm lake", "duration": 5})
    status = provider.poll(model, submitted.raw)
    assert submitted.provider_job_id == "task-1"
    assert status.state == "done"
    assert status.result_url == "https://cdn.test/kie.mp4"


def test_catalog_diff_marks_new_then_deprecated_and_hides_after_grace(
        isolated, monkeypatch, tmp_path):
    from backend import catalog_sync
    from backend.providers.base import CloudVideoModel, VideoProvider

    class FakeProvider(VideoProvider):
        key = "fake"

        def __init__(self):
            self.models = [CloudVideoModel(
                id="fake:a", provider="fake", provider_model="a", label="A")]

        def list_models(self):
            return self.models

    cache_file = tmp_path / "catalog.json"
    monkeypatch.setattr(catalog_sync, "CACHE_FILE", cache_file)
    provider = FakeProvider()
    first = catalog_sync.models_for(provider)
    assert first[0].status == "new"

    provider.models = []
    deprecated = catalog_sync.models_for(provider, force=True)
    assert deprecated[0].status == "deprecated"
    state = json.loads(cache_file.read_text())
    state["fake"]["models"][0]["deprecated_at"] = time.time() - catalog_sync.DEPRECATED_FOR_S - 1
    cache_file.write_text(json.dumps(state))
    assert catalog_sync.models_for(provider) == []


def test_catalog_refresh_failure_keeps_last_good_snapshot(isolated, monkeypatch, tmp_path):
    from backend import catalog_sync
    from backend.providers.base import CloudVideoModel, VideoProvider

    class FakeProvider(VideoProvider):
        key = "fake"
        broken = False

        def list_models(self):
            if self.broken:
                raise RuntimeError("network down")
            return [CloudVideoModel(id="fake:a", provider="fake", provider_model="a", label="A")]

    monkeypatch.setattr(catalog_sync, "CACHE_FILE", tmp_path / "catalog.json")
    provider = FakeProvider()
    catalog_sync.models_for(provider)
    provider.broken = True
    assert [m.id for m in catalog_sync.models_for(provider, force=True)] == ["fake:a"]


def test_cloud_billing_requires_paid_toggle_and_verified_price(isolated, monkeypatch):
    from backend import cloud_jobs
    from backend.providers.base import CloudVideoModel, VideoProvider
    from backend.providers import registry

    class FakeProvider(VideoProvider):
        key = "fake"
        name = "Fake"

        def has_key(self):
            return True

    provider = FakeProvider()
    model = CloudVideoModel(id="fake:x", provider="fake", provider_model="x", label="X")
    monkeypatch.setattr(registry, "provider_for_id", lambda _repo: (provider, model))
    isolated["providers"]["fake"] = {"key": "secret", "paid": False}
    with pytest.raises(cloud_jobs.PaidUseDisabled):
        cloud_jobs.start_cloud_generation("txt2video", {"repo": "fake:x", "prompt": "x"})
    isolated["providers"]["fake"]["paid"] = True
    with pytest.raises(ValueError, match="no verified price"):
        cloud_jobs.start_cloud_generation("txt2video", {"repo": "fake:x", "prompt": "x"})


def test_active_cloud_job_round_trips_through_history(isolated, monkeypatch, tmp_path):
    from backend import video

    output = tmp_path / "output"
    history = output / ".history.json"
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", history)
    manager = video.VideoManager()
    job = video.VideoJob(
        job_id="resume-me", mode="txt2video", params={"repo": "fal:x", "cloud": True},
        state="running", started_at=time.time(), cloud_state={
            "provider": "fal", "model_id": "fal:x", "submit_raw": {"request_id": "abc"},
            "spend_id": "spend-1", "estimate_usd": 0.04,
        },
    )
    manager._jobs[job.job_id] = job
    manager.persist_state()
    restored = video.VideoManager().get("resume-me")
    assert restored is not None
    assert restored.state == "running"
    assert restored.cloud_state["submit_raw"]["request_id"] == "abc"


def test_transient_timeout_keeps_polling_same_paid_task(isolated, monkeypatch):
    from backend import cloud_jobs, video
    from backend.providers.base import CloudVideoModel, JobStatus, SubmitResult

    class Provider:
        key = "fake"
        submit_calls = 0
        poll_calls = 0

        def submit(self, model, mode, params):
            self.submit_calls += 1
            return SubmitResult("provider-task-1", {"request_id": "provider-task-1"})

        def poll(self, model, raw):
            self.poll_calls += 1
            assert raw["request_id"] == "provider-task-1"
            if self.poll_calls == 1:
                raise TimeoutError("temporary gateway timeout")
            if self.poll_calls < 5:
                return JobStatus(state="running")
            return JobStatus(state="done", result_url="https://cdn.test/video.mp4")

    provider = Provider()
    model = CloudVideoModel(
        id="fake:model", provider="fake", provider_model="model", label="Model")
    job = video.VideoJob("safe-job", "txt2video", {"repo": "fake:model", "cloud": True})
    finishes = []
    monkeypatch.setattr(cloud_jobs.gen_manager, "persist_state", lambda: None)
    monkeypatch.setattr(cloud_jobs, "_wait", lambda *_args: None)
    monkeypatch.setattr(cloud_jobs, "_download", lambda *_args: None)
    monkeypatch.setattr(cloud_jobs.spend, "record_finish",
                        lambda *args, **kwargs: finishes.append((args, kwargs)))

    cloud_jobs._drive(job, provider, model, "txt2video", job.params, "spend-1", 0.04)
    assert provider.submit_calls == 1
    assert provider.poll_calls == 5
    assert job.cloud_state["provider_job_id"] == "provider-task-1"
    assert job.cloud_state["provider_terminal"] is True
    assert job.cloud_state["provider_state"] == "done"
    assert len(finishes) == 1
    assert finishes[-1][1]["state"] == "done"


def test_repair_reuses_saved_submit_response_without_resubmitting(isolated, monkeypatch):
    from backend import cloud_jobs, video
    from backend.providers import registry
    from backend.providers.base import CloudVideoModel, JobStatus

    class Provider:
        key = "fake"

        def submit(self, *_args):
            raise AssertionError("repair must never submit a second task")

        def poll(self, model, raw):
            assert raw == {"request_id": "saved-task"}
            return JobStatus(state="done", result_url="https://cdn.test/recovered.mp4")

    provider = Provider()
    model = CloudVideoModel(
        id="fake:model", provider="fake", provider_model="model", label="Model")
    job = video.VideoJob(
        "repair-me", "txt2video", {"repo": "fake:model", "cloud": True}, state="error",
        cloud_state={
            "provider": "fake", "provider_job_id": "saved-task",
            "model_id": "fake:model", "provider_model": "model",
            "submit_raw": {"request_id": "saved-task"}, "spend_id": "spend-1",
            "estimate_usd": 0.04, "provider_terminal": False,
        },
    )

    class Manager:
        def list_jobs(self):
            return [job]

        def get(self, job_id):
            return job if job_id == job.job_id else None

        def persist_state(self):
            pass

        def resume_cloud(self, active_job, runner):
            runner(active_job)
            return True

    monkeypatch.setattr(cloud_jobs, "gen_manager", Manager())
    monkeypatch.setattr(registry, "provider_for_id", lambda _model_id: (provider, model))
    monkeypatch.setattr(cloud_jobs, "_download", lambda *_args: None)
    monkeypatch.setattr(cloud_jobs.spend, "record_finish", lambda *_args, **_kwargs: None)

    repaired, attached = cloud_jobs.repair_job("repair-me")
    assert attached is True
    assert repaired.output_path.endswith("repair-me.mp4")
    assert repaired.cloud_state["provider_state"] == "done"


def test_completed_provider_task_retries_download_without_rebilling(isolated, monkeypatch):
    from backend import cloud_jobs, video
    from backend.providers.base import CloudVideoModel, JobStatus, SubmitResult

    class Provider:
        key = "fake"
        submit_calls = 0

        def submit(self, *_args):
            self.submit_calls += 1
            return SubmitResult("paid-task", {"id": "paid-task"})

        def poll(self, *_args):
            return JobStatus(state="done", result_url="https://cdn.test/paid.mp4")

    downloads = []
    finishes = []

    def flaky_download(*_args):
        downloads.append(True)
        if len(downloads) == 1:
            raise TimeoutError("signed URL temporarily unavailable")

    provider = Provider()
    model = CloudVideoModel(
        id="fake:model", provider="fake", provider_model="model", label="Model")
    job = video.VideoJob("download-repair", "txt2video", {"cloud": True})
    monkeypatch.setattr(cloud_jobs.gen_manager, "persist_state", lambda: None)
    monkeypatch.setattr(cloud_jobs, "_wait", lambda *_args: None)
    monkeypatch.setattr(cloud_jobs, "_download", flaky_download)
    monkeypatch.setattr(cloud_jobs.spend, "record_finish",
                        lambda *args, **kwargs: finishes.append((args, kwargs)))

    cloud_jobs._drive(job, provider, model, "txt2video", job.params, "spend-1", 0.04)
    assert provider.submit_calls == 1
    assert len(downloads) == 2
    assert len(finishes) == 1
    assert finishes[0][1] == {"actual_usd": 0.04, "duration_s": None, "state": "done"}


def test_submit_timeout_is_persisted_and_blocks_duplicate_paid_submit(isolated, monkeypatch):
    from backend import cloud_jobs, video
    from backend.providers import registry
    from backend.providers.base import CloudVideoModel

    class Provider:
        key = "fake"
        name = "Fake"

        def has_key(self):
            return True

        def submit(self, *_args):
            raise TimeoutError("response lost after provider may have accepted task")

    provider = Provider()
    model = CloudVideoModel(
        id="fake:model", provider="fake", provider_model="model", label="Model",
        price_unit="per_video", price_usd=0.04)
    job = video.VideoJob("ambiguous", "txt2video", {"repo": "fake:model", "cloud": True})
    persists = []
    finishes = []
    monkeypatch.setattr(cloud_jobs.gen_manager, "persist_state", lambda: persists.append(True))
    monkeypatch.setattr(cloud_jobs.spend, "record_finish",
                        lambda *args, **kwargs: finishes.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="will not resubmit"):
        cloud_jobs._drive(job, provider, model, "txt2video", job.params, "spend-1", 0.04)
    assert len(persists) == 2  # before paid boundary + ambiguous outcome
    assert finishes == []     # reservation remains until the provider outcome is known
    assert job.cloud_state["provider_state"] == "submit-unknown"
    assert job.serialize()["cloud_recovery"]["submission_unknown"] is True
    assert job.serialize()["cloud_recovery"]["recoverable"] is False

    isolated["providers"]["fake"] = {"key": "secret", "paid": True}
    monkeypatch.setattr(registry, "provider_for_id", lambda _repo: (provider, model))
    monkeypatch.setattr(cloud_jobs.gen_manager, "list_jobs", lambda: [job])
    with pytest.raises(ValueError, match="will not risk another paid submission"):
        cloud_jobs.start_cloud_generation("txt2video", {"repo": "fake:model", "prompt": "x"})


def test_recovery_metadata_is_safe_and_visible_to_clients(isolated):
    from backend.video import VideoJob

    job = VideoJob(
        "visible", "txt2video", {"cloud": True}, state="error",
        cloud_state={
            "provider": "fal", "provider_job_id": "task-public-id",
            "provider_state": "retrying", "provider_terminal": False,
            "submit_raw": {"signed_secret_url": "must-not-leak"},
            "last_poll_error": "temporary timeout", "poll_attempts": 4,
        },
    )
    payload = job.serialize()
    assert payload["cloud_recovery"]["task_id"] == "task-public-id"
    assert payload["cloud_recovery"]["recoverable"] is True
    assert "submit_raw" not in payload
    assert "must-not-leak" not in json.dumps(payload)


def test_repair_watchdog_runs_recovery_scan(isolated, monkeypatch):
    from backend import cloud_jobs

    class StopWatchdog(Exception):
        pass

    scans = []
    monkeypatch.setattr(cloud_jobs, "_watchdog_started", False)
    monkeypatch.setattr(cloud_jobs, "resume_inflight", lambda: scans.append(True) or 0)
    monkeypatch.setattr(cloud_jobs.time, "sleep", lambda _seconds: (_ for _ in ()).throw(StopWatchdog()))

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            try:
                self.target()
            except StopWatchdog:
                pass

    monkeypatch.setattr(cloud_jobs.threading, "Thread", ImmediateThread)
    cloud_jobs.start_repair_watchdog()
    assert scans == [True]
