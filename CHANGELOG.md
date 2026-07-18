# Changelog — Video Studio KH

All notable changes to Video Studio KH are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change. Re-install required.
- **MINOR** (1.1.x → 1.2.x) — new engine / new feature / new model family. **Re-run "Install Generation"** to pick up new Python deps.
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak / catalog entry within an existing family. **Just run Update** from the Pinokio sidebar.

---

## [0.8.1] — 2026-07-18

### Fixed — automatic-update settings stay stable while editing

- Kept unsaved mode, frequency, and maintenance-time choices separate from
  live updater status. The five-second poll no longer restores old values while
  a user is configuring Automatic or Notify only.
- Replaced the cramped native controls with consistent mode cards, styled
  scheduling controls, one primary save action, and contextual update actions.
- Corrected the success-state styles to use Video Studio's existing `--good`
  color token; the copied panel referenced an undefined `--ok` token.

### Verification

- Verified draft persistence across repeated status polls, responsive layout,
  JavaScript syntax, and the full test suite. Rendering, cloud jobs, the updater,
  scheduler, launcher, and dependency manifests were deliberately unchanged.

---

## [0.8.0] — 2026-07-15

### Added — safe optional automatic updates

- Added Off, Notify only, and Automatic modes in Settings, with daily/weekly
  schedules, status, manual checks, retry, and “Update after current work.”
- Updates defer while any local or paid cloud video generation is queued or
  running, or while a model download is active. The updater verifies the fixed
  remote, clean `main`, fast-forward history, disk, dependencies, imports,
  service health, and the exact running version.
- Added a short-lived launchd scheduler, lock protection, retry/backoff,
  rotating redacted logs, notifications, restart recovery, and bounded rollback.
  The feature is Off by default.

### Verification

- Added focused updater/readiness tests and verified schedule lifecycle, APIs,
  launchers, dependency integrity, the catalog truth audit, and responsive UI.

## [0.7.0] — 2026-07-15

### Production audit — catalog truth, safe media, and recoverable local jobs

- Consolidated LTX to the current official `Lightricks/LTX-Video-0.9.8-13B-distilled`
  Diffusers release and wired its real `LTXConditionPipeline`. Removed the misleading
  254 GB multi-checkpoint `Lightricks/LTX-Video` row and superseded 0.9.7 row; their
  existing cache folders are not deleted. Canonicalized CogVideoX to `zai-org` while
  preserving transparent use of legacy `THUDM` caches.
- Retained Wan 2.2 TI2V/A14B, HunyuanVideo T2V/I2V, and CogVideoX 2B/5B/I2V because
  every visible mode maps to an installed Diffusers pipeline. Catalog details now show
  measured download size, minimum/recommended memory, speed tier, frame/duration and
  resolution limits, license links, and commercial-use restrictions. CogVideoX-2B is
  now honestly presented as the lowest-memory local tier; none of the local models is
  claimed to be MLX-native or comfortable on a 16 GB Mac.
- Rebuilt the incomplete generation lock so it includes the full verified stack. Base
  and generation requirements now use exact proven pins; Install, Install Generation,
  and Update consume the locks and gate success on imports, required pipeline classes,
  and `pip check`. Updated `setuptools` to 83.0.0 to resolve PYSEC-2026-3447.
- Local jobs now enter a strict oldest-first queue with one heavyweight render at a
  time, visible queue positions, and distinct preparing/loading/generating/encoding/
  cancelling stages. Active local jobs are persisted; a restart converts interrupted
  work to a useful recoverable history error instead of silently losing it. Pipelines
  and MPS caches are released after success, cancellation, and failure.
- Fixed unsafe history deletion and playback so stored paths can never escape managed
  output/upload folders. Uploaded images are decoded with Pillow; videos are inspected
  with structured ffprobe limits. Cloud result URLs must be public HTTPS, downloads are
  capped at 2 GB, partial files are cleaned, and public/persisted job parameters no
  longer include inline media or provider-private parameters.
- Added disk preflight, resolution/frame validation, structured post-encode integrity
  checks, and durable codec/resolution/FPS/frame/duration/size metadata. History can
  restore every generation setting; source media must be selected again by design.
- Modernized Generate with an at-a-glance model/mode/format/frame/seed strip, collapsed
  advanced controls, truthful model-aware dimensions, queue stages, friendly errors
  with collapsed technical details, stable non-autoplay previews, and responsive output
  history even when no model is currently downloaded.

### Verification

- 35 automated tests pass, including new regressions for LTX dispatch, canonical cache
  aliases, private job data, managed deletion, restart recovery, decoded uploads, and
  cloud-result SSRF protection. Python compilation, JavaScript syntax, lock integrity,
  all 9 catalog rows, all 4 engine families, `pip check`, `pip-audit`, FFmpeg/ffprobe,
  Git whitespace, and desktop/390 px browser checks pass.
- The existing real fal output was re-inspected: H.264/yuv420p, 1280×704, 24 FPS,
  121 frames, 5.0417 seconds, playable in the browser with no console errors. It is
  nonblank and changes from first to last frame, though its generated motion is subtle.
- No local model generation was possible: this machine has no cached video weights,
  only 16 GB unified memory, and its saved Hugging Face token currently fails validation.
  No multi-gigabyte download or additional paid cloud job was started during the audit.
- First/last-frame conditioning, camera/reference control, LoRA, audio-driven video,
  upscaling, and interpolation remain absent because no audited local worker implements
  them yet; the UI does not advertise those controls.
- The sole test warning is FastAPI's upstream `TestClient` compatibility shim warning
  that the current `httpx` bridge will move to `httpx2`; it does not affect production.

## [0.6.0] — 2026-07-14

### Added — Kie/Replicate providers, fresh catalogs, and restart-safe cloud jobs

- Added Kie.ai and Replicate adapters behind the existing cloud generation API.
  Replicate's curated models are augmented from its live text-to-video collection.
- Added a persistent 30-minute catalog cache with new/deprecated diffs. Removed
  models remain visible for a 30-day migration window; a failed refresh keeps the
  last known-good catalog.
- Provider task IDs now persist privately immediately after submission. Local
  timeouts and temporary poll/result-download failures use bounded backoff and
  keep checking the original task indefinitely instead of spending credits on a
  replacement. Startup recovery plus a watchdog automatically repairs stopped
  pollers; the Outputs UI also offers **repair saved task** for manual recovery.
- Submission intent is persisted before the paid API call. If that first response
  is lost before a task ID returns, the outcome is marked unknown and additional
  paid submissions to that provider are blocked instead of risking duplicate use.
- Recovery history writes are serialized so concurrent cloud pollers cannot
  overwrite another job's saved provider task.
- Paid-use consent is now enforced by the backend, not only shown in Settings.
  Models without a verified cost remain visible but cannot start a billable job.
- Fixed silent cloud-key saves in Settings: the provider rows initialize safely,
  Save shows progress/success/errors, and failed toggles restore server state.
- Finished the cloud gateway polish pass: the Generate tab now previews the
  provider charge, offers capability/duration/resolution model filters and
  cloud-native duration/resolution/aspect controls, while Settings plots a
  14-day spend history with provider breakdowns.
- Per-second jobs now reconcile estimated spend against the downloaded MP4's
  actual duration. Fixed per-video pricing remains exact, and provider-complete
  jobs stay booked while a result download is recovering.
- Fixed cloud Generate readiness being incorrectly blocked by the local video
  engine check; cloud models now use their own key/paid/verified-price gates.

### Verified

- Real fal end-to-end generation completed: queue submit, polling, valid 5.04 s
  MP4 download/serving, and $0.04 spend reconciliation.
- Live app catalog reports fal, Kie, and Replicate models. Full suite: 28 tests
  covering adapters, catalog diffs/grace, billing gates, durable task IDs,
  no-resubmit timeout recovery, watchdog healing, and restart persistence.

No new dependencies; run **Update** and restart.

## [0.5.0] — 2026-07-13

### Added — cloud video provider gateway (fal.ai) with spend guardrails

Video Studio can now act as a **gateway for cloud video generators**, so a client like Story Studio links to it once and gets local **and** cloud models in one live catalog. See `SPEC.md`.

- **fal.ai provider** — curated fal video models (Kling, Hailuo, Veo 3, Seedance, LTX) appear in `/api/catalog` alongside local ones, each tagged `is_cloud` + `hub_modality:video` + `provider` + `cost_tier:paid-cloud` so Studio Hub sorts them into its cloud lane automatically. The model list is hand-editable at `app/backend/providers/fal_models.json`.
- **Same generation API** — a `provider:`-prefixed model id (e.g. `fal:fal-ai/kling-video/v2/master/...`) routes through the existing `/api/generate/txt2video` + `video2video`; the gateway submits to the provider, polls, and downloads the clip into `app/output/`, so the job/SSE/`/video` lifecycle is identical to a local render. Local generation is untouched.
- **Spend guardrails (real money)** — per-provider **and** global daily/monthly USD caps (calendar reset), enforced together with a pre-submit gate that blocks a generation before it bills. Every cloud job's cost is recorded in `spend.db`. New Settings UI to link keys, toggle paid, set caps, and watch live spend.
- **New endpoints:** `/api/providers`, `/api/providers/{key}/{key,paid,refresh}`, `/api/spend`, `/api/spend/caps`.

No new Python dependencies (cloud HTTP uses the stdlib), so **just run Update** and restart — no reinstall. Verified: 8 new tests (routing, catalog-merge shape, spend caps/booking/reset) plus a full app-boot check. A live fal generation needs your own fal key + credit to confirm end to end.

## [0.4.1] — 2026-07-13

### Fixed — saved fleet credentials apply without restarting Video Studio

- Protected requests now verify against the current owner-only fleet-token file instead of a startup snapshot. Studio Hub credential saves and rotations take effect immediately, and authenticated browser cookies follow the current value.

Verified with a live-rotation middleware regression test plus the full test suite. No launcher, engine, or dependency changes; **Just run Update**.

## [0.4.0] — 2026-07-12

### Added — secure fleet access and capability contract

- Remote API, uploaded media, and generated-video access now requires the automatically shared StudioHub fleet token; loopback Pinokio use remains passwordless.
- Browser writes are same-origin protected, authenticated browser sessions use an HttpOnly cookie, and remote Studio pages prompt once per tab when a token is needed.
- Added normalized `GET /api/capabilities` metadata for text-to-video, image-to-video, and video-to-video operation preflight.

### Verification

- Python and JavaScript syntax checks pass. Security-contract tests cover public health/capability routes, protected catalog access, accepted fleet credentials, cross-origin write rejection, and private token permissions.

## [0.3.1] — 2026-07-12

### Fixed — honest pipeline readiness and safer media uploads

- Diagnostics now verifies every required Diffusers pipeline class, not merely that
  the package imports. The UI and API reject models whose exact text/image/video
  pipeline is missing and point users to Update/Reinstall Generation.
- The Generate button now requires a selected ready model, prompt, and source media
  when appropriate, with a specific inline explanation for the missing input.
- The header version badge now keeps the `/api/health` payload instead of reducing it
  to `{ok}`, fixing the visible `vundefined` label. The empty output-folder action also
  sends a normal toast string instead of rendering `[object Object]`.
- Uploads stream to disk instead of being read entirely into memory. Images are limited
  to 20 MB, videos to 500 MB, and unsupported filename types are rejected rather than
  silently relabeled. The frontend enforces and explains the same limits.
- Video parameters now have explicit dimensions, frame, FPS, step, guidance, strength,
  seed, and prompt bounds. Failed jobs remove partial MP4 files.

### Security

- Hugging Face token storage is forced to owner-only (`0600`) permissions.
- Remote update-version metadata is rendered with `textContent`.
- The installed Python environment was audited and has no known dependency
  vulnerabilities. LAN bind/CORS remain unchanged for server-mode compatibility.

### Verification

- Python/JavaScript/HTML checks, request-boundary and streaming-upload tests, pipeline
  coverage checks, dependency audit, and a stopped-app browser smoke test all pass.

## [0.3.0] — 2026-07-10

### Added — Clip management: per-clip reveal/delete, disk management, richer progress

Carries the Voice Studio generator improvements to Video Studio, adapted to its clip model (frontend live on reload; the new endpoints activate after one **Update** — no new Python deps):

- **Per-clip actions** — each finished clip now has **📂 reveal** (show the .mp4 in Finder) and **🗑 delete** (two-click; removes the clip from history and deletes the file). *(Backend: `DELETE /api/generate/history/{id}`.)*
- **Disk management** — a footer shows how many clips and how much disk the outputs use, with one-click prune ("keep newest 50" / "delete > 30 days"). High-value here since video files are large. *(Backend: `GET /api/output/stats`, `POST /api/output/prune`.)*
- **Richer progress** — the bar now moves off zero the moment a job starts, and the label reads % · step X/Y · elapsed using the real progress fields.
- **Friendlier empty state** naming the new player/download/reveal/delete affordances.

### Notes
- MINOR bump (0.2.8 → 0.3.0). Frontend is live on reload; the endpoints + progress kick need one **Update** (restart) — the UI degrades gracefully until then (disk footer hides; delete/prune show a "run Update" hint).

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
