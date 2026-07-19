import json
import threading
from pathlib import Path

import pytest


def test_catalog_uses_current_loader_compatible_ltx_and_canonical_cog_ids():
    from backend import catalog

    repos = {model.repo for model in catalog.CATALOG}
    assert "Lightricks/LTX-Video" not in repos
    assert "Lightricks/LTX-Video-0.9.7-distilled" not in repos
    ltx = catalog.get_model("Lightricks/LTX-Video-0.9.8-13B-distilled")
    assert ltx is not None
    assert ltx.pipeline_classes == {
        "txt2video": "LTXConditionPipeline", "img2video": "LTXConditionPipeline"
    }
    assert catalog.get_model("THUDM/CogVideoX-2b").repo == "zai-org/CogVideoX-2b"


def test_catalog_has_a_native_mlx_model_for_16_and_24_gb_macs():
    from backend import catalog

    lance = catalog.get_model("mlx-community/Lance-3B-Video-bf16")
    assert lance is not None
    assert lance.family == "lance-mlx"
    assert lance.engine == "mlx-lance"
    assert lance.is_apple_optimized is True
    assert lance.min_unified_memory_gb == 16
    assert lance.recommended_unified_memory_gb == 24
    assert lance.capabilities == ("txt2video",)
    assert lance.max_frames == 25
    assert lance.video_defaults["width"] == 512
    assert lance.video_defaults["height"] == 512


def test_lance_pipeline_readiness_is_scoped_to_text_to_video(monkeypatch):
    from backend import catalog, video

    lance = catalog.get_model("mlx-community/Lance-3B-Video-bf16")
    monkeypatch.setattr(video, "LANCE_AVAILABLE", True)
    assert video.pipeline_available("lance-mlx", "txt2video") is True
    assert video.pipeline_available("lance-mlx", "img2video") is False
    assert video.model_pipeline_available(lance, "txt2video") is True
    assert video.model_pipeline_available(lance, "img2video") is False


def test_generate_dispatches_lance_without_importing_diffusers(monkeypatch, tmp_path):
    from backend import video

    manager = video.VideoManager()
    called = {}

    def fake_lance(job, model, output_path, **kwargs):
        called.update({"repo": model.repo, "output": output_path, **kwargs})

    monkeypatch.setattr(manager, "_generate_lance", fake_lance)
    job = video.VideoJob("mlx", "txt2video", {
        "repo": "mlx-community/Lance-3B-Video-bf16",
        "prompt": "A paper kite over the sea",
        "frames": 17,
        "fps": 12,
        "steps": 30,
        "guidance": 4.0,
        "width": 512,
        "height": 512,
        "seed": 7,
    })
    target = tmp_path / "lance.mp4"
    manager._generate(job, target)

    assert called["repo"] == "mlx-community/Lance-3B-Video-bf16"
    assert called["output"] == target
    assert called["frames"] == 17
    assert job.resolved_seed == 7


def test_lance_rejects_oversized_render_before_loading_on_16_gb(monkeypatch, tmp_path):
    from backend import catalog, system_info, video

    manager = video.VideoManager()
    lance = catalog.get_model("mlx-community/Lance-3B-Video-bf16")
    job = video.VideoJob("mlx-limit", "txt2video", {"prompt": "x"})
    monkeypatch.setattr(system_info, "detect_memory_gb", lambda: 16)
    monkeypatch.setattr(video, "_load_lance_pipeline", lambda _model: pytest.fail("must not load"))

    with pytest.raises(ValueError, match="limited to 512×512"):
        manager._generate_lance(
            job, lance, tmp_path / "oversized.mp4", frames=17, fps=12,
            steps=30, guidance=4.0, width=768, height=768, seed=7,
        )


def test_renamed_repo_can_reuse_legacy_cache(monkeypatch, tmp_path):
    from backend import cache

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    snapshot = (tmp_path / "hub" / "models--THUDM--CogVideoX-2b" /
                "snapshots" / "legacy")
    snapshot.mkdir(parents=True)
    (snapshot / "transformer.safetensors").write_bytes(b"weights")

    status = cache.status_snapshot("zai-org/CogVideoX-2b", ("THUDM/CogVideoX-2b",))
    assert status["state"] == "cached"
    assert status["cached_repo"] == "THUDM/CogVideoX-2b"


def test_public_job_payload_never_exposes_inline_media_or_provider_params():
    from backend.video import VideoJob

    job = VideoJob("safe", "img2video", {
        "prompt": "x", "image_data_uri": "data:image/png;base64,SECRET",
        "provider_params": {"secret": "hidden"},
    })
    payload = json.dumps(job.serialize())
    assert "SECRET" not in payload
    assert "provider_params" not in payload


def test_history_delete_refuses_unmanaged_output(monkeypatch, tmp_path):
    from backend import video

    output = tmp_path / "output"
    outside = tmp_path / "do-not-delete.mp4"
    outside.write_bytes(b"user file")
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", output / ".history.json")
    manager = video.VideoManager()
    manager._jobs["unsafe"] = video.VideoJob(
        "unsafe", "txt2video", {}, state="done", output_path=str(outside)
    )

    assert manager.delete_job("unsafe") is True
    assert outside.read_bytes() == b"user file"


def test_stale_local_job_becomes_recoverable_history_error(monkeypatch, tmp_path):
    from backend import video

    output = tmp_path / "output"
    history = output / ".history.json"
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", history)
    manager = video.VideoManager()
    manager._jobs["stale"] = video.VideoJob(
        "stale", "txt2video", {"repo": "zai-org/CogVideoX-2b"},
        state="running", stage="generating",
    )
    manager.persist_state()

    restored = video.VideoManager().get("stale")
    assert restored.state == "error"
    assert restored.stage == "interrupted"
    assert "app restart" in restored.error


def test_local_queue_runs_strictly_oldest_first(monkeypatch, tmp_path):
    from backend import video

    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(video, "OUTPUT_DIR", output)
    monkeypatch.setattr(video, "HISTORY_FILE", output / ".history.json")
    monkeypatch.setattr(video, "ENGINE_AVAILABLE", True)
    manager = video.VideoManager()
    order = []

    def fake_generate(job, path):
        order.append(job.job_id)
        path.write_bytes(b"video")

    monkeypatch.setattr(manager, "_generate", fake_generate)
    first = video.VideoJob("first", "txt2video", {})
    second = video.VideoJob("second", "txt2video", {})
    manager._jobs.update({"first": first, "second": second})
    manager._local_queue.extend(["first", "second"])
    threads = [threading.Thread(target=manager._run, args=(second,)),
               threading.Thread(target=manager._run, args=(first,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert order == ["first", "second"]


def test_cloud_result_download_rejects_private_network(monkeypatch, tmp_path):
    from backend import cloud_jobs

    monkeypatch.setattr(cloud_jobs.socket, "getaddrinfo", lambda *_a, **_k: [
        (2, 1, 6, "", ("127.0.0.1", 443))
    ])
    with pytest.raises(ValueError, match="private or local"):
        cloud_jobs._download("https://example.test/result.mp4", tmp_path / "out.mp4")


def test_invalid_image_is_rejected_after_extension_check(tmp_path):
    from fastapi import HTTPException
    from backend.main import _validate_uploaded_media

    fake = tmp_path / "fake.png"
    fake.write_bytes(b"not an image")
    with pytest.raises(HTTPException, match="decodable image"):
        _validate_uploaded_media(fake, "img2video")
