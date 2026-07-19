# Video Studio KH

Local **text-to-video** and **video-to-video** generation for Apple Silicon,
powered by native MLX plus PyTorch (MPS) + 🤗 Diffusers. A 1-click Pinokio
launcher with a model catalog, download manager, and a generation UI with an
in-browser video player.

Part of the **KH studio suite** (Chat / Image / Music / Voice / **Video**) — each
a standalone single-modality launcher.

> **Heads-up on performance.** Local video generation is heavy and slow on
> Apple Silicon, especially for the larger models (Wan 2.2 A14B, HunyuanVideo).
> On a 16 GB or 24 GB Mac, start with **Lance 3B Video MLX** at its 512×512,
> 17-frame default. It automatically uses low-memory relay mode on 16 GB and a
> parallel loading on 24 GB+. Reserve the larger Diffusers models for
> a high-memory Mac (e.g. an M3 Ultra Mac Studio).

---

## What it does

- **Text → Video** — generate a clip from a prompt.
- **Image → Video** — animate a still image (first-frame / image-to-video).
- **Video → Video** — restyle an existing clip guided by a prompt (CogVideoX).
- **Model catalog + downloads** — browse, download, and manage Hugging Face
  weights with live progress; downloads are independent of generation.
- **Clear local/cloud lanes** — the library and Generate selector show where
  each model runs, which provider it uses, and whether an API key or paid-use
  permission is still needed.
- **In-browser player** — generated `.mp4` clips play inline; download or reveal
  in Finder.

### Seed model catalog

| Family | Models | Modes |
|---|---|---|
| **Lance MLX** | `mlx-community/Lance-3B-Video-bf16` | **t2v** |
| **LTX-Video** | `Lightricks/LTX-Video-0.9.8-13B-distilled` | t2v, i2v |
| **Wan 2.2** | `Wan-AI/Wan2.2-TI2V-5B-Diffusers`, `…T2V-A14B…`, `…I2V-A14B…` | t2v, i2v |
| **HunyuanVideo** | `hunyuanvideo-community/HunyuanVideo`, `…-I2V` | t2v, i2v |
| **CogVideoX** | `zai-org/CogVideoX-2b`, `…-5b`, `…-5b-I2V` | t2v, i2v, **v2v** |

---

## How to use (Pinokio)

1. **Install** — installs a Python env and the lightweight server (catalog +
   downloads only; fast).
2. **Install Generation** — installs the locked native MLX and
   PyTorch/Diffusers engines. Run this once before generating.
3. **Start** — launches the server and opens the Web UI.
4. In the **Models** tab, download **Lance 3B Video MLX** for a 16 GB or 24 GB
   Mac. Its weights are about 15.6 GB, so leave enough free disk space.
5. In the **Generate** tab, pick the model, choose a mode, write a prompt (and
   upload an image/clip for i2v/v2v), then **Generate video**.

Gated repos need a Hugging Face token — paste it in **Settings**.

### Lance MLX memory profiles

- **16 GB:** automatic `relay` mode loads one heavy phase at a time. Keep the
  safe 512×512, 17-frame default and close other memory-heavy apps.
- **24 GB+:** automatic `parallel` mode keeps all model phases resident during
  the render. Video Studio still defaults to the same conservative clip profile;
  you can raise frames up to 25.
- The app enforces the upstream validated ≤25-frame / ≤16,128-latent-token
  quality envelope. It does not list speculative MLX conversions that lack a
  demonstrated end-to-end low-memory runtime.

See the [Lance MLX source and measured memory modes](https://github.com/xocialize/lance-mlx)
and the [Video bf16 model card](https://huggingface.co/mlx-community/Lance-3B-Video-bf16).

**Install Generation / Reinstall Generation** stays in the Pinokio sidebar
whether Video Studio is stopped, running through **Start**, or running as the
startup service. Click it directly; the installer safely refreshes the locked
engine, verifies it, and restarts the active server mode for you. During another
install, update, or reset, it is temporarily hidden to prevent concurrent
changes to the same environment.

**What's New** is always available in the sidebar, including while installation
or updating is in progress. It opens Pinokio's release-notes viewer at the
latest version while keeping the full history available below it.

The server binds `0.0.0.0:47872` so other devices on your LAN / Tailscale can
reach the API directly. On this machine it's also at `http://localhost:47872`.

### Run as an always-on service (optional)

Use **Install as Startup Service** in the sidebar to run the server via a macOS
launchd agent that starts at login and self-heals (KeepAlive + a health
watchdog). Use the service **or** Pinokio's Start — not both (they share the
port).

---

## Optional automatic updates

Settings includes a safe automatic updater that defaults to **Off**. Notify and
Automatic modes can run daily or weekly, always waiting for local generations,
paid cloud jobs, and model downloads to finish. “Update after current work”
retries until Video Studio is idle. Every update verifies the expected GitHub
repository, clean fast-forward history, disk, dependencies, imports, health,
and the running version; failed post-update verification triggers rollback.

Monitor it through `GET /api/auto-update/status` and
`GET /api/auto-update/readiness`. Settings, checks, updates, and retry use POST
endpoints under `/api/auto-update/`. Logs are in `logs/auto_update/`; switching
Off unloads the schedule immediately.

## Local output retention

Completed MP4s are temporary local backups. Automatic cleanup is enabled by
default, keeps them for three days, and enforces an 80 GB hard cap by deleting
the oldest completed clips first. Active jobs, models, source uploads,
credentials, provider state, and settings are excluded.

```text
GET  /api/storage-policy
PUT  /api/storage-policy          # { enabled, retention_days, max_gb }
POST /api/storage-policy/cleanup  # optional { target_bytes }
```

## API

Base URL: `http://localhost:47872`. Interactive docs at `/docs` (Swagger) and
`/redoc`. The engine must be installed (**Install Generation**) and the model
fully downloaded before a generation job will run.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness |
| `GET` | `/api/catalog` | models + families + cache state |
| `POST` | `/api/downloads` | start a model download (`{ "repo": "..." }`) |
| `GET` | `/api/downloads/stream` | SSE download progress |
| `GET` | `/api/generate/diagnostics` | engine + package readiness |
| `POST` | `/api/generate/txt2video` | start a text-to-video job (JSON) |
| `POST` | `/api/generate/video2video` | start an image-to-video or video-to-video job (multipart) |
| `GET` | `/api/generate/jobs` | list jobs |
| `GET` | `/api/generate/jobs/{id}` | poll one job |
| `POST` | `/api/generate/jobs/{id}/repair` | re-attach a saved cloud provider task without resubmitting |
| `GET` | `/api/generate/jobs/{id}/video` | fetch the rendered mp4 |
| `DELETE` | `/api/generate/jobs/{id}` | cancel a job |
| `GET` | `/api/generate/stream` | SSE job progress |

A job (`/api/generate/jobs/{id}`) looks like:

```json
{
  "job": {
    "id": "a1b2c3d4e5f6",
    "mode": "txt2video",
    "state": "done",
    "progress": 1.0,
    "current_step": 40,
    "total_steps": 40,
    "output_url": "/api/generate/jobs/a1b2c3d4e5f6/video",
    "resolved_seed": 12345,
    "duration_seconds": 612.4
  }
}
```

`state` is one of `queued | running | done | error | cancelled`. `stage` adds
`preparing | loading | generating | encoding | cancelling | interrupted`, and
`queue_position` reports strict oldest-first local scheduling. Only one local
heavyweight render runs at a time.

### JavaScript (fetch)

```js
const BASE = "http://localhost:47872";

// Text-to-video
const res = await fetch(`${BASE}/api/generate/txt2video`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    repo: "zai-org/CogVideoX-2b",
    prompt: "A cinematic drone shot over a misty forest at sunrise",
    frames: 97, fps: 24, steps: 40, guidance: 3.0, seed: -1,
  }),
});
const { job } = await res.json();

// Poll until done, then the clip is at output_url
let state = job.state, id = job.id;
while (!["done", "error", "cancelled"].includes(state)) {
  await new Promise((r) => setTimeout(r, 3000));
  const j = await (await fetch(`${BASE}/api/generate/jobs/${id}`)).json();
  state = j.job.state;
}
console.log("video:", `${BASE}/api/generate/jobs/${id}/video`);

// If a saved cloud poller ever stops, re-attach to its original provider task.
// This does not submit another paid generation.
await fetch(`${BASE}/api/generate/jobs/${id}/repair`, { method: "POST" });

// Video-to-video (multipart): restyle an input clip
const fd = new FormData();
fd.append("file", inputClipBlob, "input.mp4");
fd.append("repo", "zai-org/CogVideoX-5b");
fd.append("mode", "video2video");
fd.append("prompt", "make it look like a watercolor painting");
fd.append("strength", "0.7");
await fetch(`${BASE}/api/generate/video2video`, { method: "POST", body: fd });
```

### Python (requests)

```python
import time, requests

BASE = "http://localhost:47872"

# Text-to-video
job = requests.post(f"{BASE}/api/generate/txt2video", json={
    "repo": "zai-org/CogVideoX-2b",
    "prompt": "A corgi running across a sunny meadow, slow motion",
    "frames": 97, "fps": 24, "steps": 40, "guidance": 3.0, "seed": -1,
}).json()["job"]

# Poll
while job["state"] not in ("done", "error", "cancelled"):
    time.sleep(3)
    job = requests.get(f"{BASE}/api/generate/jobs/{job['id']}").json()["job"]

# Save the mp4
if job["state"] == "done":
    mp4 = requests.get(f"{BASE}/api/generate/jobs/{job['id']}/video").content
    open("out.mp4", "wb").write(mp4)

# Safe cloud repair: reuses the provider task ID already saved for this job.
requests.post(f"{BASE}/api/generate/jobs/{job['id']}/repair")

# Image-to-video (multipart)
with open("frame.png", "rb") as f:
    requests.post(f"{BASE}/api/generate/video2video",
        files={"file": ("frame.png", f, "image/png")},
        data={"repo": "zai-org/CogVideoX-5b-I2V", "mode": "img2video",
              "prompt": "gentle parallax camera move", "frames": 49, "fps": 8})
```

### curl

```bash
BASE=http://localhost:47872

# Text-to-video
curl -s -X POST "$BASE/api/generate/txt2video" \
  -H 'Content-Type: application/json' \
  -d '{"repo":"zai-org/CogVideoX-2b","prompt":"a neon Tokyo street at night","frames":49,"fps":8,"steps":50,"guidance":6.0,"seed":-1}'

# Poll a job
curl -s "$BASE/api/generate/jobs/<JOB_ID>"

# Download the result
curl -s "$BASE/api/generate/jobs/<JOB_ID>/video" -o out.mp4

# Image-to-video / video-to-video (multipart)
curl -s -X POST "$BASE/api/generate/video2video" \
  -F file=@input.mp4 \
  -F repo=zai-org/CogVideoX-5b \
  -F mode=video2video \
  -F prompt="watercolor style" \
  -F strength=0.7

# Start a model download
curl -s -X POST "$BASE/api/downloads" \
  -H 'Content-Type: application/json' \
  -d '{"repo":"zai-org/CogVideoX-2b"}'
```

---

## Cloud video providers (gateway)

Video Studio is also a **gateway** for cloud video generators. Link fal.ai,
Kie.ai, or Replicate and its models appear in the **same**
`/api/catalog` alongside local ones, with the same generation API — so a client
like Story Studio connects **once** and gets local **and** cloud models, kept
current as providers add/deprecate models. See `SPEC.md` for the full design.

- **Link a provider:** Settings → *Cloud video providers* → paste your API key,
  then enable **paid generation** (nothing bills until both are set).
- **Spend guardrails:** set **per-provider and global** daily/monthly USD caps
  (Settings → *Spend guardrails*). Caps reset on the calendar; a generation that
  would exceed a cap is **blocked before it runs**. Every cloud job's cost is
  recorded. The Generate tab shows the estimate before submit, and Settings
  plots the last 14 days of spend by provider.
- **Cloud controls:** filter ready models by capability, minimum duration, and
  resolution, then set the selected model's duration, resolution, and aspect
  ratio directly in Generate.
- **Routing:** cloud models carry a `provider:` id (e.g.
  `fal:fal-ai/kling-video/v2/master/text-to-video`). Generation goes through the
  **same** endpoints; the gateway submits to the provider, polls, and downloads
  the clip into `app/output/` — the job/SSE/`/video` lifecycle is identical to a
  local render.
- **Freshness + recovery:** provider catalogs refresh through a persistent TTL
  cache. New models are marked, removed models remain visible as deprecated for
  30 days. Each cloud job saves its intent before crossing the paid API boundary,
  then saves the provider's task ID immediately when it is returned.
  Timeouts and temporary network/result-download failures back off and keep
  polling that same task indefinitely; they never submit or bill a replacement.
  A watchdog re-attaches stopped pollers automatically after errors or app
  restarts, and a **repair saved task** action is available for manual recovery.
  If the initial submit response is lost before a task ID arrives, Video Studio
  marks the outcome unknown and blocks further paid submissions to that provider
  rather than guessing and risking duplicate credits.
- **Price safety:** a cloud model without a verified price remains browseable,
  but Video Studio refuses to submit it until a trustworthy price is configured.
  Per-second estimates are reconciled from the downloaded MP4's actual duration;
  fixed per-video prices remain exact.

Provider/spend API:

```bash
BASE=http://localhost:47872

# List providers (key-set state, paid toggle, model count, spend vs caps)
curl -s "$BASE/api/providers"
# Set a provider API key (owner-only; never returned)
curl -s -X POST "$BASE/api/providers/fal/key"  -H 'Content-Type: application/json' -d '{"key":"YOUR_FAL_KEY"}'
# Enable paid generation
curl -s -X POST "$BASE/api/providers/fal/paid" -H 'Content-Type: application/json' -d '{"paid":true}'
# Spend today/month vs caps + recent records + 14-day history
curl -s "$BASE/api/spend"
# Set caps (USD; 0 = no cap)
curl -s -X POST "$BASE/api/spend/caps" -H 'Content-Type: application/json' \
  -d '{"global":{"daily":20,"monthly":300},"per_provider":{"fal":{"daily":10,"monthly":150}}}'

# Generate with a cloud model — same endpoint as local
curl -s -X POST "$BASE/api/generate/txt2video" -H 'Content-Type: application/json' \
  -d '{"repo":"fal:fal-ai/kling-video/v2/master/text-to-video","prompt":"a red kite over the sea","duration":5}'

# Manually re-attach polling to the provider task already saved for a job.
# This endpoint never creates a new provider task.
curl -s -X POST "$BASE/api/generate/jobs/<JOB_ID>/repair"
```

> Curated model metadata lives in `app/backend/providers/*_models.json`.
> Replicate augments its curated entries from its live text-to-video collection;
> fal.ai and Kie.ai currently use the curated files because they do not expose a
> suitable stable model-list endpoint for this gateway.

## Notes & limitations

- **Apple Silicon only** (`darwin` / `arm64`). Lance runs natively through MLX;
  the other local families target MPS, with unsupported Diffusers ops allowed
  to fall back to CPU through `PYTORCH_ENABLE_MPS_FALLBACK`.
- **Frame counts** are rounded to each architecture's valid value (LTX/CogVideoX
  → 8·n+1, Lance/Wan/HunyuanVideo → 4·n+1).
- The newest models may require a recent **diffusers** release; if a pipeline is
  missing, run **Install Generation** again (it reinstalls/updates the engine)
  or bump the pin in `app/requirements-generation.txt`.
- Models, downloads, generated clips, and the Python env live under the launcher
  folder and are git-ignored.
