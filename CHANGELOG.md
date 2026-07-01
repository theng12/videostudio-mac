# Changelog — Video Studio KH

All notable changes to Video Studio KH are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change. Re-install required.
- **MINOR** (1.1.x → 1.2.x) — new engine / new feature / new model family. **Re-run "Install Generation"** to pick up new Python deps.
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak / catalog entry within an existing family. **Just run Update** from the Pinokio sidebar.

---

## [0.1.1] — 2026-07-01

### Fixed — numeric/UX consistency audit: catalog size and live download size disagreed for the same model

First formal audit pass for Video Studio KH, applying the same sweep already run on Voice Studio KH (v1.7.2–v1.7.4). Checked byte/GB formatting and UX/UI consistency across the whole frontend.

**Byte formatting split-brain (`app/frontend/app.js`):** `fmtBytes()` — used for live download progress (`Models` card during an active download, and the `Downloads` tab) — divided by 1024 at each step (binary/GiB math) but labeled the result "KB"/"MB"/"GB"/"TB" (decimal/SI unit names). `formatGb()` — used for the catalog's static `size_gb` estimate shown on every model card — is decimal, matching how Hugging Face's API reports file sizes (`app/backend/downloads.py` sums real byte counts from `HfApi.repo_info`, and even logs them as decimal GB via `/1e9`). Same model, same card: the static size and the live download size could show different numbers for the identical repo (e.g. a 19.0 GB catalog entry would show "17.7 GB / 17.7 GB" while downloading). Fixed `fmtBytes()` to divide by 1000, matching `formatGb()`, the catalog, and the backend's own decimal convention — the exact fix already validated in Voice Studio KH v1.7.2/v1.7.3.

**`formatGb()`'s own sub-1 GB branch** rounded via `gb * 1024` instead of `gb * 1000` for models under 1 GB. No current catalog entry is under 1 GB, so this hadn't visibly fired yet, but it's the same bug class (would silently inflate a future small model's advertised size by ~2.4%, same as the Voice Studio Kokoro case). Fixed to `* 1000` proactively.

**Verified:** `node --check` on the edited file, plus a direct logic check against a real catalog value — `formatGb(19.0)` and `fmtBytes(19000000000)` (LTX-Video's real size in bytes) now both print `"19.0 GB"`; before the fix they printed `"19.0 GB"` vs `"17.7 GB"`. Also confirmed `formatGb(0.34)` now returns `"340 MB"` instead of `"348 MB"`. A live in-browser walkthrough was **not** possible this pass — the app has never been installed on this machine (no `conda_env` yet) and Pinokio's bundled `conda`/`pterm` tooling isn't reachable from a plain shell outside Pinokio's own runtime, so no server could be started to click through. Recommend a quick manual check in the Models/Downloads tabs after the next install.

**Checked, no change needed (UX/UI sweep):**
- **Chip color semantics** — `.chip.ok/.fit-ok/.engine-ready`, `.chip.warn/.fit-tight/.engine-missing`, `.chip.fit-risky`, cache-state chips, pill states, and settings messages all already reference `var(--good)`/`var(--warn)`/`var(--bad)` directly (`style.css`). No duplicate hardcoded palettes to consolidate — this app didn't have the split-palette problem Voice Studio had.
- **Terminology** — "cached" / "partial" / "not downloaded" is used consistently everywhere that state appears. "engine-ready" / "engine-missing" is a deliberately separate axis (cached *and* the generation engine installed), not a synonym for the cache state — not drift.
- **Interaction parity** — there are no confirmation dialogs anywhere in the app (Download, Cancel, Reveal in Finder, Clear history/downloads/filters all act immediately), applied uniformly across tabs. No asymmetry between entry points.
- **Minor, left as-is:** `cancelDownload()` is labeled "Cancel" (ghost button, Models tab card) vs "cancel" (link-btn, Downloads tab list) — same action, same immediate behavior in both places, just different casing/chrome. Per the audit's own priority guidance this is low-value wording/styling, not a functional inconsistency, so left unchanged.
