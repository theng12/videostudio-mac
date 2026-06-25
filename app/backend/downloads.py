"""
Download manager.

Wraps huggingface_hub.snapshot_download in a background thread per job.
Progress is recovered by polling the on-disk cache (bytes downloaded /
total expected bytes), since snapshot_download's internal tqdm is awkward
to capture without monkey-patching globally.

Total expected bytes are fetched once via the HF API (HfApi.repo_info)
before the download starts. Resume is automatic in huggingface_hub 0.27+
(the previous opt-in `resume_download=True` was removed in 1.0). Partial
files are kept as <hash>.incomplete and continued from the byte offset
on retry. Cancelling a job leaves the .incomplete files in place; the
next start_download for the same repo picks up where it left off.

NB: huggingface_hub will sometimes discard a partial and restart from
byte 0 if it can't verify the partial matches the current remote etag.
This is server-driven and outside our control.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Optional

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.utils import HfHubHTTPError

from . import cache, settings


@dataclass
class DownloadJob:
    job_id: str
    repo: str
    token: Optional[str] = None
    state: str = "queued"           # queued | running | paused | done | error | cancelled
    error: Optional[str] = None
    total_bytes: int = 0            # 0 until we resolve it from HF API
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    # Sliding-window byte-rate, smoothed with an EMA so the UI doesn't
    # oscillate when chunks land in bursts.
    _last_speed_sample: Optional[tuple[float, int]] = field(default=None, repr=False)
    _speed_bps: float = field(default=0.0, repr=False)

    def serialize(self) -> dict:
        bytes_done = cache.disk_bytes(self.repo)
        bytes_partial = cache.incomplete_bytes(self.repo)
        observed = bytes_done + bytes_partial

        # Update the rolling speed estimate. Only meaningful while running;
        # cleared when the job reaches a terminal state so the UI doesn't show
        # stale speeds next to a finished job.
        now = time.time()
        if self.state == "running":
            if self._last_speed_sample is None:
                self._last_speed_sample = (now, observed)
            else:
                last_t, last_b = self._last_speed_sample
                dt = now - last_t
                if dt >= 0.5:
                    delta = max(0, observed - last_b)
                    instant = delta / dt
                    # EMA: 30% new + 70% old. Smooths spikes from chunk arrivals
                    # without lagging too far behind real bandwidth changes.
                    self._speed_bps = 0.3 * instant + 0.7 * self._speed_bps
                    self._last_speed_sample = (now, observed)
        else:
            self._speed_bps = 0.0

        percent = None
        if self.total_bytes > 0:
            percent = min(100.0, observed / self.total_bytes * 100.0)
        elif self.state == "done":
            percent = 100.0

        # ETA: remaining bytes at current speed. Only show when speed is
        # nonzero and there's a known total.
        eta_seconds = None
        if self.state == "running" and self._speed_bps > 0 and self.total_bytes > 0:
            remaining = max(0, self.total_bytes - observed)
            eta_seconds = remaining / self._speed_bps

        return {
            "id": self.job_id,
            "repo": self.repo,
            "state": self.state,
            "error": self.error,
            "bytes_done": bytes_done,
            "bytes_partial": bytes_partial,
            "bytes_observed": observed,
            "bytes_total": self.total_bytes,
            "percent": percent,
            "speed_bps": self._speed_bps,
            "eta_seconds": eta_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class DownloadManager:
    """In-memory registry of download jobs, keyed by repo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, DownloadJob] = {}     # job_id -> job
        self._active_by_repo: dict[str, str] = {}   # repo   -> active job_id

    # ---------- public API ----------

    def start(self, repo: str, token: Optional[str] = None) -> DownloadJob:
        with self._lock:
            existing = self._active_for_repo_locked(repo)
            if existing is not None:
                return existing
            job = DownloadJob(job_id=uuid.uuid4().hex[:12], repo=repo, token=token or None)
            self._jobs[job.job_id] = job
            self._active_by_repo[repo] = job.job_id

        job.thread = threading.Thread(
            target=self._run, args=(job,), name=f"dl-{repo}", daemon=True
        )
        job.thread.start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.state in ("done", "error", "cancelled"):
            return False
        job.cancel_event.set()
        # The thread will observe the cancel on the next file write; mark
        # the state immediately so the UI reflects user intent.
        if job.state == "running":
            job.state = "cancelling"
        return True

    def get(self, job_id: str) -> Optional[DownloadJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    def clear_finished(self) -> int:
        """Drop all terminal-state jobs from memory. Returns count removed."""
        with self._lock:
            terminal = [jid for jid, j in self._jobs.items()
                        if j.state in ("done", "error", "cancelled")]
            for jid in terminal:
                self._jobs.pop(jid, None)
            # Also prune any active_by_repo pointers that referenced removed jobs
            for repo, jid in list(self._active_by_repo.items()):
                if jid not in self._jobs:
                    self._active_by_repo.pop(repo, None)
        return len(terminal)

    def active_for_repo(self, repo: str) -> Optional[DownloadJob]:
        with self._lock:
            return self._active_for_repo_locked(repo)

    # ---------- internals ----------

    def _active_for_repo_locked(self, repo: str) -> Optional[DownloadJob]:
        job_id = self._active_by_repo.get(repo)
        if job_id is None:
            return None
        job = self._jobs.get(job_id)
        # Also treat "cancelling" as no-longer-active so a user can immediately
        # kick off a fresh attempt even if the old thread is wedged on a dead
        # socket. hf_hub's internal `.locks/` directory keeps concurrent writes
        # on the same blob safe.
        if job is None or job.state in ("done", "error", "cancelled", "cancelling"):
            self._active_by_repo.pop(repo, None)
            return None
        return job

    def _resolve_total_bytes(self, repo: str, token: Optional[str]) -> int:
        effective = token or settings.get_hf_token()
        try:
            info = HfApi().repo_info(repo_id=repo, files_metadata=True, token=effective)
        except HfHubHTTPError:
            return 0
        except Exception:
            return 0
        total = 0
        for sibling in info.siblings or []:
            size = getattr(sibling, "size", None) or 0
            try:
                total += int(size)
            except (TypeError, ValueError):
                continue
        return total

    def _run(self, job: DownloadJob) -> None:
        job.state = "running"
        job.started_at = time.time()
        job.total_bytes = self._resolve_total_bytes(job.repo, job.token)
        cache.ensure_hub_dir()
        print(
            f"[downloads] starting {job.repo}  "
            f"(job={job.job_id}, total={job.total_bytes / 1e9:.2f} GB)",
            flush=True,
        )

        try:
            # cache_dir omitted on purpose — honours HF_HOME from env.
            # resume is automatic in huggingface_hub 0.27+; the explicit
            # resume_download kwarg was removed in 1.0.
            # If the user didn't pass a per-download token, fall back to the
            # global token from Settings — useful for gated repos and higher
            # rate limits.
            effective_token = job.token or settings.get_hf_token()
            snapshot_download(
                repo_id=job.repo,
                token=effective_token,
            )
            if job.cancel_event.is_set():
                job.state = "cancelled"
                print(f"[downloads] cancelled {job.repo}", flush=True)
            else:
                job.state = "done"
                print(f"[downloads] done {job.repo}", flush=True)
        except Exception as e:
            if job.cancel_event.is_set():
                job.state = "cancelled"
                print(f"[downloads] cancelled (during exception) {job.repo}", flush=True)
            else:
                job.state = "error"
                job.error = f"{type(e).__name__}: {e}"
                print(f"[downloads] error {job.repo}: {job.error}", file=sys.stderr, flush=True)
                traceback.print_exc()
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active_by_repo.get(job.repo) == job.job_id:
                    self._active_by_repo.pop(job.repo, None)


manager = DownloadManager()
