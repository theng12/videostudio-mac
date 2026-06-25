"""
Import existing HF-cache-style model folders into our HF_HOME.

Two transfer modes:

- **link** (default): create a symlink under HF_HOME/hub pointing at the
  source. Instant, zero copy — but breaks if the source is later deleted.
  Best when you want to keep the original source app installed alongside.

- **move**: physically relocate the folder into HF_HOME/hub via
  `shutil.move`. Same filesystem = instant inode rename, cross-filesystem =
  copy+delete. After a successful move the source is gone, so the
  destination is independent and you can safely uninstall the source app.
  Best for the "I'm migrating off another launcher" case.

The "Scan candidates" flow looks at folders listed in
VIDEOSTUDIO_EXTRA_MODEL_DIRS (colon-separated) for HF-cache-style
'models--owner--repo' subfolders.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import cache, catalog


@dataclass
class ImportCandidate:
    source_path: str
    repo: str
    in_catalog: bool

    def serialize(self) -> dict:
        return {
            "source_path": self.source_path,
            "repo": self.repo,
            "in_catalog": self.in_catalog,
        }


def _parse_hf_folder_name(name: str) -> Optional[str]:
    """'models--Lightricks--LTX-Video' -> 'Lightricks/LTX-Video'."""
    if not name.startswith("models--"):
        return None
    rest = name[len("models--"):]
    # Repo names can contain '-' but the path separator is '--', which we
    # need to split on conservatively. HF uses the first '--' as the org
    # boundary; the rest is the repo path (which itself may contain '--').
    parts = rest.split("--", 1)
    if len(parts) != 2:
        return None
    org, repo = parts
    if not org or not repo:
        return None
    return f"{org}/{repo}"


def _is_valid_hf_folder(path: Path) -> bool:
    """A folder counts as HF-cache-formatted if it has either blobs/ or snapshots/."""
    return path.is_dir() and ((path / "blobs").exists() or (path / "snapshots").exists())


def extra_dirs() -> list[Path]:
    raw = os.environ.get("VIDEOSTUDIO_EXTRA_MODEL_DIRS", "").strip()
    if not raw:
        return []
    out: list[Path] = []
    for token in raw.split(":"):
        token = token.strip()
        if not token:
            continue
        p = Path(token).expanduser()
        if p.exists():
            out.append(p.resolve())
    return out


def scan_for_candidates() -> list[ImportCandidate]:
    """
    Walk the configured extra dirs (one level deep) plus their immediate
    'hub' subfolder, looking for HF-cache-style 'models--*--*' folders that
    aren't already linked into our hub.
    """
    our_hub = cache.hub_dir().resolve()
    seen: set[str] = set()
    out: list[ImportCandidate] = []
    for root in extra_dirs():
        candidates: list[Path] = []
        # Allow either '<root>/models--*' or '<root>/hub/models--*'.
        for child in root.iterdir():
            candidates.append(child)
            if child.is_dir() and child.name == "hub":
                try:
                    candidates.extend(child.iterdir())
                except FileNotFoundError:
                    pass
        for c in candidates:
            if not c.is_dir():
                continue
            repo = _parse_hf_folder_name(c.name)
            if repo is None:
                continue
            if not _is_valid_hf_folder(c):
                continue
            if c.resolve() == (our_hub / c.name).resolve():
                continue   # already at home
            if repo in seen:
                continue
            seen.add(repo)
            out.append(ImportCandidate(
                source_path=str(c.resolve()),
                repo=repo,
                in_catalog=catalog.get_model(repo) is not None,
            ))
    return out


def import_path(source_path: str, repo: Optional[str] = None, mode: str = "link") -> dict:
    """
    Bring an existing HF cache folder into our HF_HOME/hub.

    `mode`:
      - "link": symlink (the original stays put, our hub references it)
      - "move": physically relocate the folder into our hub

    Returns a dict describing what happened.
    """
    if mode not in ("link", "move"):
        return {"ok": False, "error": f"Unknown mode: {mode}"}

    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        return {"ok": False, "error": f"Path does not exist: {src}"}
    if not _is_valid_hf_folder(src):
        return {"ok": False, "error": f"Not an HF cache folder (no blobs/ or snapshots/): {src}"}

    if repo is None:
        repo = _parse_hf_folder_name(src.name)
        if repo is None:
            return {
                "ok": False,
                "error": (
                    f"Could not infer repo from folder name '{src.name}'. "
                    "Pass the repo explicitly (e.g. 'Lightricks/LTX-Video')."
                ),
            }

    cache.ensure_hub_dir()
    target = cache.repo_cache_dir(repo)

    # If a symlink to this very source already exists, treat as a no-op for link
    # mode, or fall through to upgrade-to-move (unlink + move) for move mode.
    if target.exists():
        if target.is_symlink() and target.resolve() == src:
            if mode == "link":
                return {"ok": True, "mode": "link", "already": True, "repo": repo,
                        "target": str(target)}
            # mode == "move" and target is a symlink pointing at our source:
            # unlink the symlink, then proceed to physically move the source
            # into the freed target path below.
            try:
                target.unlink()
            except OSError as e:
                return {"ok": False, "error": f"could not remove stale symlink: {e}",
                        "repo": repo}
        else:
            return {
                "ok": False,
                "error": (
                    f"Target already exists at {target}. Remove it first if you want to "
                    f"re-{mode}."
                ),
                "repo": repo,
            }

    if mode == "link":
        try:
            target.symlink_to(src, target_is_directory=True)
        except OSError as e:
            return {"ok": False, "error": f"symlink failed: {e}", "repo": repo}
        return {"ok": True, "mode": "link", "repo": repo,
                "target": str(target), "source": str(src)}

    # mode == "move": shutil.move handles both same-fs rename (instant) and
    # cross-fs copy+delete fallback transparently.
    try:
        shutil.move(str(src), str(target))
    except OSError as e:
        return {"ok": False, "error": f"move failed: {e}", "repo": repo}
    return {"ok": True, "mode": "move", "repo": repo,
            "target": str(target), "source_was": str(src)}
