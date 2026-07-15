"""Safe, opt-in application updates for the KH Studio family.

The web server only starts this module in a detached helper process.  That
helper can therefore stop and restart the server without killing itself.  All
commands are fixed argument arrays; user settings never become shell input.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Optional
from urllib.request import Request, urlopen


MODES = {"off", "notify", "auto"}
FREQUENCIES = {"daily", "weekly"}
STATES = {"idle", "checking", "available", "deferred", "updating",
          "restarting", "succeeded", "failed"}
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
SECRET_RE = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key)\s*[:=]\s*([^\s,;]+)")
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    """An expected, actionable updater refusal or failure."""


class UpdateDeferred(UpdateError):
    """Work is active, so the update must be retried later."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Optional[dt.datetime] = None) -> str:
    return (value or _utc_now()).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {k: ("[redacted]" if re.search(r"token|secret|password|key", k, re.I)
                    else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", value)
    return value


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise UpdateError(f"Unsafe symlinked updater directory: {path.parent}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_redact(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


class AutoUpdater:
    def __init__(self, spec: dict, readiness: Optional[Callable[[], list[str]]] = None,
                 *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 now: Callable[[], dt.datetime] = _utc_now) -> None:
        self.spec = dict(spec)
        self.root = Path(self.spec["root"]).resolve()
        self.readiness = readiness
        self.runner = runner
        self.now = now
        self.state_dir = self.root / "auto_update"
        self.config_path = self.state_dir / "config.json"
        self.status_path = self.state_dir / "status.json"
        self.lock_path = self.state_dir / "update.lock"
        self.log_dir = self.root / "logs" / "auto_update"
        self.agent_label = f"com.kh.{self.spec['slug']}.updater"
        self.agent_path = Path.home() / "Library" / "LaunchAgents" / f"{self.agent_label}.plist"
        self._thread_lock = threading.Lock()
        self._validate_spec()
        self.log = self._make_logger()

    def _validate_spec(self) -> None:
        if self.root.is_symlink() or not (self.root / ".git").is_dir():
            raise UpdateError("Updater root must be a real Git checkout.")
        branch = self.spec.get("branch", "main")
        if not BRANCH_RE.fullmatch(branch) or branch.startswith("-") or ".." in branch:
            raise UpdateError("Unsafe configured Git branch.")
        remote = self.spec.get("expected_remote", "")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", remote):
            raise UpdateError("Unsafe expected Git remote.")
        port = int(self.spec["port"])
        if not 1024 <= port <= 65535:
            raise UpdateError("Unsafe app port.")

    def _make_logger(self) -> logging.Logger:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"kh-auto-update.{self.spec['slug']}.{id(self)}")
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(self.log_dir / "updater.log", maxBytes=1_000_000,
                                      backupCount=4, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    @property
    def defaults(self) -> dict:
        return {
            "mode": "off",
            "frequency": "daily",
            "maintenance_hour": int(self.spec["default_hour"]),
            "idle_only": True,
            "weekday": int(self.spec.get("default_weekday", 6)),
        }

    def _load_json(self, path: Path, fallback: dict) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else dict(fallback)
        except (OSError, ValueError):
            return dict(fallback)

    def settings(self) -> dict:
        data = {**self.defaults, **self._load_json(self.config_path, {})}
        if data.get("mode") not in MODES:
            data["mode"] = "off"
        if data.get("frequency") not in FREQUENCIES:
            data["frequency"] = "daily"
        data["maintenance_hour"] = max(0, min(23, int(data.get("maintenance_hour", self.defaults["maintenance_hour"]))))
        data["idle_only"] = bool(data.get("idle_only", True))
        return data

    def _read_status(self) -> dict:
        return self._load_json(self.status_path, {
            "state": "idle", "last_checked": None, "latest_version": None,
            "next_check": None, "last_update_result": None, "defer_reason": None,
            "details": [], "rollback": None, "pending_manual": False,
        })

    def _write_status(self, **changes: object) -> dict:
        status = self._read_status()
        status.update(_redact(changes))
        state = status.get("state")
        if state not in STATES:
            status["state"] = "failed"
        _atomic_json(self.status_path, status)
        return status

    def installed_version(self) -> str:
        try:
            return (self.root / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"

    def _version_matches(self, actual: object, expected: str) -> bool:
        value = str(actual or "")
        return value == expected or (
            bool(self.spec.get("allow_build_suffix")) and value.startswith(expected + ".")
        )

    def release_notes_url(self) -> str:
        return self.spec["expected_remote"][:-4] + "/blob/main/CHANGELOG.md"

    def public_status(self) -> dict:
        status = self._read_status()
        settings = self.settings()
        latest = status.get("latest_version")
        installed = self.installed_version()
        return {
            **status,
            "settings": settings,
            "installed_version": installed,
            "latest_version": latest,
            "update_available": bool(latest and latest != installed),
            "scheduler": self.scheduler_status(),
            "release_notes_url": self.release_notes_url(),
        }

    def _next_regular(self, settings: Optional[dict] = None) -> dt.datetime:
        cfg = settings or self.settings()
        now = self.now().astimezone()
        candidate = now.replace(hour=cfg["maintenance_hour"], minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        if cfg["frequency"] == "weekly":
            target = int(cfg.get("weekday", 6)) % 7
            candidate += dt.timedelta(days=(target - candidate.weekday()) % 7)
        return candidate.astimezone(dt.timezone.utc)

    def save_settings(self, payload: dict) -> dict:
        current = self.settings()
        mode = payload.get("mode", current["mode"])
        frequency = payload.get("frequency", current["frequency"])
        hour = payload.get("maintenance_hour", current["maintenance_hour"])
        idle_only = payload.get("idle_only", current["idle_only"])
        if mode not in MODES:
            raise UpdateError("Mode must be off, notify, or auto.")
        if frequency not in FREQUENCIES:
            raise UpdateError("Frequency must be daily or weekly.")
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise UpdateError("Maintenance hour must be from 0 through 23.")
        if not isinstance(idle_only, bool):
            raise UpdateError("Idle-only must be true or false.")
        saved = {"mode": mode, "frequency": frequency, "maintenance_hour": hour,
                 "idle_only": idle_only, "weekday": int(current.get("weekday", 6))}
        _atomic_json(self.config_path, saved)
        next_check = None if mode == "off" else _iso(self._next_regular(saved))
        self._write_status(next_check=next_check, defer_reason=None,
                           pending_manual=False, state="idle")
        applied = self.apply_scheduler()
        if mode != "off" and not applied["installed"]:
            raise UpdateError("Settings saved, but the automatic-update schedule could not be installed.")
        if mode == "off" and applied["installed"]:
            raise UpdateError("Settings saved, but the automatic-update schedule is still loaded.")
        return self.public_status()

    def _plist(self) -> bytes:
        python = self.root / "conda_env" / "bin" / "python"
        if not python.is_file():
            python = Path(sys.executable)
        payload = {
            "Label": self.agent_label,
            "ProgramArguments": [str(python), "-m", "backend.auto_update", "--scheduled"],
            "WorkingDirectory": str(self.root / "app"),
            "RunAtLoad": True,
            "StartInterval": 900,
            "ProcessType": "Background",
            "ThrottleInterval": 60,
            "StandardOutPath": str(self.log_dir / "launchd.log"),
            "StandardErrorPath": str(self.log_dir / "launchd.err.log"),
            "EnvironmentVariables": {
                "PATH": f"{self._pinokio_home() / 'bin/miniforge/bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONUNBUFFERED": "1",
            },
        }
        return plistlib.dumps(payload, sort_keys=True)

    def _launchctl(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return self.runner(["/bin/launchctl", *args], text=True, capture_output=True,
                           timeout=30, check=check)

    def apply_scheduler(self, *, force_pending: bool = False) -> dict:
        self.agent_path.parent.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        domain = f"gui/{uid}"
        self._launchctl("bootout", f"{domain}/{self.agent_label}")
        should_install = self.settings()["mode"] != "off" or force_pending
        if not should_install:
            with contextlib.suppress(FileNotFoundError):
                self.agent_path.unlink()
            return self.scheduler_status()
        if self.agent_path.exists() and self.agent_path.is_symlink():
            raise UpdateError("Refusing a symlinked LaunchAgent file.")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.agent_path.with_name(f".{self.agent_path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(self._plist())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.agent_path)
        result = self._launchctl("bootstrap", domain, str(self.agent_path))
        if result.returncode:
            raise UpdateError(f"launchd rejected the updater schedule: {result.stderr.strip()}")
        return self.scheduler_status()

    def scheduler_status(self) -> dict:
        if sys.platform != "darwin":
            return {"installed": False, "label": self.agent_label, "supported": False}
        result = self._launchctl("print", f"gui/{os.getuid()}/{self.agent_label}")
        return {"installed": result.returncode == 0, "label": self.agent_label,
                "supported": True, "plist": str(self.agent_path)}

    def _run(self, args: list[str], *, cwd: Optional[Path] = None,
             timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        safe_args = [str(a) for a in args]
        self.log.info("run %s", " ".join(safe_args))
        result = self.runner(safe_args, cwd=str(cwd or self.root), text=True,
                             capture_output=True, timeout=timeout)
        if check and result.returncode:
            message = (result.stderr or result.stdout or "command failed").strip()
            raise UpdateError(f"{safe_args[0]} failed: {_redact(message)}")
        return result

    def _git(self, *args: str, timeout: int = 120, check: bool = True) -> str:
        result = self._run(["/usr/bin/git", *args], timeout=timeout, check=check)
        return result.stdout.strip()

    def _pinokio_home(self) -> Path:
        # Every supported checkout is PINOKIO_HOME/api/<app>. Resolve from the
        # fixed repository location instead of trusting environment input.
        home = self.root.parent.parent.resolve()
        if self.root.parent != home / "api":
            raise UpdateError("Repository is outside PINOKIO_HOME/api.")
        return home

    def _git_preflight(self, *, fetch: bool = True) -> dict:
        if self._git("remote", "get-url", "origin") != self.spec["expected_remote"]:
            raise UpdateError("Unexpected Git remote. Repair origin before updating.")
        branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch != self.spec.get("branch", "main"):
            raise UpdateError("Updater requires the configured main branch (not detached HEAD).")
        dirty = self._git("status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise UpdateError("Working tree has local changes. Commit or remove them before updating.")
        if fetch:
            self._git("fetch", "--prune", "origin", self.spec.get("branch", "main"), timeout=180)
        local = self._git("rev-parse", "HEAD")
        remote_ref = f"origin/{self.spec.get('branch', 'main')}"
        remote = self._git("rev-parse", remote_ref)
        previous = self._read_status().get("last_remote_commit")
        if previous and previous != remote:
            if self._run(["/usr/bin/git", "merge-base", "--is-ancestor", str(previous), remote],
                         check=False).returncode:
                raise UpdateError("Remote history was rewritten. Automatic update refused.")
        if local != remote:
            if self._run(["/usr/bin/git", "merge-base", "--is-ancestor", local, remote],
                         check=False).returncode:
                raise UpdateError("Local and remote history diverged. Fast-forward update refused.")
        latest = self._git("show", f"{remote_ref}:VERSION").strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z-]+){1,4}", latest):
            raise UpdateError("Published VERSION metadata is invalid.")
        return {"local": local, "remote": remote, "latest": latest,
                "available": local != remote}

    def check(self) -> dict:
        self._write_status(state="checking", defer_reason=None, details=[])
        try:
            result = self._git_preflight(fetch=True)
            state = "available" if result["available"] else "succeeded"
            self._write_status(state=state, last_checked=_iso(self.now()),
                               latest_version=result["latest"],
                               last_remote_commit=result["remote"],
                               last_update_result="Update available" if result["available"] else "Already up to date",
                               details=["Git remote, branch, worktree, and fast-forward safety checks passed."])
        except Exception as exc:
            self._write_status(state="failed", last_checked=_iso(self.now()),
                               last_update_result="Update check failed",
                               details=[str(_redact(str(exc)))])
            raise
        return self.public_status()

    def trigger_check(self) -> dict:
        if not self._thread_lock.acquire(blocking=False):
            raise UpdateError("An update check is already running.")
        def worker() -> None:
            try:
                self.check()
            except Exception:
                self.log.exception("update check failed")
            finally:
                self._thread_lock.release()
        threading.Thread(target=worker, daemon=True).start()
        return self.public_status()

    def readiness_reasons(self) -> list[str]:
        if self.readiness is not None:
            return [str(x) for x in self.readiness() if x]
        url = f"http://127.0.0.1:{int(self.spec['port'])}/api/auto-update/readiness"
        try:
            with urlopen(Request(url, headers={"User-Agent": "KH-Studio-Updater/1"}), timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            return [str(x) for x in data.get("reasons", []) if x]
        except Exception:
            return []  # a stopped app owns no active jobs

    def readiness_status(self) -> dict:
        reasons = self.readiness_reasons()
        return {"idle": not reasons, "reasons": reasons}

    def _notify(self, title: str, message: str) -> None:
        clean_title = str(_redact(title))[:100]
        clean_message = str(_redact(message))[:240]
        script = 'display notification "' + clean_message.replace("\\", "\\\\").replace('"', '\\"') + \
                 '" with title "' + clean_title.replace("\\", "\\\\").replace('"', '\\"') + '"'
        self._run(["/usr/bin/osascript", "-e", script], timeout=15, check=False)

    @contextlib.contextmanager
    def _exclusive_lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError("Another update is already running.") from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _health(self, expected: str, *, timeout: int = 90) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{int(self.spec['port'])}/api/health"
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=3) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if data.get("ok") and self._version_matches(data.get("app_version"), expected):
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _service_loaded(self) -> bool:
        label = self.spec.get("server_label")
        if not label:
            return False
        return self._launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0

    def _health_alive(self) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{int(self.spec['port'])}/api/health", timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def active_mode(self) -> str:
        if (self.root / "service" / ".installed").exists() or self._service_loaded():
            return "service"
        if self._health_alive():
            return "pinokio"
        return "stopped"

    def _pterm(self, action: str) -> None:
        home = self._pinokio_home()
        node = home / "bin" / "miniforge" / "bin" / "node"
        cli = home / "bin" / "npm" / "lib" / "node_modules" / "pterm" / "index.js"
        if not node.is_file() or not cli.is_file():
            raise UpdateError("Pinokio command helper is unavailable; use Repair in Pinokio.")
        ref = f"pinokio://127.0.0.1:42000/api/{self.root.name}"
        self._run([str(node), str(cli), action, "start.js", "--ref", ref], timeout=90)

    def _stop_mode(self, mode: str) -> None:
        if mode == "service":
            domain = f"gui/{os.getuid()}"
            for label in (self.spec.get("watchdog_label"), self.spec.get("server_label")):
                if label:
                    self._launchctl("bootout", f"{domain}/{label}")
        elif mode == "pinokio":
            self._pterm("stop")
        deadline = time.monotonic() + 30
        while self._health_alive() and time.monotonic() < deadline:
            time.sleep(1)
        if self._health_alive():
            raise UpdateError("The app did not stop cleanly; no files were changed.")

    def _start_mode(self, mode: str) -> None:
        if mode == "service":
            self._run(["/bin/bash", "install_service.sh"], timeout=120)
        elif mode == "pinokio":
            self._pterm("start")

    def _python(self) -> Path:
        candidate = self.root / "conda_env" / "bin" / "python"
        if not candidate.is_file():
            raise UpdateError("The app environment is not installed.")
        return candidate

    def _install_dependencies(self) -> None:
        python = self._python()
        uv = self._pinokio_home() / "bin" / "miniforge" / "bin" / "uv"
        base = self.root / "app" / self.spec.get("requirements", "requirements.txt")
        if not base.is_file():
            raise UpdateError("Base requirements file is missing.")
        prefix = [str(uv), "pip", "install", "--python", str(python)] if uv.is_file() \
                 else [str(python), "-m", "pip", "install"]
        self._run([*prefix, "-r", str(base)], cwd=self.root / "app", timeout=1200)
        marker = self.spec.get("generation_marker")
        generation = self.root / "app" / self.spec.get("generation_requirements", "requirements-generation.txt")
        if marker and generation.is_file() and any((self.root / "conda_env" / "lib").glob(f"python*/site-packages/{marker}")):
            self._run([*prefix, "-r", str(generation)], cwd=self.root / "app", timeout=1800)

    def _verify_import(self, expected: str) -> None:
        module = self.spec.get("verify_module", "backend.main")
        allow_suffix = bool(self.spec.get("allow_build_suffix"))
        code = ("import importlib; m=importlib.import_module(" + repr(module) + "); "
                "v=str(getattr(m,'APP_VERSION','')); expected=" + repr(expected) + "; "
                "assert v==expected or (" + repr(allow_suffix) + " and v.startswith(expected+'.')), (v,expected); "
                "print('UPDATE_VERIFY_OK')")
        self._run([str(self._python()), "-c", code], cwd=self.root / "app", timeout=180)

    def _temporary_health(self, expected: str) -> bool:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        process = subprocess.Popen(
            [str(self._python()), "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(self.root / "app"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                        data = json.loads(response.read().decode("utf-8"))
                    return bool(data.get("ok") and self._version_matches(data.get("app_version"), expected))
                except Exception:
                    time.sleep(1)
            return False
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _verify_health(self, mode: str, expected: str) -> bool:
        return self._temporary_health(expected) if mode == "stopped" else self._health(expected)

    def _rollback(self, old_sha: str, new_sha: str, mode: str, old_version: str) -> bool:
        try:
            self._stop_mode(mode)
            if self._git("rev-parse", "HEAD") != new_sha:
                raise UpdateError("Repository changed during rollback; manual repair required.")
            if self._git("status", "--porcelain", "--untracked-files=normal"):
                raise UpdateError("Worktree changed during rollback; manual repair required.")
            # Move only the updater-applied clean tree back. This is deliberately
            # not `git reset --hard`, and it runs only after proving no user edit
            # exists in the worktree.
            self._git("read-tree", "--reset", "-u", old_sha)
            self._git("update-ref", "refs/heads/main", old_sha, new_sha)
            self._install_dependencies()
            self._verify_import(old_version)
            self._start_mode(mode)
            return self._verify_health(mode, old_version)
        except Exception:
            self.log.exception("rollback failed")
            return False

    def update(self, *, automatic: bool = False) -> dict:
        with self._exclusive_lock():
            old_sha = ""
            new_sha = ""
            old_version = self.installed_version()
            mode = self.active_mode()
            try:
                reasons = self.readiness_reasons()
                if reasons:
                    reason = "; ".join(reasons)
                    self._write_status(state="deferred", defer_reason=reason,
                                       last_update_result="Update deferred",
                                       next_retry=_iso(self.now() + dt.timedelta(minutes=15)))
                    self._notify(f"{self.spec['title']} update deferred", reason)
                    raise UpdateDeferred(reason)
                if shutil.disk_usage(self.root).free < int(self.spec.get("min_free_bytes", MIN_FREE_BYTES)):
                    raise UpdateError("Not enough free disk space for a safe update and rollback.")
                preflight = self._git_preflight(fetch=True)
                old_sha = preflight["local"]
                new_sha = preflight["remote"]
                if not preflight["available"]:
                    self._write_status(state="succeeded", latest_version=preflight["latest"],
                                       last_checked=_iso(self.now()), last_update_result="Already up to date",
                                       defer_reason=None, pending_manual=False)
                    return self.public_status()
                self._write_status(state="updating", latest_version=preflight["latest"],
                                   last_checked=_iso(self.now()), defer_reason=None,
                                   details=[f"Rollback point {old_sha[:12]}", f"Active mode: {mode}"])
                self._notify(f"{self.spec['title']} update started", f"Installing {preflight['latest']}")
                self._stop_mode(mode)
                self._git("merge", "--ff-only", f"origin/{self.spec.get('branch', 'main')}")
                self._install_dependencies()
                self._verify_import(preflight["latest"])
                self._write_status(state="restarting")
                self._start_mode(mode)
                if not self._verify_health(mode, preflight["latest"]):
                    raise UpdateError("The updated app did not become healthy on the expected version.")
                self._write_status(state="succeeded", last_update_result=f"Updated to {preflight['latest']}",
                                   rollback=None, pending_manual=False, next_retry=None,
                                   next_check=_iso(self._next_regular()),
                                   details=["Dependencies installed.", "Import check passed.",
                                            "Health and running version verified."])
                self._notify(f"{self.spec['title']} update succeeded", f"Now running {preflight['latest']}")
                if self.settings()["mode"] == "off":
                    self.apply_scheduler()
                return self.public_status()
            except UpdateDeferred:
                raise
            except Exception as exc:
                rollback = None
                if old_sha and new_sha and old_sha != new_sha:
                    rollback = self._rollback(old_sha, new_sha, mode, old_version)
                message = str(_redact(str(exc)))
                self._write_status(state="failed", last_update_result="Update failed",
                                   rollback="succeeded" if rollback else ("failed" if rollback is False else None),
                                   details=[message], pending_manual=False)
                self._notify(f"{self.spec['title']} update failed",
                             message if rollback is None else f"{message} Rollback {'succeeded' if rollback else 'failed'}.")
                if self.settings()["mode"] == "off":
                    self.apply_scheduler()
                raise

    def trigger_update(self, *, after_current: bool = False) -> dict:
        if after_current:
            reasons = self.readiness_reasons()
            self._write_status(state="deferred", defer_reason="; ".join(reasons) if reasons else "Queued for the next idle check",
                               pending_manual=True, next_retry=_iso(self.now()))
            self.apply_scheduler(force_pending=True)
            return self.public_status()
        self._spawn("--update", "--manual")
        self._write_status(state="updating", last_update_result="Update started", defer_reason=None)
        return self.public_status()

    def retry(self) -> dict:
        return self.trigger_update(after_current=False)

    def _spawn(self, *args: str) -> None:
        python = self._python()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(self.log_dir / "helper.log", "a", encoding="utf-8")
        try:
            subprocess.Popen([str(python), "-m", "backend.auto_update", *args],
                             cwd=str(self.root / "app"), stdout=stream, stderr=stream,
                             start_new_session=True, close_fds=True)
        finally:
            stream.close()

    def scheduled(self) -> dict:
        status = self._read_status()
        cfg = self.settings()
        pending = bool(status.get("pending_manual"))
        if cfg["mode"] == "off" and not pending:
            self.apply_scheduler()
            return self.public_status()
        due_values = [_parse_iso(status.get("next_check")), _parse_iso(status.get("next_retry"))]
        due_values = [x for x in due_values if x]
        if due_values and self.now() < min(due_values):
            return self.public_status()
        if pending:
            try:
                return self.update(automatic=False)
            except UpdateDeferred:
                return self.public_status()
        checked = self.check()
        self._write_status(next_check=_iso(self._next_regular()), next_retry=None)
        if not checked.get("update_available"):
            return self.public_status()
        if cfg["mode"] == "notify":
            version = checked.get("latest_version")
            if status.get("notified_version") != version:
                self._notify(f"{self.spec['title']} update available", f"Version {version} is ready")
                self._write_status(notified_version=version)
            return self.public_status()
        if cfg["mode"] == "auto":
            try:
                return self.update(automatic=True)
            except UpdateDeferred:
                return self.public_status()
        return self.public_status()


def cli() -> int:
    from .auto_update_config import create_updater
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--manual", action="store_true")
    args = parser.parse_args()
    updater = create_updater()
    try:
        if args.update:
            updater.update(automatic=not args.manual)
        else:
            updater.scheduled()
        return 0
    except UpdateDeferred:
        return 0
    except Exception:
        updater.log.exception("updater helper failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())

