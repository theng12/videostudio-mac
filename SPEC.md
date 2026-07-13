# Video Studio KH — SPEC: Cloud Provider Gateway

Spec for the **v0.5 expansion**: turn Video Studio from a local-only text/image/
video-to-video generator into a **gateway for video generation providers**
(fal, kie, replicate, …) that exposes a single, live-updated catalog and one
generation API — so a client like **Story Studio** links to Video Studio *once*
and gets every local **and** cloud video model, kept current as providers add
and deprecate models.

> This document specifies the new cloud layer. The existing local engine
> (PyTorch/MPS + Diffusers) is unchanged and keeps working exactly as today.

---

## 1. Why this exists

Story Studio currently has to add/remove individual video models by hand. That
doesn't scale: cloud providers change their lineup constantly. Instead:

- **Video Studio becomes the one video endpoint.** It aggregates the local
  engine **plus** any linked cloud providers behind its existing API surface.
- **Story Studio connects once** (1 LAN/API connection, directly or via the
  Hub) and reads a unified `/api/catalog`. It never manages provider models
  again — the gateway does, live.
- **Local and cloud become peers.** Same catalog, same generation API, same job
  lifecycle. The client can't tell them apart (and shouldn't have to).

### The zero-upstream-change insight

The Hub already routes video generically:

```
studiohub broker.py:  "video": ("/api/generate/txt2video", "prompt", "video")
```

It reads a studio's `/api/catalog` for models and POSTs jobs to
`/api/generate/txt2video`. **Therefore, if Video Studio's catalog includes cloud
models and its generate endpoint routes them to the right provider, Story Studio
and the Hub need no changes to *generate*.** The only Hub-side change is
presentational: sorting the unified catalog by local vs cloud more clearly
(§10 + the companion Hub prompt).

---

## 2. Non-goals (for now)

- Not building our own model hosting — we proxy providers.
- Not unifying provider parameter schemas — params are **opaque pass-through**
  per model (mirrors the Hub's decided policy for job params).
- Not receiving provider webhooks — behind a LAN we **poll** (see §5).
- Not touching the local generation engine's behavior.

---

## 3. Architecture

```
Story Studio ──1 LAN/API conn──▶  Video Studio (the gateway)
   reads ONE /api/catalog          ├── local engine (PyTorch/MPS + Diffusers)   [unchanged]
   POSTs ONE /api/generate/txt2video│
   polls   ONE /api/generate/jobs  └── cloud layer
                                        ├── ProviderRegistry (linked + keys)
                                        ├── fal adapter ─┐
                                        ├── kie adapter  ├─ live model lists
                                        ├── replicate    ─┘  (Seedream/Kling/Veo/…)
                                        ├── CatalogSync   (TTL refresh + deprecation)
                                        └── SpendLedger   (caps + records)
```

New backend modules (peers of the existing `video.py` / `downloads.py`):

| Module | Responsibility |
|---|---|
| `providers/base.py` | `VideoProvider` ABC + shared dataclasses (`CloudVideoModel`, `SubmitResult`, `JobStatus`) |
| `providers/fal.py` | fal adapter (Phase 1) |
| `providers/registry.py` | which providers are linked, key resolution, dispatch by model id |
| `catalog_sync.py` | TTL-cached live model listing + new/deprecated diffing |
| `spend.py` | SpendLedger: caps, pre-submit gate, estimate/actual records (SQLite) |
| `cloud_jobs.py` | cloud job lifecycle: submit → poll → download → ledger (reuses the existing job manager) |

---

## 4. Provider abstraction

Video providers are heterogeneous async job queues (not OpenAI-compatible like
Chat's LLM providers), so each is a thin adapter behind one interface:

```python
class VideoProvider(ABC):
    key: str                       # "fal" — used in model ids + settings
    name: str                      # "fal.ai"
    docs_url: str

    def has_key(self) -> bool: ...
    def list_models(self) -> list[CloudVideoModel]: ...      # live where supported; else curated
    def estimate_cost(self, model, params) -> float | None:  # USD, for the pre-submit gate
    def submit(self, model, params) -> SubmitResult:         # returns provider_job_id
    def poll(self, provider_job_id) -> JobStatus:            # queued|running|done|error (+result_url)
    def cancel(self, provider_job_id) -> bool: ...
```

```python
@dataclass(frozen=True)
class CloudVideoModel:
    id: str                    # gateway id, e.g. "fal:fal-ai/kling-video/v2/master"
    provider: str              # "fal"
    label: str
    capabilities: tuple[str]   # ("txt2video","img2video", …) — MULTI-PURPOSE (§9)
    max_duration_s: float | None
    resolutions: tuple[str, ...]      # ("720p","1080p")
    aspect_ratios: tuple[str, ...]    # ("16:9","9:16","1:1")
    price: dict | None         # {"unit":"per_second"|"per_video","usd":0.05}
    status: str = "available"  # available | new | deprecated
    first_seen: float = 0.0    # unix ts (for the "new" badge)
    deprecated_at: float | None = None   # unix ts (for the 30-day grace, §6)
```

### 4.1 Model id scheme

`"<provider>:<provider_model_path>"` — e.g. `fal:fal-ai/kling-video/v2/master`.
- Stable across catalog refreshes (keyed on the provider's own model path).
- The `<provider>:` prefix is how routing (§5) tells cloud from local; local
  models keep their bare Hugging Face repo id (no prefix).
- The Hub keys models by `repo`, so this id **is** the `repo` for cloud entries.

---

## 5. Generation routing & job lifecycle parity

No new generation endpoints. `/api/generate/txt2video` and `/api/generate/video2video`
inspect the model id:

- **bare HF repo** → existing local path (unchanged).
- **`<provider>:…`** → cloud path in `cloud_jobs.py`:
  1. **Guardrail check** (§7): estimate cost; if it would exceed a cap, reject
     *before* submitting (HTTP 402, clear message). Also require the provider's
     paid toggle to be on.
  2. **Submit** to the provider adapter → get `provider_job_id`.
  3. **Register a job in the existing job manager** so `/api/generate/jobs`,
     `/jobs/{id}`, the SSE `/stream`, and `/jobs/{id}/video` all work unchanged.
  4. A background **poller** polls the provider (interval ~3–5 s) until done.
  5. **Download** the result mp4 into `app/output/` — so it is served and
     ledgered exactly like a local clip (the Hub's scanner + `artifact_url`
     keep working).
  6. Mark the job done; record **actual** cost in the SpendLedger.

**Result:** Story Studio and the Hub see the identical job lifecycle. A cloud
generation is indistinguishable from a local one except it's faster to start and
carries a `provider` + cost.

Polling (not webhooks) is deliberate — Video Studio sits behind a LAN with no
public callback URL. Poll cadence and a hard timeout per job are configurable.

---

## 6. Live catalog sync & deprecation

`catalog_sync.py` refreshes each linked provider's model list on a TTL (default
~30 min; also on demand). For each provider it diffs the fresh list against the
last-known snapshot (persisted):

- **new** — appeared this refresh → `status:"new"`, `first_seen=now`. The "new"
  badge clears after N days (default 14).
- **available** — present as normal.
- **deprecated** — was known, now missing from the provider (or flagged EOL) →
  `status:"deprecated"`, `deprecated_at=now`. **Kept visible with a
  "deprecated" badge for 30 days**, then hidden. Not deleted immediately,
  because Story Studio may still hold a queued job referencing it. A submit
  against a deprecated model still works during the grace window (with a
  warning) so in-flight work doesn't break.

Live where the provider has a clean model-list API (fal, replicate); a curated
fallback list (checked into the adapter) covers providers/models without one
(kie may need this). Curated entries are merged and also age via the same
new/available/deprecated states, edited by hand.

---

## 7. Spend guardrails — **both caps, calendar reset, with records**

Cloud video is real money per generation, so guardrails are first-class.
`spend.py` owns a small SQLite store (`spend.db`, gitignored) at the launcher
root — Video Studio has no ledger DB of its own (that lives in the Hub).

### 7.1 Caps

Two caps, enforced together (the tighter one wins):

- **Per-provider caps** — a daily and a monthly USD limit *per provider*.
- **Global caps** — a daily and a monthly USD limit *across all providers*.

Resets are **calendar-based** (local timezone): the daily window resets at local
midnight; the monthly window resets on the 1st. Every window's total is
**recorded** so history is queryable (spend per day/month/provider/model).

### 7.2 Records

`spend.db` schema:

```sql
CREATE TABLE spend (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,               -- submit time
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  job_id TEXT,                    -- links to the generation job
  est_usd REAL,                   -- estimate at submit
  actual_usd REAL,                -- filled on completion (nullable)
  duration_s REAL,                -- output length billed
  state TEXT NOT NULL             -- submitted | done | error | refunded
);
```

### 7.3 Pre-submit gate

Before any cloud submit:
1. `est = provider.estimate_cost(model, params)` (price × requested duration,
   or per-video price).
2. Compute the projected total for each relevant window (this provider's
   day+month, and global day+month).
3. If **any** window would exceed its cap → **block** with HTTP 402 and a
   message naming the cap hit and the current/limit amounts. No money spent.
4. Otherwise record a `submitted` row (`est_usd`), submit, and on completion
   patch `actual_usd`/`state`.

Where a provider only reports actual cost after completion, the estimate governs
the gate and actuals reconcile the record (and can trip the cap for the *next*
job).

### 7.4 UI

Settings shows, per provider and global: **today / this month** spend vs cap,
a small history, and the caps as editable fields. A job that's blocked surfaces
the reason inline in the Generate tab.

---

## 8. Auth / API keys

- One API key per provider, from **Settings** (persisted) with an **env
  override** (`VIDEOSTUDIO_FAL_KEY`, …), mirroring Chat's `env_var` pattern.
- **Owner-only, never returned** by any endpoint (only a masked/`set: true`
  indicator), same discipline as the HF token.
- A provider with no key is shown as "link to enable"; its models are listed
  (so the user sees what's available) but generation is gated until a key +
  the paid toggle are set.

---

## 9. Capabilities — multi-purpose

Cloud models are **multi-purpose**: a model may support `txt2video` *and*
`img2video` (and occasionally `video2video`). The catalog entry lists all
supported modes in `capabilities`.

- **No strict rejection at submit.** The gateway passes the request through in
  the requested mode; if the model/provider can't do it, the provider's error
  is surfaced verbatim. (Story Studio is responsible for sending a mode the
  chosen model supports — e.g. it will only send `img2video` where intended.)
- Params beyond mode (resolution, duration, aspect, seed, etc.) are **opaque
  pass-through** to the provider, per the ecosystem's job-param policy.

---

## 10. Hub integration (unified catalog, sorted local vs cloud)

Cloud video models flow to the Hub **for free**, because the Hub already polls
each studio's `/api/catalog`. To slot into the Hub's existing cloud machinery,
each cloud catalog entry Video Studio serializes MUST carry:

| field | value |
|---|---|
| `repo` | the gateway id, e.g. `fal:fal-ai/kling-video/v2/master` |
| `is_cloud` | `true`  (the Hub already filters on this — `/api/hub/catalog?cloud=true`) |
| `hub_modality` | `"video"` |
| `provider` | `"fal"` |
| `cost_tier` | `"paid-cloud"` (aligns with the Hub's local / free-cloud / paid-cloud tiering) |
| `size_gb` | `0` (no download — matches the existing cloud-model pattern) |
| `status` | `available \| new \| deprecated` |
| `capabilities`, `price`, `label` | as above |

Local models keep `is_cloud: false` (or absent) and their real `size_gb`.

**Hub-side work (separate session — prompt provided below):** the Hub already
has `is_cloud`, an `/api/hub/catalog?cloud=` filter, and a `.pill.cloud` style,
but its **Models tab should sort/group local vs cloud clearly** (a lane split or
grouping + provider badge + deprecated badge), and its stats/aggregation should
keep the two lanes visually distinct. Video Studio's job is only to emit the
fields above correctly; the Hub owns the presentation.

---

## 11. API surface (Video Studio)

New/changed endpoints (all under the existing auth):

```
GET  /api/catalog                      → now merges local + cloud models (unified)
GET  /api/providers                    → linked providers, key-set state, paid toggle, caps, spend
POST /api/providers/{key}/key          → set/clear a provider API key (owner-only)
POST /api/providers/{key}/paid         → toggle paid-model use for a provider
POST /api/providers/{key}/refresh      → force a live catalog resync for a provider
GET  /api/spend                        → today/month totals vs caps, per provider + global, history
POST /api/spend/caps                   → set per-provider and global daily/monthly caps
POST /api/generate/txt2video           → unchanged contract; routes local vs cloud by model id
POST /api/generate/video2video         → unchanged contract; routes local vs cloud by model id
```

Unchanged and reused as-is: `/api/generate/jobs`, `/jobs/{id}`, `/jobs/{id}/video`,
`/jobs/{id}/cancel`, `/api/generate/stream`, `/api/capabilities`, `/api/health`.

---

## 12. Phased roadmap

- **Phase 1 — prove it end to end (fal only).**
  Provider ABC + fal adapter (submit/poll/download + list_models or curated
  fallback), `provider:` routing in the existing generate endpoint, cloud job
  lifecycle, merge into `/api/catalog` with the Hub fields (§10), SpendLedger
  with both caps + calendar reset + pre-submit gate, and Settings UI for the
  fal key + caps + spend. Verify a real fal generation flows through Story
  Studio unchanged.
- **Phase 2 — breadth + freshness.**
  kie + replicate adapters, `catalog_sync` live-listing + new/deprecated
  diffing with the 30-day grace, per-provider refresh UI.
- **Phase 3 — polish.**
  Richer cost estimates, spend history charts, capability filters in the
  Generate picker, retry/backoff tuning per provider.

---

## 13. Open items / risks

- **fal API verification is implementation step 1.** The exact current submit/
  poll URL shape, auth header, and whether fal exposes a clean programmatic
  model-list endpoint (vs. needing a curated list) must be confirmed against
  fal's live docs before coding the adapter. The abstraction (§4) is designed so
  a curated fallback works if live-listing isn't clean.
- **Estimate accuracy.** Some providers only price per-video or only report
  actual after completion; the gate leans on estimates and reconciles actuals,
  which can let a single job slightly overshoot before the next is blocked.
- **Clock/timezone** for calendar resets uses the host's local time; document it.
- **Result retention.** Downloaded cloud clips live in `app/output/` like local
  ones — same disk-usage considerations; reuse any existing cleanup.

---

## 14. Decisions (locked)

1. First provider: **fal.ai**.
2. Spend caps: **both** per-provider **and** global; **calendar** day/month
   resets; **records** kept.
3. Deprecated models: **kept visible with a badge for 30 days**, then hidden.
4. Hub: cloud models **sent to the Hub's unified catalog**; the Hub sorts local
   vs cloud more clearly (companion Hub session).
5. Capabilities: **multi-purpose** (t2v + i2v); no strict submit-time rejection —
   Story Studio sends the correct mode.
6. Gateway placement: **in Video Studio**.
