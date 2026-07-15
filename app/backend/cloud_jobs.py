"""Cloud generation lifecycle: gate → submit → poll → download.

Routes a provider-prefixed generation (``fal:…``) to the matching adapter and
drives it to completion, registering the job in the SAME VideoManager the local
engine uses — so /api/generate/jobs, the SSE stream, /jobs/{id}/video and the
Hub's output scanner all work with zero changes. The result mp4 is downloaded
into app/output/<job_id>.mp4, exactly like a local clip.
"""
from __future__ import annotations

import time
import threading
import ipaddress
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import settings as app_settings, spend
from .providers.base import CloudVideoModel
from .providers import registry
from .video import OUTPUT_DIR, _probe_output, manager as gen_manager

POLL_INTERVAL_S = 4.0
DELAYED_AFTER_S = 20 * 60
DELAYED_POLL_INTERVAL_S = 30.0
MAX_RETRY_BACKOFF_S = 5 * 60.0
WATCHDOG_INTERVAL_S = 30.0
_DL_TIMEOUT_S = 300
_MAX_RESULT_BYTES = 2_000_000_000
_WATCHDOG_LOCK = threading.Lock()
_watchdog_started = False


class NoProviderKey(Exception):
    pass


class PaidUseDisabled(Exception):
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
    provider_cfg = ((app_settings.get("providers") or {}).get(prov.key) or {})
    if not provider_cfg.get("paid"):
        raise PaidUseDisabled(
            f"Paid use is off for {prov.name}. Enable it in Settings before generating."
        )
    for existing in gen_manager.list_jobs():
        saved = existing.cloud_state or {}
        if (saved.get("provider") == prov.key
                and saved.get("provider_state") == "submit-unknown"
                and not saved.get("provider_terminal")):
            raise ValueError(
                f"{prov.name} has an unresolved submission ({existing.job_id}) whose provider task ID "
                "was not returned. Video Studio will not risk another paid submission. Check the "
                "provider dashboard and resolve that job first."
            )

    if mode not in model.capabilities:
        raise ValueError(
            f"{model.label} does not support {mode}. Supported: {', '.join(model.capabilities)}."
        )
    duration = params.get("duration")
    if duration is not None and model.max_duration_s is not None:
        if float(duration) > model.max_duration_s:
            raise ValueError(f"{model.label} supports at most {model.max_duration_s:g} seconds.")
    resolution = params.get("resolution")
    if resolution and model.resolutions and resolution not in model.resolutions:
        raise ValueError(
            f"{model.label} does not support {resolution}. Choose: {', '.join(model.resolutions)}."
        )
    aspect_ratio = params.get("aspect_ratio")
    if aspect_ratio and model.aspect_ratios and aspect_ratio not in model.aspect_ratios:
        raise ValueError(
            f"{model.label} does not support {aspect_ratio}. Choose: {', '.join(model.aspect_ratios)}."
        )

    est = prov.estimate_cost(model, params)
    if est is None:
        raise ValueError(
            f"{model.label} has no verified price, so Video Studio will not submit a billable job. "
            "Refresh the provider catalog or configure a trusted price first."
        )
    # Pre-submit guardrail — may raise CapExceeded (before any spend).
    spend.check_gate(prov.key, est)
    spend_id = spend.record_submit(prov.key, model.id, job_id="", est_usd=est)

    def runner(job):
        _patch_spend_job(spend_id, job.job_id)
        _drive(job, prov, model, mode, params, spend_id, est)

    job = gen_manager.submit_cloud(mode, params, runner)
    return job, est


def _drive(job, prov, model, mode: str, params: dict, spend_id: str,
           est: float, submit_raw: Optional[dict] = None) -> None:
    out_path = OUTPUT_DIR / f"{job.job_id}.mp4"
    if submit_raw is None:
        # Persist intent before crossing the paid API boundary. If the provider
        # accepts the job but its response times out, this record prevents an
        # automatic or accidental second submission.
        job.cloud_state = {
            "provider": prov.key,
            "model_id": model.id,
            "provider_model": model.provider_model,
            "model_label": model.label,
            "mode": mode,
            "spend_id": spend_id,
            "estimate_usd": est,
            "submitted_at": time.time(),
            "provider_state": "submitting",
            "provider_terminal": False,
            "poll_attempts": 0,
            "consecutive_errors": 0,
        }
        gen_manager.persist_state()
        try:
            submit = prov.submit(model, mode, params)
        except Exception as exc:
            job.cloud_state.update({
                "provider_state": "submit-unknown",
                "last_poll_error": f"Submission outcome unknown: {type(exc).__name__}: {exc}",
            })
            gen_manager.persist_state()
            raise RuntimeError(
                "Provider submission outcome is unknown. Video Studio will not resubmit; "
                "check the provider dashboard for the task."
            ) from exc
        submit_raw = submit.raw
        # Save the returned durable ID before the first poll. From this point
        # onward recovery always polls this task and NEVER resubmits.
        job.cloud_state.update({
            "provider_job_id": submit.provider_job_id,
            "submit_raw": submit_raw,
            "provider_state": "submitted",
        })
        gen_manager.persist_state()

    recovery = job.cloud_state or {}
    if not recovery.get("provider_job_id"):
        recovery["provider_job_id"] = _provider_job_id(submit_raw)
    recovery.setdefault("submitted_at", job.started_at or time.time())
    recovery.setdefault("provider_terminal", False)
    job.cloud_state = recovery

    while True:
        if job.cancel_event.is_set():
            if not recovery.get("provider_completed"):
                try:
                    prov.cancel(model, submit_raw)
                except Exception as exc:
                    recovery["last_poll_error"] = f"Cancel request failed: {exc}"
            recovery.update({"provider_state": "cancelled", "provider_terminal": True})
            gen_manager.persist_state()
            if not recovery.get("provider_completed"):
                spend.record_finish(spend_id, actual_usd=None, duration_s=None, state="cancelled")
            return

        now = time.time()
        recovery["poll_attempts"] = int(recovery.get("poll_attempts") or 0) + 1
        try:
            status = prov.poll(model, submit_raw)
        except Exception as exc:
            failures = int(recovery.get("consecutive_errors") or 0) + 1
            recovery.update({
                "provider_state": "retrying",
                "last_poll_error": f"{type(exc).__name__}: {exc}",
                "last_checked_at": now,
                "consecutive_errors": failures,
            })
            gen_manager.persist_state()
            delay = min(MAX_RETRY_BACKOFF_S, max(POLL_INTERVAL_S, 2 ** min(failures, 8)))
            _wait(job, delay)
            continue

        recovery.update({
            "last_checked_at": now,
            "consecutive_errors": 0,
            "last_poll_error": None,
        })

        if status.state == "error":
            recovery.update({
                "provider_state": "error",
                "provider_terminal": True,
                "last_poll_error": status.error or "Provider reported a terminal error.",
            })
            gen_manager.persist_state()
            spend.record_finish(spend_id, actual_usd=None, duration_s=None, state="error")
            raise RuntimeError(status.error or "provider reported an error")

        if status.state == "done":
            # Credits have been consumed once the provider reports completion,
            # even if fetching the local copy needs retries afterward.
            first_completion = not recovery.get("provider_completed")
            recovery.update({
                "provider_completed": True,
                "provider_completed_at": recovery.get("provider_completed_at") or time.time(),
                "provider_state": "result-download",
            })
            if first_completion:
                spend.record_finish(spend_id, actual_usd=est, duration_s=None, state="done")
            try:
                if not status.result_url:
                    raise RuntimeError("provider reported done but returned no video URL")
                _download(status.result_url, out_path)
            except Exception as exc:
                # The provider task is already paid and may be complete. Keep its
                # reservation and poll again for a fresh result URL; never submit.
                failures = int(recovery.get("consecutive_errors") or 0) + 1
                recovery.update({
                    "provider_state": "result-retry",
                    "last_poll_error": f"Result download failed: {type(exc).__name__}: {exc}",
                    "consecutive_errors": failures,
                })
                gen_manager.persist_state()
                _wait(job, min(MAX_RETRY_BACKOFF_S, 2 ** min(failures, 8)))
                continue
            job.output_path = str(out_path.resolve())
            # Test adapters may stub the downloader; production downloads must
            # exist and are structurally inspected before becoming playable.
            if out_path.exists():
                job.media_info = _probe_output(out_path)
            duration_s = _mp4_duration_s(out_path)
            if duration_s is not None:
                actual_usd = est
                if model.price_unit == "per_second":
                    actual_usd = round(float(model.price_usd or 0) * duration_s, 4)
                spend.record_finish(
                    spend_id, actual_usd=actual_usd, duration_s=duration_s, state="done")
            recovery.update({
                "provider_state": "done",
                "provider_terminal": True,
                "last_poll_error": None,
                "completed_at": time.time(),
            })
            gen_manager.persist_state()
            return

        if status.progress is not None:
            job.progress = min(0.9, max(job.progress, status.progress))
        elif job.progress < 0.9:
            job.progress = min(0.9, (job.progress or 0.05) + 0.03)

        elapsed = now - float(recovery.get("submitted_at") or now)
        if elapsed >= DELAYED_AFTER_S:
            if not recovery.get("delayed_at"):
                recovery["delayed_at"] = now
                gen_manager.persist_state()
            recovery["provider_state"] = "delayed"
            _wait(job, DELAYED_POLL_INTERVAL_S)
        else:
            recovery["provider_state"] = status.state or "running"
            _wait(job, POLL_INTERVAL_S)


def resume_inflight() -> int:
    """Re-attach every recoverable task without submitting a second task."""
    resumed = 0
    for job in gen_manager.list_jobs():
        resumed += int(_resume_one(job))
    return resumed


def repair_job(job_id: str):
    """Manually re-attach one saved task. Returns (job, newly_attached)."""
    job = gen_manager.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if not _recoverable(job):
        raise ValueError("This job has no recoverable provider task.")
    return job, _resume_one(job)


def start_repair_watchdog() -> None:
    """Continuously heal recoverable jobs whose poller thread has stopped."""
    global _watchdog_started
    with _WATCHDOG_LOCK:
        if _watchdog_started:
            return
        _watchdog_started = True

    def worker() -> None:
        while True:
            try:
                resumed = resume_inflight()
                if resumed:
                    print(f"[cloud] watchdog repaired {resumed} job(s)", flush=True)
            except Exception as exc:
                print(f"[cloud] repair watchdog: {exc}", flush=True)
            time.sleep(WATCHDOG_INTERVAL_S)

    threading.Thread(target=worker, name="cloud-repair-watchdog", daemon=True).start()


def _recoverable(job) -> bool:
    state = job.cloud_state or {}
    return bool(state and isinstance(state.get("submit_raw"), dict)
                and not state.get("provider_terminal") and not job.output_path
                and job.state != "cancelled")


def _resume_one(job) -> bool:
    if not _recoverable(job):
        return False
    state = job.cloud_state or {}
    pair = _recovery_pair(state)
    spend_id = state.get("spend_id")
    submit_raw = state.get("submit_raw")
    est = state.get("estimate_usd")
    if pair is None or not spend_id or not isinstance(submit_raw, dict) or est is None:
        job.state = "error"
        job.error = "Saved provider task is incomplete or its model is unavailable; repair will retry automatically."
        state["last_poll_error"] = job.error
        gen_manager.persist_state()
        return False
    prov, model = pair

    def runner(active_job, p=prov, m=model, s=state, raw=submit_raw):
        _drive(active_job, p, m, s.get("mode", active_job.mode), active_job.params,
               s["spend_id"], float(s["estimate_usd"]), raw)

    return gen_manager.resume_cloud(job, runner)


def _recovery_pair(state: dict):
    """Resolve a saved task even after its model ages out of the live catalog."""
    pair = registry.provider_for_id(state.get("model_id", ""))
    if pair is not None:
        return pair
    provider_key = state.get("provider")
    provider_model = state.get("provider_model")
    prov = registry.PROVIDERS.get(provider_key)
    if prov is None or not provider_model:
        return None
    model_id = state.get("model_id") or f"{provider_key}:{provider_model}"
    return prov, CloudVideoModel(
        id=model_id,
        provider=provider_key,
        provider_model=provider_model,
        label=state.get("model_label") or provider_model,
    )


def _provider_job_id(raw: dict) -> Optional[str]:
    for key in ("request_id", "id", "task_id", "taskId"):
        value = raw.get(key)
        if value:
            return str(value)
    return None


def _wait(job, seconds: float) -> None:
    """Back off while still reacting immediately to user cancellation."""
    job.cancel_event.wait(max(0.0, seconds))


def _mp4_duration_s(path: Path) -> Optional[float]:
    """Read duration from the MP4 `mvhd` atom without another dependency."""
    try:
        data = path.read_bytes()
        marker = data.find(b"mvhd")
        if marker < 0 or marker + 40 > len(data):
            return None
        version = data[marker + 4]
        if version == 1:
            timescale = int.from_bytes(data[marker + 28:marker + 32], "big")
            duration = int.from_bytes(data[marker + 32:marker + 40], "big")
        else:
            timescale = int.from_bytes(data[marker + 16:marker + 20], "big")
            duration = int.from_bytes(data[marker + 20:marker + 24], "big")
        return round(duration / timescale, 4) if timescale > 0 else None
    except (OSError, ValueError):
        return None


def _validate_public_https_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Provider result URL must be HTTPS.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"Provider result host could not be resolved: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Provider result URL resolved to a private or local address.")


def _download(url: str, dest: Path) -> None:
    _validate_public_https_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".mp4.part")
    req = urllib.request.Request(url, headers={"User-Agent": "videostudio"})
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=_DL_TIMEOUT_S) as r, tmp.open("wb") as f:
            _validate_public_https_url(r.geturl())
            declared = int(r.headers.get("Content-Length") or 0)
            if declared > _MAX_RESULT_BYTES:
                raise ValueError("Provider result exceeds the 2 GB safety limit.")
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_RESULT_BYTES:
                    raise ValueError("Provider result exceeds the 2 GB safety limit.")
                f.write(chunk)
        if written == 0:
            raise ValueError("Provider returned an empty video.")
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _patch_spend_job(spend_id: str, job_id: str) -> None:
    try:
        with spend._LOCK, spend._conn() as conn:  # noqa: SLF001 (internal helper)
            conn.execute("UPDATE spend SET job_id = ? WHERE id = ?", (job_id, spend_id))
    except Exception:
        pass
