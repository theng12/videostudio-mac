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

## ✅ Live fal verification — DONE (2026-07-14)

Real fal queue submit → poll → result download completed with
`fal:fal-ai/ltx-video-13b-distilled`. The gateway produced a valid 5.04-second
MP4, served it through the normal `/video` endpoint, and reconciled the $0.04
estimate in the spend ledger.

## ✅ Phase 2 — DONE (v0.6.0)

- **Kie.ai + Replicate adapters** use the same `VideoProvider` lifecycle as fal.
- **Live model listing + persistent TTL cache** augments curated Replicate entries
  from its provider collection; failed refreshes retain the last good snapshot.
- **Deprecation flow** diffs refreshes, marks models `new` / `deprecated`, keeps
  deprecated entries visible for **30 days**, then hides them.
- **Self-healing recovery** persists provider task IDs immediately after submit,
  retries polling/result downloads with bounded backoff instead of failing on a
  local timeout, and re-attaches stopped pollers via startup + watchdog repair.
  A manual repair endpoint/UI action always targets the saved task and can never
  submit a duplicate cloud generation.
  Submission intent is persisted before the paid request; an ambiguous submit
  response blocks new jobs for that provider instead of risking a duplicate.
- **Billing safety** now enforces the paid toggle server-side and blocks models
  whose price cannot be verified.

## ✅ Phase 3 — DONE (v0.6.0)

- The Generate tab shows a live **provider cost estimate before submit**, with
  paid/key/verified-price readiness and cap enforcement kept as hard gates.
- Settings includes a **14-day spend chart** with per-provider breakdowns.
- The generation model picker has **capability, minimum-duration, and resolution
  filters**, plus cloud-native duration/resolution/aspect controls.
- Per-second jobs reconcile the ledger from the downloaded MP4's actual duration;
  fixed per-video prices remain exact. The estimate is booked as soon as the
  provider completes so a temporary result-download failure never releases spent
  credits from the guardrail.

## 🤝 Companion — Studio Hub (separate session)

Cloud models reach the Hub for free via `/api/catalog`. Hub-side work (its own session): Models-tab **local vs cloud lane** grouping + provider/deprecated badges. The Hub ledger already gained `is_cloud` + a `local`/`cloud` lane split. Nothing here depends on it.
