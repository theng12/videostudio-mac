from pathlib import Path
import importlib.util
import re


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release.py"
SPEC = importlib.util.spec_from_file_location("video_release_guard", SCRIPT)
release_guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release_guard)


def test_current_version_matches_detailed_whats_new_entry():
    assert release_guard.validate_static(ROOT) == []


def test_product_change_requires_version_increase_and_changelog_change():
    errors = release_guard.validate_release_delta(
        current_version="1.2.3",
        base_version="1.2.3",
        changed_paths=["app/backend/main.py"],
        latest_changelog_version="1.2.3",
    )
    assert any("VERSION increase" in error for error in errors)
    assert any("VERSION to be changed" in error for error in errors)
    assert any("CHANGELOG.md to be changed" in error for error in errors)


def test_product_change_passes_with_new_version_and_matching_notes():
    assert release_guard.validate_release_delta(
        current_version="1.2.4",
        base_version="1.2.3",
        changed_paths=["VERSION", "CHANGELOG.md", "app/frontend/app.js"],
        latest_changelog_version="1.2.4",
    ) == []


def test_metadata_only_correction_does_not_demand_another_release():
    assert release_guard.validate_release_delta(
        current_version="1.2.3",
        base_version="1.2.3",
        changed_paths=["CHANGELOG.md"],
        latest_changelog_version="1.2.3",
    ) == []


def test_all_launcher_stops_use_canonical_app_local_uris():
    for name in ("update.js", "install_generation.js"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert 'uri: "{{path.resolve(cwd, \'start.js\')}}"' in source
        assert not re.search(
            r'method:\s*"script\.stop",\s*params:\s*\{\s*uri:\s*"start\.js"',
            source,
        )
