"""
Video generation manager.

Wraps 🤗 Diffusers video pipelines (LTX-Video, Wan 2.2, HunyuanVideo,
CogVideoX) in a thread-per-job pattern that mirrors Image Studio's generation
manager and the download manager. The torch/diffusers imports are wrapped in
try/except so the server still runs (catalog / download browsing) when the
heavy engine isn't installed yet — the generation endpoints just return 503.

Runs on Apple's MPS backend when available (falls back to CPU). Output clips
land in `app/output/<job_id>.mp4`.

Modes:
  - "txt2video"   : text → clip
  - "img2video"   : still image → clip (first-frame / image-to-video)
  - "video2video" : input clip + prompt → restyled clip (CogVideoX)

NOTE: this engine is authored against documented Diffusers pipeline APIs. Heavy
generation is intended for the big-memory target Mac; on smaller machines the
server, catalog, downloads, and diagnostics all work, but a generation job may
be very slow or run out of memory. Per-pipeline call kwargs are filtered by
introspecting each pipeline's __call__ signature, so models with slightly
different parameters (CogVideoX has no width/height knob, etc.) still work.
"""
from __future__ import annotations

import gc
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import catalog


# ───────────── module-level locks / paths ─────────────
# Diffusers pipelines load multi-GB weights into a process-wide torch/MPS state
# that isn't safe to run concurrently — parallel generations exhaust unified
# memory. Serialize ALL generations behind this lock so batched submissions
# just queue up in order.
_GEN_LOCK = threading.Lock()

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
HISTORY_FILE = OUTPUT_DIR / ".history.json"
HISTORY_MAX = 200   # keep last N completed jobs; oldest are trimmed off disk


# ───────────── soft import of the heavy engine ─────────────

TORCH_AVAILABLE = False
DIFFUSERS_AVAILABLE = False
ENGINE_IMPORT_ERROR: Optional[str] = None
try:
    import torch  # noqa: F401
    TORCH_AVAILABLE = True
except Exception as e:  # pragma: no cover - exercised only without the engine
    ENGINE_IMPORT_ERROR = f"torch: {type(e).__name__}: {e}"
try:
    import diffusers  # noqa: F401
    DIFFUSERS_AVAILABLE = True
except Exception as e:  # pragma: no cover
    msg = f"diffusers: {type(e).__name__}: {e}"
    ENGINE_IMPORT_ERROR = msg if ENGINE_IMPORT_ERROR is None else f"{ENGINE_IMPORT_ERROR}; {msg}"

ENGINE_AVAILABLE = TORCH_AVAILABLE and DIFFUSERS_AVAILABLE


# ───────────── pipeline dispatch tables ─────────────
# (family, mode) → diffusers pipeline class name. Resolved lazily from the
# `diffusers` namespace inside the worker so import stays cheap and a missing
# class produces a clear error.
_PIPELINE_CLASSES: dict[tuple[str, str], str] = {
    ("ltx-video", "txt2video"):    "LTXPipeline",
    ("ltx-video", "img2video"):    "LTXImageToVideoPipeline",
    ("wan22", "txt2video"):        "WanPipeline",
    ("wan22", "img2video"):        "WanImageToVideoPipeline",
    ("hunyuanvideo", "txt2video"): "HunyuanVideoPipeline",
    ("hunyuanvideo", "img2video"): "HunyuanVideoImageToVideoPipeline",
    ("cogvideox", "txt2video"):    "CogVideoXPipeline",
    ("cogvideox", "img2video"):    "CogVideoXImageToVideoPipeline",
    ("cogvideox", "video2video"):  "CogVideoXVideoToVideoPipeline",
}

# Valid frame counts differ per architecture: LTX/CogVideoX want 8·n+1,
# Wan/Hunyuan want 4·n+1. We round the requested count to the nearest valid one.
_FRAME_BASE: dict[str, int] = {
    "ltx-video": 8, "wan22": 4, "hunyuanvideo": 4, "cogvideox": 8,
}


# ───────────── diagnostics ─────────────

_PACKAGE_CHECKLIST = [
    ("torch",           "PyTorch tensor engine (MPS backend on Apple Silicon)"),
    ("diffusers",       "Video generation pipelines (engine core)"),
    ("transformers",   "Text encoders for the prompt"),
    ("accelerate",      "Model loading / device placement"),
    ("safetensors",     "Weight file loading"),
    ("sentencepiece",   "Tokenizer backend for some text encoders"),
    ("imageio-ffmpeg",  "mp4 muxing for the exported clip", "imageio_ffmpeg"),
    ("numpy",           "Tensor numerics"),
    ("Pillow",          "Image I/O for image-to-video inputs", "PIL"),
    ("huggingface_hub", "Model registry + cache access"),
]

# Per-engine dependency requirements. Family ids must match the catalog. Every
# video family rides the same torch+diffusers stack.
_COMMON_REQS = ["torch", "diffusers", "transformers", "imageio-ffmpeg", "numpy"]
_ENGINE_REQUIREMENTS = {fid: list(_COMMON_REQS) for fid in catalog.FAMILIES}

# Every catalog family has a working dispatch branch (see _PIPELINE_CLASSES).
_WIRED_FAMILIES = set(catalog.FAMILIES.keys())


def pipeline_available(family: str, mode: str) -> bool:
    """Whether the installed Diffusers exposes this exact pipeline class."""
    class_name = _PIPELINE_CLASSES.get((family, mode))
    if not class_name:
        return False


def _has_diffusers_class(class_name: str) -> bool:
    try:
        import diffusers
        return hasattr(diffusers, class_name)
    except Exception:
        return False


def model_pipeline_available(model: catalog.ModelEntry, mode: str) -> bool:
    """Check the exact class selected for a catalog row."""
    class_name = model.pipeline_classes.get(mode) or _PIPELINE_CLASSES.get((model.family, mode))
    if not class_name:
        return False
    try:
        import diffusers
        return hasattr(diffusers, class_name)
    except Exception:
        return False
    try:
        import diffusers
        return hasattr(diffusers, class_name)
    except Exception:
        return False


def _probe_package(display_name: str, import_name: Optional[str] = None) -> dict:
    target = import_name or display_name
    try:
        import importlib
        mod = importlib.import_module(target)
        version = getattr(mod, "__version__", None)
        return {"installed": True, "version": version, "error": None}
    except Exception as e:
        return {"installed": False, "version": None, "error": f"{type(e).__name__}: {e}"}


def _device() -> Optional[str]:
    if not TORCH_AVAILABLE:
        return None
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def diagnostics() -> dict:
    """Per-package + per-engine health check, rendered as a checklist in the
    Generate tab so users see what's installed and which engines are ready
    BEFORE submitting a job."""
    pkg_results = []
    pkg_status: dict[str, bool] = {}
    for entry in _PACKAGE_CHECKLIST:
        if len(entry) == 3:
            display_name, role, import_name = entry
        else:
            display_name, role = entry
            import_name = None
        probe = _probe_package(display_name, import_name)
        pkg_results.append({"package": display_name, "role": role, **probe})
        pkg_status[display_name] = probe["installed"]

    engine_results = []
    for family, requires in _ENGINE_REQUIREMENTS.items():
        missing = [p for p in requires if not pkg_status.get(p)]
        deps_ok = not missing
        wired = family in _WIRED_FAMILIES
        required_classes = {
            model.pipeline_classes.get(mode) or _PIPELINE_CLASSES.get((family, mode))
            for model in catalog.CATALOG if model.family == family
            for mode in model.capabilities
        }
        missing_pipelines = [class_name for class_name in sorted(required_classes)
                             if class_name and deps_ok and not _has_diffusers_class(class_name)]
        engine_results.append({
            "family": family,
            "requires": requires,
            "missing": missing,
            "deps_ok": deps_ok,
            "wired": wired,
            "missing_pipelines": missing_pipelines,
            "ready": deps_ok and wired and not missing_pipelines,
        })

    return {
        "available": ENGINE_AVAILABLE,
        "error": ENGINE_IMPORT_ERROR,
        "device": _device(),
        "packages": pkg_results,
        "engines": engine_results,
        "any_missing": any(not p["installed"] for p in pkg_results),
        "ready_count": sum(1 for e in engine_results if e["ready"]),
        "total_engines": len(engine_results),
    }


# ───────────── job model ─────────────

@dataclass
class VideoJob:
    job_id: str
    mode: str                            # txt2video | img2video | video2video
    params: dict                         # echoed back so the UI can show settings
    state: str = "queued"                # queued | running | done | error | cancelled
    stage: str = "queued"                # queued | loading | generating | encoding | ...
    progress: float = 0.0                # 0.0 - 1.0; updates on step boundaries
    current_step: int = 0
    total_steps: int = 0
    output_path: Optional[str] = None
    resolved_seed: Optional[int] = None  # the actual seed used (for reproducibility)
    error: Optional[str] = None
    media_info: dict = field(default_factory=dict)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Persisted for restart recovery but intentionally omitted from serialize().
    cloud_state: Optional[dict] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None

    def serialize(self) -> dict:
        duration = None
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            duration = max(0.0, end - self.started_at)
        cloud_recovery = None
        if self.cloud_state:
            state = self.cloud_state
            provider_terminal = bool(state.get("provider_terminal"))
            has_resume_data = isinstance(state.get("submit_raw"), dict)
            cloud_recovery = {
                "provider": state.get("provider"),
                "task_id": state.get("provider_job_id"),
                "status": state.get("provider_state") or "polling",
                "delayed": bool(state.get("delayed_at")),
                "recoverable": has_resume_data and not provider_terminal and not self.output_path
                               and self.state != "cancelled",
                "submission_unknown": state.get("provider_state") == "submit-unknown",
                "last_error": state.get("last_poll_error"),
                "last_checked_at": state.get("last_checked_at"),
                "poll_attempts": int(state.get("poll_attempts") or 0),
            }
        public_params = {k: v for k, v in self.params.items()
                         if k not in ("image_data_uri", "provider_params")}
        return {
            "id": self.job_id,
            "mode": self.mode,
            "state": self.state,
            "progress": self.progress,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "stage": self.stage,
            "params": public_params,
            "output_path": self.output_path,
            "output_url": f"/api/generate/jobs/{self.job_id}/video" if self.output_path else None,
            "resolved_seed": self.resolved_seed,
            "error": self.error,
            "media_info": self.media_info,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": duration,
            "cloud_recovery": cloud_recovery,
        }


class _Cancelled(Exception):
    """Raised from the step callback to abort a running generation."""


# ───────────── pipeline cache (1 entry) ─────────────
# Loading a multi-GB video pipeline is the slowest part of a job. This single
# slot prevents two models being resident during a switch; the manager releases
# it at every terminal job state so success, cancellation, and failure return
# unified memory to the system.
_PIPE_CACHE: dict = {"key": None, "pipe": None}


def _free_cached_pipeline() -> None:
    if _PIPE_CACHE.get("pipe") is not None:
        _PIPE_CACHE["pipe"] = None
        _PIPE_CACHE["key"] = None
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def _torch_dtype(name: str):
    import torch
    return {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }.get(name, torch.bfloat16)


def _load_pipeline(model: catalog.ModelEntry, mode: str):
    """Return a ready-to-call diffusers pipeline placed on the best device.
    Reuses the temporary slot only while the same job is active."""
    import torch
    key = (model.repo, mode)
    if _PIPE_CACHE.get("key") == key and _PIPE_CACHE.get("pipe") is not None:
        return _PIPE_CACHE["pipe"]

    _free_cached_pipeline()

    cls_name = model.pipeline_classes.get(mode) or _PIPELINE_CLASSES.get((model.family, mode))
    if cls_name is None:
        raise ValueError(
            f"{model.label} ({model.family}) does not support '{mode}'. "
            f"Supported: {', '.join(model.capabilities)}."
        )
    import diffusers
    PipelineClass = getattr(diffusers, cls_name, None)
    if PipelineClass is None:
        raise RuntimeError(
            f"Your installed diffusers ({getattr(diffusers, '__version__', '?')}) has no "
            f"{cls_name}. Update diffusers (Reinstall Generation) to a release that ships it."
        )

    defaults = model.video_defaults or {}
    dtype = _torch_dtype(defaults.get("dtype", "bfloat16"))
    from . import cache
    _cached_repo, snapshot = cache.resolve_cached_repo(model.repo, model.aliases)
    source = str(snapshot) if snapshot is not None else model.repo
    pipe = PipelineClass.from_pretrained(source, torch_dtype=dtype, local_files_only=snapshot is not None)

    device = _device() or "cpu"
    pipe = pipe.to(device)

    # Memory savers — best-effort; harmless if a pipeline lacks them.
    for obj, fn in ((pipe, "enable_attention_slicing"),
                    (getattr(pipe, "vae", None), "enable_slicing"),
                    (getattr(pipe, "vae", None), "enable_tiling")):
        try:
            if obj is not None and hasattr(obj, fn):
                getattr(obj, fn)()
        except Exception:
            pass

    _PIPE_CACHE["key"] = key
    _PIPE_CACHE["pipe"] = pipe
    return pipe


def _round_frames(frames: int, base: int) -> int:
    """Round to the nearest valid frame count for the architecture (base·n + 1)."""
    n = max(1, round((frames - 1) / base))
    return base * n + 1


def _filtered_call(pipe, kwargs: dict) -> object:
    """Call pipe(**kwargs) but only pass parameters its __call__ actually
    accepts — video pipelines differ (CogVideoX has no width/height, v2v adds
    `strength`, etc.). Avoids brittle per-pipeline kwargs handling."""
    import inspect
    try:
        accepted = set(inspect.signature(pipe.__call__).parameters)
    except (TypeError, ValueError):
        accepted = set(kwargs)
    call_kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    return pipe(**call_kwargs)


# ───────────── generation manager ─────────────

class VideoManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queue_changed = threading.Condition(self._lock)
        self._jobs: dict[str, VideoJob] = {}
        self._local_queue: list[str] = []
        self._load_history()

    # ----- public API -----

    def is_available(self) -> bool:
        return ENGINE_AVAILABLE

    def availability(self) -> dict:
        return {
            "available": ENGINE_AVAILABLE,
            "error": ENGINE_IMPORT_ERROR,
            "device": _device(),
        }

    def list_jobs(self) -> list[VideoJob]:
        return list(self._jobs.values())

    def serialized_jobs(self) -> list[dict]:
        with self._lock:
            positions = {job_id: index + 1 for index, job_id in enumerate(self._local_queue)}
            jobs = list(self._jobs.values())
        payload = []
        for job in jobs:
            item = job.serialize()
            item["queue_position"] = positions.get(job.job_id)
            payload.append(item)
        return payload

    def get(self, job_id: str) -> Optional[VideoJob]:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Signal cancellation. A queued job flips to 'cancelled' immediately so
        the UI reacts within the next SSE snapshot; a running job is aborted at
        the next denoising-step boundary via the step callback."""
        with self._queue_changed:
            job = self._jobs.get(job_id)
            if job is None or job.state in ("done", "error", "cancelled"):
                return False
            job.cancel_event.set()
            job.stage = "cancelling"
            if job.state == "queued":
                job.state = "cancelled"
                job.stage = "cancelled"
                job.finished_at = time.time()
                if job_id in self._local_queue:
                    self._local_queue.remove(job_id)
                self._queue_changed.notify_all()
                self._persist()
        return True

    def start_txt2video(self, params: dict) -> VideoJob:
        return self._submit("txt2video", params)

    def start_video2video(self, params: dict) -> VideoJob:
        # Covers both image-to-video and clip-to-clip; the exact mode is carried
        # in params["mode"] so the worker picks the right pipeline.
        mode = params.get("mode", "video2video")
        return self._submit(mode, params)

    def submit_cloud(self, mode: str, params: dict, runner) -> VideoJob:
        """Register a CLOUD job in the same registry the local engine uses, so it
        shows up in list_jobs / SSE / /video like any other. `runner(job)` does
        the provider submit→poll→download and sets job.output_path on success.

        Cloud jobs do NOT take _GEN_LOCK (no local GPU) — several can run
        concurrently, they're just HTTP polling."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        job = VideoJob(job_id=uuid.uuid4().hex[:12], mode=mode, params=params, total_steps=0)
        job.params = {**params, "cloud": True}
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist()
        job.thread = threading.Thread(
            target=self._run_cloud, args=(job, runner), name=f"vid-cloud-{job.job_id}", daemon=True)
        job.thread.start()
        return job

    def resume_cloud(self, job: VideoJob, runner) -> bool:
        """Re-attach a persisted cloud job to its existing provider task."""
        with self._lock:
            if job.state not in ("queued", "running", "error") or not job.cloud_state:
                return False
            if job.thread and job.thread.is_alive():
                return False
            job.cancel_event.clear()
            job.state = "queued"
            job.error = None
            job.finished_at = None
            job.thread = threading.Thread(
                target=self._run_cloud, args=(job, runner),
                name=f"vid-cloud-{job.job_id}", daemon=True,
            )
            job.thread.start()
        return True

    def persist_state(self) -> None:
        self._persist()

    def _run_cloud(self, job: VideoJob, runner) -> None:
        if job.cancel_event.is_set():
            job.state = "cancelled"
            job.stage = "cancelled"
            job.finished_at = time.time()
            self._persist()
            return
        job.state = "running"
        job.stage = "provider"
        job.started_at = job.started_at or time.time()
        job.progress = 0.05
        print(f"[vid] starting cloud {job.mode} {job.job_id}: {job.params.get('repo')}", flush=True)
        try:
            runner(job)
            if job.cancel_event.is_set() or not job.output_path:
                job.state = "cancelled" if job.cancel_event.is_set() else "error"
                job.stage = "cancelled" if job.cancel_event.is_set() else "failed"
                if job.state == "error" and not job.error:
                    job.error = "cloud job produced no output"
            else:
                job.progress = 1.0
                job.state = "done"
                job.stage = "completed"
                print(f"[vid] done cloud {job.job_id} → {job.output_path}", flush=True)
        except Exception as e:
            if job.cancel_event.is_set():
                job.state = "cancelled"
                job.stage = "cancelled"
            else:
                job.state = "error"
                job.stage = "failed"
                job.error = f"{type(e).__name__}: {e}"
                print(f"[vid] cloud error {job.job_id}: {job.error}", file=sys.stderr, flush=True)
        finally:
            job.finished_at = time.time()
            self._persist()

    def _submit(self, mode: str, params: dict) -> VideoJob:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        job = VideoJob(
            job_id=uuid.uuid4().hex[:12],
            mode=mode,
            params=params,
            total_steps=int(params.get("steps", 30)),
        )
        with self._queue_changed:
            self._jobs[job.job_id] = job
            self._local_queue.append(job.job_id)
            self._persist()
        job.thread = threading.Thread(
            target=self._run, args=(job,), name=f"vid-{job.job_id}", daemon=True,
        )
        job.thread.start()
        return job

    def clear_history(self) -> int:
        with self._lock:
            terminal = [jid for jid, j in self._jobs.items()
                        if j.state in ("done", "error", "cancelled")]
            jobs = [self._jobs.pop(jid) for jid in terminal]
        for job in jobs:
            self._delete_managed_input(job)
        self._persist()
        return len(terminal)

    def delete_job(self, job_id: str) -> bool:
        """Remove one finished job from history AND delete its .mp4 file from disk.
        (The DELETE .../jobs/{id} route only cancels active jobs; this is for a
        finished clip the user wants gone.)"""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        if job.output_path:
            try:
                output = Path(job.output_path).resolve()
                output.relative_to(OUTPUT_DIR.resolve())
                output.unlink()
            except FileNotFoundError:
                pass
            except ValueError:
                print(f"[vid] refused to delete unmanaged output: {job.output_path}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[vid] delete_job unlink failed: {e}", file=sys.stderr, flush=True)
        self._delete_managed_input(job)
        self._persist()
        return True

    def output_stats(self) -> dict:
        """Total size + count of generated clips in the outputs folder — so the UI
        can show how much disk the outputs are using (the history index and the
        files on disk can diverge)."""
        total = 0
        count = 0
        if OUTPUT_DIR.exists():
            for p in OUTPUT_DIR.glob("*.mp4"):
                try:
                    total += p.stat().st_size
                    count += 1
                except OSError:
                    pass
        return {"bytes": total, "count": count, "dir": str(OUTPUT_DIR.resolve())}

    def prune_outputs(self, keep_last: int = 0, older_than_days: float = 0.0) -> dict:
        """Delete .mp4 clips to reclaim disk. Exactly one mode:
          - keep_last > 0: keep the newest N, delete the rest.
          - older_than_days > 0: delete files older than that many days.
        History entries for deleted files are trimmed too."""
        if not OUTPUT_DIR.exists():
            return {"deleted": 0, "freed_bytes": 0}
        clips = sorted(OUTPUT_DIR.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if keep_last > 0:
            to_delete = clips[keep_last:]
        elif older_than_days > 0:
            cutoff = time.time() - older_than_days * 86400
            to_delete = [p for p in clips if p.stat().st_mtime < cutoff]
        else:
            return {"deleted": 0, "freed_bytes": 0}
        freed = 0
        deleted = 0
        stems = set()
        for p in to_delete:
            try:
                sz = p.stat().st_size
                p.unlink()
                freed += sz
                deleted += 1
                stems.add(p.stem)
            except OSError:
                pass
        if stems:
            with self._lock:
                for jid in [j for j in self._jobs if j in stems]:
                    self._jobs.pop(jid, None)
            self._persist()
        return {"deleted": deleted, "freed_bytes": freed}

    # ----- persistence -----

    def _persist(self) -> None:
        try:
            # Pollers and the repair watchdog can persist concurrently. One lock
            # protects the shared snapshot and temp-file replace from clobbering
            # another job's durable provider task ID.
            with self._lock:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                terminal = [j for j in self._jobs.values()
                            if j.state in ("done", "error", "cancelled")]
                terminal.sort(key=lambda j: j.finished_at or 0, reverse=True)
                terminal = terminal[:HISTORY_MAX]
                active = [j for j in self._jobs.values()
                          if j.state in ("queued", "running")]
                payload = {"jobs": [self._to_disk(j) for j in active + terminal]}
                tmp = HISTORY_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, default=str))
                os.replace(tmp, HISTORY_FILE)
        except Exception as e:
            print(f"[vid] persist failed: {e}", file=sys.stderr, flush=True)

    def _load_history(self) -> None:
        if not HISTORY_FILE.exists():
            return
        try:
            payload = json.loads(HISTORY_FILE.read_text())
            for raw in payload.get("jobs", []):
                job = self._from_disk(raw)
                if job is not None:
                    if job.state in ("queued", "running") and not job.params.get("cloud"):
                        job.state = "error"
                        job.stage = "interrupted"
                        job.error = "Local generation was interrupted by an app restart. Reuse its settings to queue a fresh local render."
                        job.finished_at = time.time()
                    self._jobs[job.job_id] = job
            print(f"[vid] loaded {len(self._jobs)} jobs from history", flush=True)
        except Exception as e:
            print(f"[vid] load history failed: {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _to_disk(job: VideoJob) -> dict:
        durable_params = {k: v for k, v in job.params.items()
                          if k not in ("image_data_uri", "provider_params")}
        return {
            "job_id": job.job_id,
            "mode": job.mode,
            "state": job.state,
            "stage": job.stage,
            "progress": job.progress,
            "current_step": job.current_step,
            "total_steps": job.total_steps,
            "params": durable_params,
            "output_path": job.output_path,
            "resolved_seed": job.resolved_seed,
            "error": job.error,
            "media_info": job.media_info,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "cloud_state": job.cloud_state,
        }

    @staticmethod
    def _from_disk(raw: dict) -> Optional["VideoJob"]:
        try:
            output_path = raw.get("output_path")
            if output_path:
                candidate = Path(output_path).resolve()
                try:
                    candidate.relative_to(OUTPUT_DIR.resolve())
                    if not candidate.is_file():
                        output_path = None
                except ValueError:
                    output_path = None
            media_info = raw.get("media_info") or {}
            if output_path and not media_info:
                try:
                    media_info = _probe_output(Path(output_path))
                except RuntimeError:
                    media_info = {}
            return VideoJob(
                job_id=raw["job_id"],
                mode=raw.get("mode", "txt2video"),
                params=raw.get("params") or {},
                state=raw.get("state", "done"),
                stage=raw.get("stage", raw.get("state", "done")),
                progress=raw.get("progress", 1.0),
                current_step=raw.get("current_step", 0),
                total_steps=raw.get("total_steps", 0),
                output_path=output_path,
                resolved_seed=raw.get("resolved_seed"),
                error=raw.get("error"),
                media_info=media_info,
                started_at=raw.get("started_at"),
                finished_at=raw.get("finished_at"),
                cloud_state=raw.get("cloud_state"),
            )
        except Exception:
            return None

    # ----- worker -----

    def _make_callback(self, job: VideoJob):
        def cb(pipe, step, timestep, callback_kwargs):
            if job.cancel_event.is_set():
                raise _Cancelled()
            job.current_step = step + 1
            if job.total_steps:
                job.progress = min(0.90, 0.15 + 0.75 * (step + 1) / job.total_steps)
            return callback_kwargs
        return cb

    def _run(self, job: VideoJob) -> None:
        with self._queue_changed:
            while self._local_queue and self._local_queue[0] != job.job_id:
                if job.cancel_event.is_set():
                    return
                self._queue_changed.wait(timeout=0.5)
        with _GEN_LOCK:
            if job.cancel_event.is_set():
                job.state = "cancelled"
                job.stage = "cancelled"
                job.finished_at = time.time()
                self._finish_queue_slot(job)
                self._persist()
                return

            job.state = "running"
            job.stage = "preparing"
            job.started_at = time.time()
            job.progress = 0.05          # move the bar off zero the moment work starts
            print(f"[vid] starting {job.mode} {job.job_id}: {job.params}", flush=True)

            if not ENGINE_AVAILABLE:
                job.state = "error"
                job.stage = "failed"
                job.error = f"Video engine not installed: {ENGINE_IMPORT_ERROR}"
                job.finished_at = time.time()
                self._finish_queue_slot(job)
                self._persist()
                return

            try:
                output_path = OUTPUT_DIR / f"{job.job_id}.mp4"
                self._generate(job, output_path)
                if job.cancel_event.is_set():
                    job.state = "cancelled"
                    job.stage = "cancelled"
                else:
                    job.output_path = str(output_path.resolve())
                    job.progress = 1.0
                    job.state = "done"
                    job.stage = "completed"
                    print(f"[vid] done {job.job_id} → {output_path}", flush=True)
            except _Cancelled:
                job.state = "cancelled"
                job.stage = "cancelled"
                print(f"[vid] cancelled {job.job_id}", flush=True)
            except Exception as e:
                if job.cancel_event.is_set():
                    job.state = "cancelled"
                else:
                    job.state = "error"
                    job.stage = "failed"
                    job.error = f"{type(e).__name__}: {e}"
                    print(f"[vid] error {job.job_id}: {job.error}", file=sys.stderr, flush=True)
                    traceback.print_exc()
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass
            finally:
                job.finished_at = time.time()
                _free_cached_pipeline()
                self._finish_queue_slot(job)
                self._persist()

    def _generate(self, job: VideoJob, output_path: Path) -> None:
        import torch
        from diffusers.utils import export_to_video, load_image, load_video

        p = job.params
        model = catalog.get_model(p["repo"])
        if model is None:
            raise ValueError(f"Unknown model: {p['repo']}")
        if job.mode not in model.capabilities:
            raise ValueError(
                f"{model.label} does not support '{job.mode}'. "
                f"Supported: {', '.join(model.capabilities)}."
            )

        defaults = model.video_defaults or {}
        steps = int(p.get("steps") or defaults.get("steps", 30))
        guidance = float(p.get("guidance") or defaults.get("guidance", 5.0))
        fps = int(p.get("fps") or defaults.get("fps", 16))
        width = int(p.get("width") or defaults.get("width", 768))
        height = int(p.get("height") or defaults.get("height", 512))
        frames = int(p.get("frames") or defaults.get("frames", 49))
        frames = _round_frames(frames, _FRAME_BASE.get(model.family, 8))
        if frames > model.max_frames:
            raise ValueError(f"{model.label} supports at most {model.max_frames} frames.")
        if width * height > 2_400_000:
            raise ValueError("Requested resolution exceeds the 2.4 megapixel safety limit.")
        if width % 16 or height % 16:
            raise ValueError("Width and height must both be divisible by 16.")
        estimated_working_bytes = width * height * frames * 16
        free_bytes = shutil.disk_usage(OUTPUT_DIR).free
        if free_bytes < max(2_000_000_000, estimated_working_bytes * 2):
            raise RuntimeError("Not enough free disk space for this render and its temporary data.")
        p["effective_frames"] = frames
        p["effective_duration_s"] = round(frames / fps, 3)
        job.total_steps = steps

        # Seed — honor an explicit seed; otherwise pick one and record it so the
        # result is reproducible.
        seed = p.get("seed")
        if seed in (None, "", -1, "-1"):
            seed = random.randint(0, 2**31 - 1)
        seed = int(seed)
        job.resolved_seed = seed
        generator = torch.Generator().manual_seed(seed)   # CPU generator (MPS-safe)

        job.stage = "loading"
        job.progress = 0.10
        pipe = _load_pipeline(model, job.mode)
        if job.cancel_event.is_set():
            raise _Cancelled()
        job.stage = "generating"
        job.progress = 0.15

        kwargs: dict = {
            "prompt": p.get("prompt", ""),
            "num_frames": frames,
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "width": width,
            "height": height,
            "generator": generator,
            "callback_on_step_end": self._make_callback(job),
        }
        neg = (p.get("negative_prompt") or "").strip()
        if neg:
            kwargs["negative_prompt"] = neg

        if job.mode == "img2video":
            image_path = p.get("image_path")
            if not image_path:
                raise ValueError("image-to-video requires an input image")
            kwargs["image"] = load_image(image_path)
        elif job.mode == "video2video":
            video_path = p.get("video_path")
            if not video_path:
                raise ValueError("video-to-video requires an input video")
            kwargs["video"] = load_video(video_path)
            strength = p.get("strength")
            if strength is not None:
                kwargs["strength"] = float(strength)

        result = _filtered_call(pipe, kwargs)

        if job.cancel_event.is_set():
            return

        frames_out = result.frames[0]
        if job.cancel_event.is_set():
            raise _Cancelled()
        job.stage = "encoding"
        job.progress = 0.95
        export_to_video(frames_out, str(output_path), fps=fps)
        job.media_info = _probe_output(output_path)

    @staticmethod
    def _delete_managed_input(job: VideoJob) -> None:
        uploads = (OUTPUT_DIR.parent / "uploads").resolve()
        for key in ("image_path", "video_path"):
            raw = job.params.get(key)
            if not raw:
                continue
            try:
                target = Path(raw).resolve()
                target.relative_to(uploads)
                target.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def _finish_queue_slot(self, job: VideoJob) -> None:
        with self._queue_changed:
            if job.job_id in self._local_queue:
                self._local_queue.remove(job.job_id)
            self._queue_changed.notify_all()


def _probe_output(path: Path) -> dict:
    """Structured metadata for history and post-encode integrity checks."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_frames:format=duration,size",
             "-of", "json", str(path)], capture_output=True, text=True,
            timeout=20, check=True,
        )
        raw = json.loads(result.stdout)
        stream = (raw.get("streams") or [])[0]
        fmt = raw.get("format") or {}
        rate = str(stream.get("avg_frame_rate") or "0/1").split("/", 1)
        fps = float(rate[0]) / max(1.0, float(rate[1]))
        info = {
            "codec": stream.get("codec_name"), "pixel_format": stream.get("pix_fmt"),
            "width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
            "fps": round(fps, 3), "frames": int(stream.get("nb_frames") or 0),
            "duration_s": round(float(fmt.get("duration") or 0), 3),
            "size_bytes": int(fmt.get("size") or path.stat().st_size),
        }
        if not info["width"] or not info["height"] or not info["frames"] or info["duration_s"] <= 0:
            raise RuntimeError("encoder produced incomplete video metadata")
        return info
    except Exception as exc:
        raise RuntimeError(f"Encoded video failed integrity inspection: {exc}") from exc


manager = VideoManager()
