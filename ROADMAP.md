# Video Studio KH — Cloud Gateway Roadmap

Status tracker for turning Video Studio into a **gateway for cloud video
providers** (fal, kie, replicate…) so a client like Story Studio links once and
gets local **and** cloud models in one live catalog. Full design: [`SPEC.md`](SPEC.md).

---

## ✅ Phase 1 — DONE (shipped in v0.5.0)

- Provider abstraction — `app/backend/providers/base.py` (`VideoProvider`, `CloudVideoModel`).
- **fal.ai adapter** — `providers/fal.py`; submit → poll → download via stdlib `urllib` (no new deps). Curated, hand-editable model list at `providers/fal_models.json`.
- Registry — `providers/registry.py`; dispatch by `provider:` id + catalog-merge helpers.
- Unified `/api/catalog` — local + cloud in one list; cloud entries carry `is_cloud` + `hub_modality:"video"` + `provider` + `cost_tier:"paid-cloud"` so Studio Hub sorts them into its cloud lane automatically.
- Same generation API — `provider:`-prefixed ids route through the existing `/api/generate/txt2video` + `video2video`; clip downloads into `app/output/`, so the job/SSE/`/video` lifecycle is identical to a local render (`cloud_jobs.py`, `video.py:submit_cloud`).
- **Spend guardrails** — `spend.py`: per-provider **and** global daily/monthly USD caps (calendar reset), pre-submit gate blocks before billing, cost records in `spend.db`.
- Endpoints — `/api/providers`, `/api/providers/{key}/{key,paid,refresh}`, `/api/spend`, `/api/spend/caps`.
- Settings UI — link key, paid toggle, caps, live spend; cloud models render with provider/new/deprecated badges and work without the local engine.
- Tests — `app/tests/test_cloud.py` (8, passing) + full app-boot check.

## ⚠️ Do first in the next session — live verification

Phase 1 is built + unit-tested but **not run against real fal** (needs a key + credit):

1. Update + restart → Settings → paste fal key → enable paid → set caps → generate.
2. **Verify/correct fal model paths + prices** in `providers/fal_models.json` against <https://fal.ai/models> (curated; some are best-guess).
3. If a model's result JSON differs, adjust `_extract_video_url()` in `providers/fal.py`.

## 🔜 Phase 2 — breadth + freshness

- **kie + replicate** adapters (same `VideoProvider` interface, ~1 file each).
- **Live model-listing** with a TTL cache — new module `catalog_sync.py`; augments/replaces the curated JSON where the provider has a models API.
- **Deprecation flow** — diff on refresh → mark `new` / `deprecated`; keep deprecated visible **30 days** then hide (SPEC §6). Backend fields (`status`, `deprecated_at`) already exist; wire the diff + background refresh.
- Persist in-flight cloud jobs so pollers **re-attach after a restart** (today an in-flight cloud job is lost on restart).

## 🎨 Phase 3 — polish

- Show the per-generation **cost estimate in the Generate tab** before submit (`estimate_usd` is already returned).
- Spend **history charts** in Settings.
- **Capability / duration / resolution filters** in the model picker.
- Reconcile **actual vs. estimated** cost (today actual = estimate).

## 🤝 Companion — Studio Hub (separate session)

Cloud models reach the Hub for free via `/api/catalog`. Hub-side work (its own session): Models-tab **local vs cloud lane** grouping + provider/deprecated badges. The Hub ledger already gained `is_cloud` + a `local`/`cloud` lane split. Nothing here depends on it.
