#!/usr/bin/env python3
"""Verify that every Video Studio change ships with release metadata.

Without ``--base`` this checks the current VERSION/CHANGELOG contract. With a
base git revision it also rejects a product diff that did not increase VERSION
and update CHANGELOG.md. The GitHub workflow uses the latter mode; developers
can run either mode locally.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RELEASE_HEADING_RE = re.compile(
    r"^## \[(\d+\.\d+\.\d+)\](?:\s+[—-]\s+.+)?$", re.MULTILINE
)
RELEASE_METADATA_FILES = frozenset({"VERSION", "CHANGELOG.md"})


def parse_semver(value: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def latest_release(changelog: str) -> tuple[str | None, str]:
    matches = list(RELEASE_HEADING_RE.finditer(changelog))
    if not matches:
        return None, ""
    first = matches[0]
    end = matches[1].start() if len(matches) > 1 else len(changelog)
    return first.group(1), changelog[first.end():end].strip()


def validate_static(root: Path) -> list[str]:
    errors: list[str] = []
    version_path = root / "VERSION"
    changelog_path = root / "CHANGELOG.md"
    if not version_path.is_file():
        return ["VERSION is missing."]
    if not changelog_path.is_file():
        return ["CHANGELOG.md is missing."]

    version = version_path.read_text(encoding="utf-8").strip()
    if parse_semver(version) is None:
        errors.append(f"VERSION must be strict MAJOR.MINOR.PATCH semver, got {version!r}.")

    latest_version, latest_body = latest_release(
        changelog_path.read_text(encoding="utf-8")
    )
    if latest_version != version:
        errors.append(
            "The newest CHANGELOG.md release must match VERSION "
            f"({latest_version!r} != {version!r})."
        )
    if not re.search(r"^###\s+\S", latest_body, re.MULTILINE):
        errors.append("The newest changelog release needs a descriptive section heading.")
    if not re.search(r"^-\s+\S", latest_body, re.MULTILINE):
        errors.append("The newest changelog release needs at least one detail bullet.")
    return errors


def validate_release_delta(
    *,
    current_version: str,
    base_version: str,
    changed_paths: Iterable[str],
    latest_changelog_version: str | None,
) -> list[str]:
    changed = {path.strip() for path in changed_paths if path.strip()}
    substantive = changed - RELEASE_METADATA_FILES
    if not substantive:
        return []

    errors: list[str] = []
    current = parse_semver(current_version)
    base = parse_semver(base_version)
    if current is None or base is None:
        errors.append("Both current and base VERSION values must be strict semver.")
    elif current <= base:
        errors.append(
            f"Repository changes require a VERSION increase ({base_version} -> {current_version})."
        )
    if "VERSION" not in changed:
        errors.append("Repository changes require VERSION to be changed.")
    if "CHANGELOG.md" not in changed:
        errors.append("Repository changes require CHANGELOG.md to be changed.")
    if latest_changelog_version != current_version:
        errors.append("The newest changelog release must describe the new VERSION.")
    return errors


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def validate_against_base(root: Path, base_ref: str) -> list[str]:
    merge_base = _git(root, "merge-base", base_ref, "HEAD")
    # Include committed, staged, unstaged, and untracked work so the documented
    # local command is useful before a release commit as well as inside CI.
    changed = set(
        _git(root, "diff", "--name-only", f"{merge_base}...HEAD").splitlines()
    )
    changed.update(_git(root, "diff", "--name-only", "--cached").splitlines())
    changed.update(_git(root, "diff", "--name-only").splitlines())
    changed.update(
        _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    )
    base_version = _git(root, "show", f"{merge_base}:VERSION").strip()
    current_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    latest_version, _ = latest_release(
        (root / "CHANGELOG.md").read_text(encoding="utf-8")
    )
    return validate_release_delta(
        current_version=current_version,
        base_version=base_version,
        changed_paths=sorted(changed),
        latest_changelog_version=latest_version,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        help="Base git revision used to require a version and changelog change.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    errors = validate_static(root)
    if args.base:
        try:
            errors.extend(validate_against_base(root, args.base))
        except RuntimeError as exc:
            errors.append(str(exc))

    if errors:
        for error in dict.fromkeys(errors):
            print(f"RELEASE_GUARD_ERROR: {error}", file=sys.stderr)
        return 1

    suffix = f" against {args.base}" if args.base else ""
    print(f"RELEASE_GUARD_OK: {(root / 'VERSION').read_text().strip()}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
