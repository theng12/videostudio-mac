# Video Studio KH

Local **text-to-video** and **video-to-video** generation for Apple Silicon,
powered by PyTorch (MPS) + 🤗 Diffusers. A 1-click Pinokio launcher with a model
catalog, download manager, and a generation UI with an in-browser video player.

Part of the **KH studio suite** (Chat / Image / Music / Voice / **Video**) — each
a standalone single-modality launcher.

> **Heads-up on performance.** Local video generation is heavy and slow on
> Apple Silicon, especially for the larger models (Wan 2.2 A14B, HunyuanVideo).
> Start with **LTX-Video** (lightest) to confirm your setup, and reserve the big
> models for a high-memory Mac (e.g. an M3 Ultra Mac Studio).

---

## What it does

- **Text → Video** — generate a clip from a prompt.
- **Image → Video** — animate a still image (first-frame / image-to-video).
- **Video → Video** — restyle an existing clip guided by a prompt (CogVideoX).
- **Model catalog + downloads** — browse, download, and manage Hugging Face
  weights with live progress; downloads are independent of generation.
- **In-browser player** — generated `.mp4` clips play inline; download or reveal
  in Finder.

### Seed model catalog

| Family | Models | Modes |
|---|---|---|
| **LTX-Video** | `Lightricks/LTX-Video`, `Lightricks/LTX-Video-0.9.7-distilled` | t2v, i2v |
| **Wan 2.2** | `Wan-AI/Wan2.2-TI2V-5B-Diffusers`, `…T2V-A14B…`, `…I2V-A14B…` | t2v, i2v |
| **HunyuanVideo** | `hunyuanvideo-community/HunyuanVideo`, `…-I2V` | t2v, i2v |
| **CogVideoX** | `THUDM/CogVideoX-2b`, `…-5b`, `…-5b-I2V` | t2v, i2v, **v2v** |

---

## How to use (Pinokio)

1. **Install** — installs a Python env and the lightweight server (catalog +
   downloads only; fast).
2. **Install Generation** — installs the heavy PyTorch + Diffusers engine. Run
   this once before generating.
3. **Start** — launches the server and opens the Web UI.
4. In the **Models** tab, download a model (start with LTX-Video).
5. In the **Generate** tab, pick the model, choose a mode, write a prompt (and
   upload an image/clip for i2v/v2v), then **Generate video**.

Gated repos need a Hugging Face token — paste it in **Settings**.

The server binds `0.0.0.0:47872` so other devices on your LAN / Tailscale can
reach the API directly. On this machine it's also at `http://localhost:47872`.

### Run as an always-on service (optional)

Use **Install as Startup Service** in the sidebar to run the server via a macOS
launchd agent that starts at login and self-heals (KeepAlive + a health
watchdog). Use the service **or** Pinokio's Start — not both (they share the
port).

---

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

`state` is one of `queued | running | done | error | cancelled`.

### JavaScript (fetch)

```js
const BASE = "http://localhost:47872";

// Text-to-video
const res = await fetch(`${BASE}/api/generate/txt2video`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    repo: "Lightricks/LTX-Video",
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

// Video-to-video (multipart): restyle an input clip
const fd = new FormData();
fd.append("file", inputClipBlob, "input.mp4");
fd.append("repo", "THUDM/CogVideoX-5b");
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
    "repo": "Lightricks/LTX-Video",
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

# Image-to-video (multipart)
with open("frame.png", "rb") as f:
    requests.post(f"{BASE}/api/generate/video2video",
        files={"file": ("frame.png", f, "image/png")},
        data={"repo": "THUDM/CogVideoX-5b-I2V", "mode": "img2video",
              "prompt": "gentle parallax camera move", "frames": 49, "fps": 8})
```

### curl

```bash
BASE=http://localhost:47872

# Text-to-video
curl -s -X POST "$BASE/api/generate/txt2video" \
  -H 'Content-Type: application/json' \
  -d '{"repo":"Lightricks/LTX-Video","prompt":"a neon Tokyo street at night","frames":97,"fps":24,"steps":40,"guidance":3.0,"seed":-1}'

# Poll a job
curl -s "$BASE/api/generate/jobs/<JOB_ID>"

# Download the result
curl -s "$BASE/api/generate/jobs/<JOB_ID>/video" -o out.mp4

# Image-to-video / video-to-video (multipart)
curl -s -X POST "$BASE/api/generate/video2video" \
  -F file=@input.mp4 \
  -F repo=THUDM/CogVideoX-5b \
  -F mode=video2video \
  -F prompt="watercolor style" \
  -F strength=0.7

# Start a model download
curl -s -X POST "$BASE/api/downloads" \
  -H 'Content-Type: application/json' \
  -d '{"repo":"Lightricks/LTX-Video"}'
```

---

## Notes & limitations

- **Apple Silicon only** (`darwin` / `arm64`). The engine targets the MPS
  backend; some Diffusers ops fall back to CPU via `PYTORCH_ENABLE_MPS_FALLBACK`.
- **Frame counts** are rounded to each architecture's valid value (LTX/CogVideoX
  → 8·n+1, Wan/HunyuanVideo → 4·n+1).
- The newest models may require a recent **diffusers** release; if a pipeline is
  missing, run **Install Generation** again (it reinstalls/updates the engine)
  or bump the pin in `app/requirements-generation.txt`.
- Models, downloads, generated clips, and the Python env live under the launcher
  folder and are git-ignored.
