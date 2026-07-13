"""Cloud generation lifecycle: gate → submit → poll → download.

Routes a provider-prefixed generation (``fal:…``) to the matching adapter and
drives it to completion, registering the job in the SAME VideoManager the local
engine uses — so /api/generate/jobs, the SSE stream, /jobs/{id}/video and the
Hub's output scanner all work with zero changes. The result mp4 is downloaded
into app/output/<job_id>.mp4, exactly like a local clip.
"""
from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import spend
from .providers import registry
from .video import OUTPUT_DIR, manager as gen_manager

POLL_INTERVAL_S = 4.0
MAX_WAIT_S = 20 * 60      # cloud video jobs are minutes, not hours
_DL_TIMEOUT_S = 300


class NoProviderKey(Exception):
    pass


def start_cloud_generation(mode: str, params: dict):
    """Validate + gate, then spawn the cloud job. Returns (VideoJob, estimate_usd).
    Raises spend.CapExceeded (→ 402), NoProviderKey (→ 400), or ValueError."""
    repo = params.get("repo", "")
    pair = registry.provider_for_id(repo)
    if pair is None:
        raise ValueError(f"Unknown cloud model: {repo}")
    prov, model = pair

    if not prov.has_key():
        raise NoProviderKey(f"No API key set for {prov.name}. Add it in Settings → Cloud providers.")

    est = prov.estimate_cost(model, params)
    # Pre-submit guardrail — may raise CapExceeded (before any spend).
    spend.check_gate(prov.key, est)
    spend_id = spend.record_submit(prov.key, model.id, job_id="", est_usd=est)

    def runner(job):
        _patch_spend_job(spend_id, job.job_id)
        out_path = OUTPUT_DIR / f"{job.job_id}.mp4"
        submit = None
        try:
            submit = prov.submit(model, mode, params)
            deadline = time.time() + MAX_WAIT_S
            while True:
                if job.cancel_event.is_set():
                    try:
                        prov.cancel(model, submit.raw)
                    except Exception:
                        pass
                    return  # _run_cloud marks it cancelled
                status = prov.poll(model, submit.raw)
                if status.state == "done":
                    if not status.result_url:
                        raise RuntimeError("provider reported done but returned no video URL")
                    _download(status.result_url, out_path)
                    job.output_path = str(out_path.resolve())
                    return
                if status.state == "error":
                    raise RuntimeError(status.error or "provider reported an error")
                # still running — nudge the progress bar and back off
                if job.progress < 0.9:
                    job.progress = min(0.9, (job.progress or 0.05) + 0.03)
                if time.time() > deadline:
                    raise RuntimeError(f"cloud job timed out after {MAX_WAIT_S // 60} min")
                time.sleep(POLL_INTERVAL_S)
        finally:
            # Book the outcome. On success the estimate is the actual (providers
            # generate the requested length; per_video is exact). On failure the
            # row is marked error so its reserved estimate no longer counts.
            if job.output_path:
                spend.record_finish(spend_id, actual_usd=est,
                                    duration_s=None, state="done")
            else:
                st = "cancelled" if job.cancel_event.is_set() else "error"
                spend.record_finish(spend_id, actual_usd=None, duration_s=None, state=st)

    job = gen_manager.submit_cloud(mode, params, runner)
    return job, est


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".mp4.part")
    req = urllib.request.Request(url, headers={"User-Agent": "videostudio"})
    with urllib.request.urlopen(req, timeout=_DL_TIMEOUT_S) as r, tmp.open("wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def _patch_spend_job(spend_id: str, job_id: str) -> None:
    try:
        with spend._LOCK, spend._conn() as conn:  # noqa: SLF001 (internal helper)
            conn.execute("UPDATE spend SET job_id = ? WHERE id = ?", (job_id, spend_id))
    except Exception:
        pass
