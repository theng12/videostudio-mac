/* global Alpine, VIDEO_PROMPTS */

// Video Studio KH — single-file Alpine component.
// Talks to the FastAPI backend in app/backend. Three tabs (Generate, Models,
// Downloads) plus Settings, wired to the same /api surface as the launcher.

function studio() {
  return {
    // ──────── top-level state ────────
    tab: "generate",
    health: { ok: false },
    apiBase: window.location.origin,

    families: {},          // {id: {id,label,summary,how_to_use}}
    models: [],            // serialized catalog rows (with .cache + .video_defaults)
    downloads: [],         // /api/downloads/stream snapshot
    genJobs: [],           // /api/generate/stream snapshot (newest first)

    diag: { device: null, packages: [], engines: [], ready_count: 0, total_engines: 0 },

    gen: {
      available: false,
      repo: "",
      mode: "txt2video",
      prompt: "",
      negativePrompt: "",
      frames: 97, fps: 24, steps: 40, guidance: 3.0,
      width: 704, height: 480, seed: -1, strength: 0.7,
      inputFile: null, inputUrl: "", inputName: "",
      submitting: false,
    },

    settings: {
      tokenInput: "", showToken: false, busy: false,
      message: "", messageKind: "info",
      hf_token_set: false, hf_token_masked: "",
    },
    conn: { bind_port: 47872 },

    toasts: [],
    _toastSeq: 0,
    _doneRepos: {},        // repo → true once its download finished (to refresh catalog once)

    // ──────── computed ────────
    get cachedModels() {
      return this.models.filter((m) => m.cache && m.cache.state === "cached");
    },
    get selectedModel() {
      return this.models.find((m) => m.repo === this.gen.repo) || null;
    },
    get selectedCapabilities() {
      return this.selectedModel ? this.selectedModel.capabilities : [];
    },
    get familyList() {
      return Object.values(this.families);
    },
    get activeDownloads() {
      return this.downloads.filter((d) => ["queued", "running"].includes(d.state));
    },

    // ──────── lifecycle ────────
    async init() {
      await this.refreshHealth();
      await this.loadCatalog();
      await this.loadDiagnostics();
      await this.loadSettings();
      this.loadConnectivity();
      this.openDownloadsStream();
      this.openGenerateStream();
      // light periodic health + diagnostics poll
      setInterval(() => this.refreshHealth(), 8000);
      setInterval(() => this.loadDiagnostics(), 15000);
    },

    async refreshHealth() {
      try {
        const r = await fetch("/api/health");
        this.health = { ok: r.ok };
      } catch (_) {
        this.health = { ok: false };
      }
    },

    async loadCatalog() {
      try {
        const r = await fetch("/api/catalog");
        const data = await r.json();
        this.families = data.families || {};
        this.models = data.models || [];
        this._syncDownloadsToModels();
        // Pick a sensible default model in Generate (first cached one).
        if (!this.gen.repo && this.cachedModels.length) {
          this.gen.repo = this.cachedModels[0].repo;
          this.applyModelDefaults();
        }
      } catch (e) {
        this.pushToast("Failed to load catalog: " + e, "error");
      }
    },

    async loadDiagnostics() {
      try {
        const r = await fetch("/api/generate/diagnostics");
        const d = await r.json();
        this.diag = d;
        this.gen.available = !!d.available;
      } catch (_) { /* engine may not be installed yet */ }
    },

    async loadSettings() {
      try {
        const r = await fetch("/api/settings");
        const s = await r.json();
        this.settings.hf_token_set = !!s.hf_token_set;
        this.settings.hf_token_masked = s.hf_token_masked || "";
      } catch (_) {}
    },

    async loadConnectivity() {
      try {
        const r = await fetch("/api/connectivity");
        const c = await r.json();
        if (c.bind_port) this.conn.bind_port = c.bind_port;
      } catch (_) {}
    },

    // ──────── SSE streams ────────
    openDownloadsStream() {
      const es = new EventSource("/api/downloads/stream");
      es.addEventListener("snapshot", (e) => {
        try {
          const payload = JSON.parse(e.data);
          this.downloads = (payload.jobs || []).slice().reverse();
          this._syncDownloadsToModels();
        } catch (_) {}
      });
      es.onerror = () => { /* browser auto-reconnects */ };
    },

    openGenerateStream() {
      const es = new EventSource("/api/generate/stream");
      es.addEventListener("snapshot", (e) => {
        try {
          const payload = JSON.parse(e.data);
          this.genJobs = (payload.jobs || []).slice().reverse();
        } catch (_) {}
      });
      es.onerror = () => {};
    },

    // Attach the live download job to each model card; when a repo's download
    // finishes, refresh the catalog once so its cache state flips to "cached".
    _syncDownloadsToModels() {
      const byRepo = {};
      for (const d of this.downloads) byRepo[d.repo] = d;
      let needsCatalogRefresh = false;
      for (const m of this.models) {
        const d = byRepo[m.repo];
        m.active_download = d && ["queued", "running"].includes(d.state) ? d : null;
        if (d && d.state === "done" && !this._doneRepos[m.repo]) {
          this._doneRepos[m.repo] = true;
          needsCatalogRefresh = true;
        }
      }
      if (needsCatalogRefresh) setTimeout(() => this.loadCatalog(), 500);
    },

    // ──────── Generate ────────
    modeLabel(cap) {
      return { txt2video: "Text → Video", img2video: "Image → Video", video2video: "Video → Video" }[cap] || cap;
    },

    onModelChange() {
      this.applyModelDefaults();
    },

    applyModelDefaults() {
      const m = this.selectedModel;
      if (!m) return;
      const d = m.video_defaults || {};
      this.gen.frames = d.frames ?? this.gen.frames;
      this.gen.fps = d.fps ?? this.gen.fps;
      this.gen.steps = d.steps ?? this.gen.steps;
      this.gen.guidance = d.guidance ?? this.gen.guidance;
      this.gen.width = d.width ?? this.gen.width;
      this.gen.height = d.height ?? this.gen.height;
      // Default the mode to the model's first capability if the current one
      // isn't supported.
      if (!m.capabilities.includes(this.gen.mode)) {
        this.gen.mode = m.capabilities[0];
      }
    },

    frameHint() {
      const m = this.selectedModel;
      if (!m) return "";
      const base = { "ltx-video": 8, "wan22": 4, "hunyuanvideo": 4, "cogvideox": 8 }[m.family] || 8;
      return `Frames are rounded to ${base}·n+1 for this model. Bigger frames/steps = much longer generation.`;
    },

    onFile(event) {
      const f = event.target.files && event.target.files[0];
      if (!f) return;
      if (this.gen.inputUrl) URL.revokeObjectURL(this.gen.inputUrl);
      this.gen.inputFile = f;
      this.gen.inputName = f.name;
      this.gen.inputUrl = URL.createObjectURL(f);
    },

    randomPrompt() {
      const list = window.VIDEO_PROMPTS || [];
      if (!list.length) return;
      this.gen.prompt = list[Math.floor(Math.random() * list.length)];
    },

    async submitGenerate() {
      if (!this.gen.prompt.trim()) return;
      this.gen.submitting = true;
      try {
        let res;
        if (this.gen.mode === "txt2video") {
          res = await fetch("/api/generate/txt2video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              repo: this.gen.repo,
              prompt: this.gen.prompt,
              negative_prompt: this.gen.negativePrompt,
              width: this.gen.width,
              height: this.gen.height,
              frames: this.gen.frames,
              fps: this.gen.fps,
              steps: this.gen.steps,
              guidance: this.gen.guidance,
              seed: this.gen.seed,
            }),
          });
        } else {
          if (!this.gen.inputFile) {
            this.pushToast("Pick an input " + (this.gen.mode === "img2video" ? "image" : "video") + " first.", "error");
            this.gen.submitting = false;
            return;
          }
          const fd = new FormData();
          fd.append("file", this.gen.inputFile);
          fd.append("repo", this.gen.repo);
          fd.append("mode", this.gen.mode);
          fd.append("prompt", this.gen.prompt);
          fd.append("negative_prompt", this.gen.negativePrompt);
          fd.append("frames", this.gen.frames);
          fd.append("fps", this.gen.fps);
          fd.append("steps", this.gen.steps);
          fd.append("guidance", this.gen.guidance);
          fd.append("seed", this.gen.seed);
          if (this.gen.mode !== "video2video") {
            fd.append("width", this.gen.width);
            fd.append("height", this.gen.height);
          } else {
            fd.append("strength", this.gen.strength);
          }
          res = await fetch("/api/generate/video2video", { method: "POST", body: fd });
        }
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          this.pushToast(err.detail || `Generate failed (${res.status})`, "error");
        } else {
          this.pushToast("Job queued — this can take a while.", "info");
        }
      } catch (e) {
        this.pushToast("Generate error: " + e, "error");
      } finally {
        setTimeout(() => { this.gen.submitting = false; }, 400);
      }
    },

    async cancelJob(id) {
      try { await fetch(`/api/generate/jobs/${id}`, { method: "DELETE" }); } catch (_) {}
    },

    async clearHistory() {
      try { await fetch("/api/generate/jobs", { method: "DELETE" }); this.genJobs = []; } catch (_) {}
    },

    useInGenerate(repo) {
      this.gen.repo = repo;
      this.applyModelDefaults();
      this.tab = "generate";
    },

    // ──────── Models / Downloads ────────
    modelsByFamily(famId) {
      return this.models.filter((m) => m.family === famId);
    },

    async startDownload(repo) {
      try {
        const r = await fetch("/api/downloads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          this.pushToast(err.detail || "Download failed to start", "error");
        } else {
          this.pushToast("Download started: " + repo, "info");
          this.tab = "downloads";
        }
      } catch (e) {
        this.pushToast("Download error: " + e, "error");
      }
    },

    async cancelDownload(id) {
      try { await fetch(`/api/downloads/${id}`, { method: "DELETE" }); } catch (_) {}
    },

    async clearDownloads() {
      try { await fetch("/api/downloads", { method: "DELETE" }); this.downloads = []; } catch (_) {}
    },

    // ──────── Settings ────────
    async saveToken() {
      this.settings.busy = true;
      this.settings.message = "";
      try {
        const r = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ hf_token: this.settings.tokenInput }),
        });
        const s = await r.json();
        this.settings.hf_token_set = !!s.hf_token_set;
        this.settings.hf_token_masked = s.hf_token_masked || "";
        this.settings.tokenInput = "";
        this.settings.message = "Saved.";
        this.settings.messageKind = "success";
      } catch (e) {
        this.settings.message = "Save failed: " + e;
        this.settings.messageKind = "error";
      } finally {
        this.settings.busy = false;
      }
    },

    async testToken() {
      this.settings.busy = true;
      this.settings.message = "Testing…";
      this.settings.messageKind = "info";
      try {
        const body = this.settings.tokenInput ? { hf_token: this.settings.tokenInput } : {};
        const r = await fetch("/api/settings/test-hf-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const d = await r.json();
        if (r.ok) {
          this.settings.message = "Valid — " + (d.name || "ok");
          this.settings.messageKind = "success";
        } else {
          this.settings.message = d.detail || "Invalid token";
          this.settings.messageKind = "error";
        }
      } catch (e) {
        this.settings.message = "Test failed: " + e;
        this.settings.messageKind = "error";
      } finally {
        this.settings.busy = false;
      }
    },

    // ──────── helpers ────────
    fmtBytes(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + " B";
      const u = ["KB", "MB", "GB", "TB"];
      let i = -1;
      do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
      return n.toFixed(1) + " " + u[i];
    },

    pushToast(text, kind = "info") {
      const id = ++this._toastSeq;
      this.toasts.push({ id, text, kind });
      setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id);
      }, 4500);
    },
  };
}
