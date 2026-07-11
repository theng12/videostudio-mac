# Changelog — Video Studio KH

All notable changes to Video Studio KH are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change. Re-install required.
- **MINOR** (1.1.x → 1.2.x) — new engine / new feature / new model family. **Re-run "Install Generation"** to pick up new Python deps.
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak / catalog entry within an existing family. **Just run Update** from the Pinokio sidebar.

---

## [0.2.8] — 2026-07-10

### Added — "Open outputs folder" button (+ Clear-history fix)

- **Open outputs folder** — new button in the history header that reveals the folder holding every generated clips file in Finder, via the existing `/api/reveal`.

### Notes
- PATCH bump (0.2.7 → 0.2.8) — frontend only. Live on reload; no restart needed.

---
## [0.2.7] — 2026-07-10

### Fixed — download ETA settle-guard and honest catalog sizes

**Absurd download ETA (`downloads.py`).** Same suite-wide fix: the speed EMA's first near-zero sample (taken before real bytes land) produced ETAs like "99679m 03s" seconds after clicking Download. `eta_seconds` is now suppressed until the job has ≥3 s of runtime. (The frontend doesn't surface ETA yet; the guard is applied for correctness and consistency with the other studios.)

**Catalog sizes now reflect the true download size.** These repos download unfiltered, so the old `size_gb` values were far too low. Corrected all 10 entries to the real Hugging Face repo sizes — most dramatically `Lightricks/LTX-Video` 19→254 GB (the repo bundles every model version), plus LTX-Video-0.9.7-distilled 19→48, Wan2.2-A14B T2V/I2V 62→126 each, Wan2.2-TI2V-5B 20→34, and HunyuanVideo 40→42/44. Verified against the HF API `blobs=true` listing.

**Checked, left unchanged:** memory floors — for video diffusion, peak runtime memory is driven by activations, not the (multi-version) download size, so the runtime floors were left as the authors set them. Download filtering (to avoid pulling every bundled version) was deferred as it needs per-model load-testing. `py_compile` clean.

## [0.2.6] — 2026-07-10

### Fixed — Inactive tabs no longer flash during startup

The four primary panes used Alpine `x-show` without `x-cloak`. Before Alpine finished
initializing, browsers rendered Generate, Models, Downloads, and Settings together for a
brief but visible full-page flash. All panes now use the stylesheet's existing cloak rule
and reveal only after Alpine has selected the active tab.

### Verification

- Reproduced the pre-fix state with scripts delayed (all four panes computed as visible),
  then confirmed all four remain hidden until Alpine initializes and only Generate shows.
- Video routes, decimal byte formatting, download names, and generation behavior were
  checked and deliberately left unchanged.

---

## [0.2.5] — 2026-07-10

### Changed — Video Studio now uses the shared Studio workspace shell

Video Studio still used an older two-row header and opened Generate without any model
or clip context. Navigation is now consolidated into a compact responsive header, while
Generate surfaces the selected model, source mode, and calculated clip length before the
render controls. Settings now uses a clear heading and responsive two-column layout.

### Verification

- Validated Alpine expressions, JavaScript syntax, HTML parsing, responsive desktop and
  mobile renders, and the existing launcher URL-capture pattern without installing the
  optional video-generation stack.
- Generation jobs, model defaults, downloads, token persistence, service mode, and API
  behavior were checked and deliberately left unchanged.

---

## [0.2.4] — 2026-07-10

### Changed — Version now shown as a badge in the top-right header (consistent across all sibling apps)

The app version was displayed inconsistently across the Studio fleet (bottom footer on
some, top-right on Chat, missing on Video). It's now a small `v0.2.4`-style badge in the
top-right of the header on every app, matching Chat Studio — visible at a glance without
scrolling to a footer.

### Notes

- PATCH bump (0.2.3 → 0.2.4) — frontend only (`index.html` + `style.css`). Served with
  no-cache headers, so it appears on the next browser reload without a restart.

---
## [0.2.3] — 2026-07-10

### Fixed — Update reinstalls the service (rewrites the launchd plist) instead of kickstarting a stale one

The service scripts were renamed from generic `serve.sh` / `watchdog.sh` to
`<app>-serve.sh` / `<app>-watchdog.sh`, and the launchd plist's `ProgramArguments`
now points at the renamed script. A machine with the service already installed has
a plist pointing at the OLD `serve.sh` — so a plain **kickstart** (`restart_service.sh`)
would relaunch a plist pointing at a now-deleted path and the service would fail to
come back up after an update.

`update.js` (and `install_generation.js`) now restart the service with
**`install_service.sh`** instead of `restart_service.sh`. `install_service.sh`
regenerates the plist to match the current on-disk scripts *before* relaunching
(bootout → bootstrap → kickstart), so the rename is folded in automatically. It's
idempotent and safe to run on every update.

### Notes

- PATCH bump (0.2.2 → 0.2.3) — launcher scripts only. Applies only where the app
  runs as a launchd service (`service/.installed`); the `start.js` path is unchanged.

---
## [0.2.2] — 2026-07-10

### Added — In-app auto-check banner: tells you when to update instead of failing silently

On load the web UI checks `GET /api/update-status` and shows a dismissible banner when this install needs attention:

- **A newer version is published** — compares this install's VERSION against the repo's published VERSION (fetched from GitHub raw, cached ~6h, in a background thread so it never blocks). Banner: "⬆ Update available (vX → vY)", pointing at the one-click **Update** button in the Pinokio sidebar.
- **The generation engine isn't installed** — detects the missing stack directly. Banner: "⚠ Generation engine not installed — the Generate tab won't work", pointing at **Install Generation** (or **Update**) in the sidebar. This is the exact silent failure that let a broken generation install look fine before.

Detect-in-app, apply-via-sidebar: a sandboxed web page (external browser, Tailscale) can't reliably drive Pinokio's script runner, so the banner points at the sidebar's one-click Update rather than trying to self-update. The banner is self-contained (no framework coupling) and degrades silently if the endpoint isn't live yet (e.g. a running service that hasn't restarted onto the new build).

### Notes

- PATCH bump (0.2.1 → 0.2.2) — backend adds `GET /api/update-status`; frontend adds the banner to `index.html`. No change to existing features.

---
## [0.2.1] — 2026-07-10

### Fixed — One-click Update that actually works, and generation installs that don't silently fail

Overhauled the update/install flow. It was tedious and, worse, quietly broken:

- **One Update button, correct in every run mode.** The old "Update & Restart" was hardwired to stop/start `start.js`, but in production this app runs as an always-on launchd **service** — so it stopped nothing and then launched a *second* server that fought the service for the fixed port. The unified `update.js` now detects the mode and restarts the **real** server (kickstart the service **or** start `start.js` — never both), so updating no longer requires manually stopping production first.
- **Generation deps refresh on the same click.** `update.js` used to install only the base deps; heavy ML deps came from a separate "Reinstall Generation" button, so a release that bumped a model dependency silently didn't apply on Update. Update now refreshes generation deps too (when generation is installed) — no second button to hunt for.
- **Install from source, not a drifted lock.** `install_generation.js` (and Update) now install from `requirements-generation.txt`, the authoritative range file. The generation `.lock.txt` had drifted — on some machines it contained only base packages, so "Install Generation" installed nothing while the UI still reported success. Source-first can't have that failure mode.
- **Verify-then-notify.** After installing, the key modules are imported; a failure breaks the run and withholds the "installed" notification. The old script fired "Generation engine installed" unconditionally — even on total failure.
- **"Update & Restart" folded into "Update"** (kept as a back-compat alias that forwards to `update.js`).

### Notes

- PATCH bump (0.2.0 → 0.2.1) — launcher scripts only (`update.js`, `install_generation.js`, `update_and_restart.js`, `pinokio.js`). No app-code change.
- Verified: all launcher scripts load; the menu renders a single mode-aware "Update"; generation deps import in the env.

---
## [0.2.0] — 2026-07-09

### Added — dependency lockfiles: fresh installs are now reproducible forever

`requirements.txt` / `requirements-generation.txt` use version **floors** (`>=`), so a fresh install months from now would resolve to whatever PyPI serves that day — one breaking release in any dependency bricks the app on a new machine while existing installs keep working. Same fix as the other four studios (Chat v1.19.0, Voice v1.8.0, Image v1.18.0, Music v1.4.0).

- **`app/requirements.lock.txt`** — the pinned phase-1 set (36 packages, compiled from the floors constrained to the verified env's installed versions).
- **`app/requirements-generation.lock.txt`** — the full verified env (38 packages at this early stage; regenerate as the generation stack grows).
- `install.js`, `install_generation.js`, and `update.js` now install from the locks. Upgrade flow documented in each lock's header.

Verified: both locks resolve all-satisfied against the live env; all three launcher scripts pass `node --check`; python was already pinned (`python=3.12`).

### Notes

- MINOR bump (0.1.2 → 0.2.0) — install-pipeline change, no package versions changed.

## [0.1.2] — 2026-07-08

### Fixed — Start now refuses to compete with startup service mode

The startup service owns port `47872` when installed, and the service-mode sidebar hides the normal Start button. But `start.js` itself still had no direct guard, so any stale menu, direct script launch, or automation path could still try to start a second Uvicorn server on the same fixed port and fail with "address already in use."

`start.js` now checks for `service/.installed` before launching the server. If service mode is active, it exits immediately with a clear message telling the user to use **Open UI (service)** or uninstall the startup service first. The existing Uvicorn URL capture and `local.set` behavior are unchanged.

**Verified:** `node --check start.js` and direct inspection against the required Pinokio URL-capture pattern (`input.event[1]`). Video Studio's latest logs show the normal Start path only; no service restart was performed.

### Notes

- PATCH bump (0.1.1 → 0.1.2) — launcher guard only, no app/backend change. **Just run Update**.

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
