/* global Alpine, VIDEO_PROMPTS */

// Video Studio KH — single-file Alpine component.
// Tabs: Generate, Models (search/sort/filters + RAM planner), Downloads, Settings.
// Talks to the FastAPI backend in app/backend over the same /api surface.

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

    // hardware snapshot + RAM planner
    system: { chip: null, chip_tier: null, unified_memory_gb: null },
    ramGb: 0,
    ramIsDetected: false,
    ramTiers: [16, 32, 64, 128, 256, 512],

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

    // Models-tab library filters
    modelFilters: {
      search: "",
      families: new Set(),
      statuses: new Set(),
      capabilities: new Set(),
      fitLevel: "all",          // all | ok | tight | over
      sortBy: "default",        // default | name | size-asc | size-desc
      collapsedFamilies: new Set(),
      expandedRepos: new Set(),
    },

    settings: {
      tokenInput: "", showToken: false, busy: false,
      message: "", messageKind: "info",
      hf_token_set: false, hf_token_masked: "",
    },
    conn: { bind_port: 47872 },

    toasts: [],
    _toastSeq: 0,
    _doneRepos: {},

    // ──────── Generate computed ────────
    get cachedModels() {
      return this.models.filter((m) => m.cache && m.cache.state === "cached");
    },
    get selectedModel() {
      return this.models.find((m) => m.repo === this.gen.repo) || null;
    },
    get selectedCapabilities() {
      return this.selectedModel ? this.selectedModel.capabilities : [];
    },
    get activeDownloads() {
      return this.downloads.filter((d) => ["queued", "running"].includes(d.state));
    },

    // ──────── lifecycle ────────
    async init() {
      await this.refreshHealth();
      await this.loadSystem();
      await this.loadCatalog();
      await this.loadDiagnostics();
      await this.loadSettings();
      this.loadConnectivity();
      this._initRamPlanner();
      this.openDownloadsStream();
      this.openGenerateStream();
      setInterval(() => this.refreshHealth(), 8000);
      setInterval(() => this.loadDiagnostics(), 15000);
    },

    async refreshHealth() {
      try { this.health = { ok: (await fetch("/api/health")).ok }; }
      catch (_) { this.health = { ok: false }; }
    },

    async loadSystem() {
      try {
        const s = await (await fetch("/api/system")).json();
        this.system = {
          chip: s.chip || null,
          chip_tier: s.chip_tier || null,
          unified_memory_gb: s.unified_memory_gb || null,
        };
      } catch (_) {}
    },

    async loadCatalog() {
      try {
        const data = await (await fetch("/api/catalog")).json();
        this.families = data.families || {};
        this.models = data.models || [];
        this._syncDownloadsToModels();
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
        const d = await (await fetch("/api/generate/diagnostics")).json();
        this.diag = d;
        this.gen.available = !!d.available;
      } catch (_) {}
    },

    async loadSettings() {
      try {
        const s = await (await fetch("/api/settings")).json();
        this.settings.hf_token_set = !!s.hf_token_set;
        this.settings.hf_token_masked = s.hf_token_masked || "";
      } catch (_) {}
    },

    async loadConnectivity() {
      try {
        const c = await (await fetch("/api/connectivity")).json();
        if (c.bind_port) this.conn.bind_port = c.bind_port;
      } catch (_) {}
    },

    // ──────── SSE streams ────────
    openDownloadsStream() {
      const es = new EventSource("/api/downloads/stream");
      es.addEventListener("snapshot", (e) => {
        try {
          this.downloads = (JSON.parse(e.data).jobs || []).slice().reverse();
          this._syncDownloadsToModels();
        } catch (_) {}
      });
    },
    openGenerateStream() {
      const es = new EventSource("/api/generate/stream");
      es.addEventListener("snapshot", (e) => {
        try { this.genJobs = (JSON.parse(e.data).jobs || []).slice().reverse(); }
        catch (_) {}
      });
    },
    _syncDownloadsToModels() {
      const byRepo = {};
      for (const d of this.downloads) byRepo[d.repo] = d;
      let refresh = false;
      for (const m of this.models) {
        const d = byRepo[m.repo];
        m.active_download = d && ["queued", "running"].includes(d.state) ? d : null;
        if (d && d.state === "done" && !this._doneRepos[m.repo]) {
          this._doneRepos[m.repo] = true; refresh = true;
        }
      }
      if (refresh) setTimeout(() => this.loadCatalog(), 500);
    },

    // ──────── RAM planner + hardware fit ────────
    get effectiveRam() {
      return this.ramGb || this.system.unified_memory_gb || 32;
    },
    /** Client-side fit verdict vs the RAM budget. Mirrors the backend's
     *  system_info.fit_for (>=1.5x comfortable / >=1.0x tight / below = risky)
     *  so every fit chip re-scores live as the slider moves. */
    fitFor(minGb) {
      const actual = this.effectiveRam;
      const floor = Math.max(Number(minGb) || 0, 1);
      const headroom = actual / floor;
      let state;
      if (headroom >= 1.5) state = "ok";
      else if (headroom >= 1.0) state = "tight";
      else state = "risky";
      const hint = headroom >= 1.5
        ? `${actual} GB is ≥1.5× this model's ${minGb} GB floor — comfortable headroom.`
        : headroom >= 1.0
          ? `${actual} GB just clears the ${minGb} GB floor — close other apps before loading.`
          : `${actual} GB is below the ${minGb} GB floor — it would swap heavily or fail to load.`;
      return { state, actual_gb: actual, required_gb: Number(minGb) || 0, hint };
    },
    setRam(gb) {
      const v = Math.max(1, Math.min(1024, Math.round(Number(gb) || 0)));
      this.ramGb = v;
      this.ramIsDetected = (v === this.system.unified_memory_gb);
      try { localStorage.setItem("videostudio.ramGb", String(v)); } catch {}
    },
    resetRamToDetected() {
      if (this.system.unified_memory_gb) this.setRam(this.system.unified_memory_gb);
    },
    _initRamPlanner() {
      try {
        const saved = localStorage.getItem("videostudio.ramGb");
        if (saved !== null && !isNaN(+saved)) {
          this.ramGb = +saved;
          this.ramIsDetected = (+saved === this.system.unified_memory_gb);
          return;
        }
      } catch {}
      this.ramGb = this.system.unified_memory_gb || 32;
      this.ramIsDetected = !!this.system.unified_memory_gb;
    },
    /** "✨ Best for your RAM" — one model per lane that best fits the budget,
     *  re-computed live. Video lanes: top quality, fastest/lightest, best v2v. */
    get bestPicks() {
      const fits = (m) => this.fitFor(m.min_unified_memory_gb).state !== "risky";
      const heavy = (m) => (Number(m.min_unified_memory_gb) || 0) * 1000 + (Number(m.size_gb) || 0);
      const hasCap = (m, c) => (m.capabilities || []).includes(c);
      const pickHeavy = (pred) => {
        const c = this.models.filter((m) => fits(m) && pred(m));
        return c.length ? c.slice().sort((a, b) => heavy(b) - heavy(a))[0] : null;
      };
      const pickLight = (pred) => {
        const c = this.models.filter((m) => fits(m) && pred(m));
        return c.length ? c.slice().sort((a, b) => (a.size_gb || 0) - (b.size_gb || 0))[0] : null;
      };
      const buckets = [
        { id: "quality", label: "Best quality", icon: "🏆", model: pickHeavy(() => true) },
        { id: "fast", label: "Fastest / lightest", icon: "⚡", model: pickLight((m) => hasCap(m, "txt2video")) },
        { id: "v2v", label: "Best for video→video", icon: "🎬", model: pickHeavy((m) => hasCap(m, "video2video")) },
      ];
      const seen = new Set();
      return buckets.filter((b) => {
        if (!b.model || seen.has(b.model.repo)) return false;
        seen.add(b.model.repo); return true;
      });
    },

    // ──────── Models-tab filters ────────
    get filteredModelsByFamily() {
      const f = this.modelFilters;
      const q = (f.search || "").trim().toLowerCase();
      const matches = (m) => {
        if (f.families.size > 0 && !f.families.has(m.family)) return false;
        if (f.statuses.size > 0) {
          const state = m.cache?.state || "absent";
          const ready = this.isModelReady(m.repo);
          const ok = f.statuses.has(state) || (f.statuses.has("engine-ready") && ready && state === "cached");
          if (!ok) return false;
        }
        if (f.capabilities.size > 0) {
          const caps = new Set(m.capabilities || []);
          for (const want of f.capabilities) if (!caps.has(want)) return false;
        }
        if (f.fitLevel && f.fitLevel !== "all") {
          const st = this.fitFor(m.min_unified_memory_gb).state;
          if (f.fitLevel === "ok" && st !== "ok") return false;
          if (f.fitLevel === "tight" && st !== "tight") return false;
          if (f.fitLevel === "over" && st !== "risky") return false;
        }
        if (q) {
          const hay = ((m.label || "") + " " + (m.repo || "") + " " + (m.best_for || "")).toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      };
      const out = {};
      for (const m of this.models) if (matches(m)) (out[m.family] ||= []).push(m);
      const cmp = (() => {
        switch (f.sortBy) {
          case "name": return (a, b) => (a.label || "").localeCompare(b.label || "");
          case "size-asc": return (a, b) => (a.size_gb || 0) - (b.size_gb || 0);
          case "size-desc": return (a, b) => (b.size_gb || 0) - (a.size_gb || 0);
          default: return (a, b) => (a.size_gb || 0) - (b.size_gb || 0);
        }
      })();
      for (const fam of Object.keys(out)) out[fam].sort(cmp);
      return out;
    },
    get availableFamilies() {
      const seen = new Set(); const out = [];
      for (const m of this.models) {
        if (seen.has(m.family)) continue;
        seen.add(m.family);
        out.push({ id: m.family, label: m.family_label || this.families?.[m.family]?.label || m.family });
      }
      return out.sort((a, b) => a.label.localeCompare(b.label));
    },
    get availableCapabilities() {
      const set = new Set();
      for (const m of this.models) for (const c of (m.capabilities || [])) set.add(c);
      return Array.from(set).sort();
    },
    get filteredModelTotalCount() {
      return Object.values(this.filteredModelsByFamily).reduce((s, l) => s + l.length, 0);
    },
    get hasActiveFilters() {
      const f = this.modelFilters;
      return !!(f.search.trim() || f.families.size || f.statuses.size || f.capabilities.size
        || (f.fitLevel && f.fitLevel !== "all"));
    },
    familiesWithResults() {
      const fb = this.filteredModelsByFamily;
      return Object.values(this.families).filter((f) => (fb[f.id] || []).length > 0);
    },
    activeFilterSummary() {
      const f = this.modelFilters; const out = [];
      if (f.search.trim()) out.push({ label: `search: "${f.search.trim()}"`, removeFn: () => (this.modelFilters.search = "") });
      for (const fam of f.families) {
        const lbl = this.availableFamilies.find((x) => x.id === fam)?.label || fam;
        out.push({ label: `family: ${lbl}`, removeFn: () => this.toggleFamilyFilter(fam) });
      }
      for (const s of f.statuses) out.push({ label: `status: ${s}`, removeFn: () => this.toggleStatusFilter(s) });
      for (const c of f.capabilities) out.push({ label: `capability: ${this.capabilityLabel(c)}`, removeFn: () => this.toggleCapabilityFilter(c) });
      if (f.fitLevel && f.fitLevel !== "all") {
        const lbl = { ok: "✓ Fits", tight: "⚠ Tight", over: "✗ Over" }[f.fitLevel] || f.fitLevel;
        out.push({ label: `RAM fit: ${lbl}`, removeFn: () => (this.modelFilters.fitLevel = "all") });
      }
      return out;
    },
    toggleFamilyFilter(id) { const s = this.modelFilters.families; s.has(id) ? s.delete(id) : s.add(id); this.modelFilters.families = new Set(s); },
    toggleStatusFilter(st) { const s = this.modelFilters.statuses; s.has(st) ? s.delete(st) : s.add(st); this.modelFilters.statuses = new Set(s); },
    toggleCapabilityFilter(c) { const s = this.modelFilters.capabilities; s.has(c) ? s.delete(c) : s.add(c); this.modelFilters.capabilities = new Set(s); },
    isFamilyFiltered(id) { return this.modelFilters.families.has(id); },
    isStatusFiltered(st) { return this.modelFilters.statuses.has(st); },
    isCapFiltered(c) { return this.modelFilters.capabilities.has(c); },
    clearAllFilters() {
      this.modelFilters.search = "";
      this.modelFilters.families = new Set();
      this.modelFilters.statuses = new Set();
      this.modelFilters.capabilities = new Set();
      this.modelFilters.fitLevel = "all";
      this.modelFilters.sortBy = "default";
    },
    // per-card + per-family expand/collapse
    isModelExpanded(repo) { return this.modelFilters.expandedRepos.has(repo); },
    toggleModelExpanded(repo) { const s = this.modelFilters.expandedRepos; s.has(repo) ? s.delete(repo) : s.add(repo); this.modelFilters.expandedRepos = new Set(s); },
    expandAllVisible() {
      const s = new Set(this.modelFilters.expandedRepos);
      for (const l of Object.values(this.filteredModelsByFamily)) for (const m of l) s.add(m.repo);
      this.modelFilters.expandedRepos = s;
    },
    collapseAllVisible() { this.modelFilters.expandedRepos = new Set(); },
    isFamilyCollapsed(id) { return this.modelFilters.collapsedFamilies.has(id); },
    toggleFamilyCollapsed(id) { const s = this.modelFilters.collapsedFamilies; s.has(id) ? s.delete(id) : s.add(id); this.modelFilters.collapsedFamilies = new Set(s); },

    // engine readiness (from diagnostics)
    modelEngine(repo) {
      const m = this.models.find((x) => x.repo === repo);
      if (!m) return null;
      return (this.diag.engines || []).find((e) => e.family === m.family) || null;
    },
    isModelReady(repo) {
      const e = this.modelEngine(repo);
      if (!e) return false;
      return !!e.ready;
    },
    modelMissingDeps(repo) { const e = this.modelEngine(repo); return e ? (e.missing || []) : []; },

    // ──────── Generate ────────
    modeLabel(cap) { return { txt2video: "Text → Video", img2video: "Image → Video", video2video: "Video → Video" }[cap] || cap; },
    onModelChange() { this.applyModelDefaults(); },
    applyModelDefaults() {
      const m = this.selectedModel; if (!m) return;
      const d = m.video_defaults || {};
      this.gen.frames = d.frames ?? this.gen.frames;
      this.gen.fps = d.fps ?? this.gen.fps;
      this.gen.steps = d.steps ?? this.gen.steps;
      this.gen.guidance = d.guidance ?? this.gen.guidance;
      this.gen.width = d.width ?? this.gen.width;
      this.gen.height = d.height ?? this.gen.height;
      if (!m.capabilities.includes(this.gen.mode)) this.gen.mode = m.capabilities[0];
    },
    frameHint() {
      const m = this.selectedModel; if (!m) return "";
      const base = { "ltx-video": 8, "wan22": 4, "hunyuanvideo": 4, "cogvideox": 8 }[m.family] || 8;
      return `Frames are rounded to ${base}·n+1 for this model. Bigger frames/steps = much longer generation.`;
    },
    onFile(event) {
      const f = event.target.files && event.target.files[0]; if (!f) return;
      if (this.gen.inputUrl) URL.revokeObjectURL(this.gen.inputUrl);
      this.gen.inputFile = f; this.gen.inputName = f.name; this.gen.inputUrl = URL.createObjectURL(f);
    },
    randomPrompt() {
      const list = window.VIDEO_PROMPTS || []; if (!list.length) return;
      this.gen.prompt = list[Math.floor(Math.random() * list.length)];
    },
    async submitGenerate() {
      if (!this.gen.prompt.trim()) return;
      this.gen.submitting = true;
      try {
        let res;
        if (this.gen.mode === "txt2video") {
          res = await fetch("/api/generate/txt2video", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              repo: this.gen.repo, prompt: this.gen.prompt, negative_prompt: this.gen.negativePrompt,
              width: this.gen.width, height: this.gen.height, frames: this.gen.frames,
              fps: this.gen.fps, steps: this.gen.steps, guidance: this.gen.guidance, seed: this.gen.seed,
            }),
          });
        } else {
          if (!this.gen.inputFile) {
            this.pushToast("Pick an input " + (this.gen.mode === "img2video" ? "image" : "video") + " first.", "error");
            this.gen.submitting = false; return;
          }
          const fd = new FormData();
          fd.append("file", this.gen.inputFile);
          fd.append("repo", this.gen.repo); fd.append("mode", this.gen.mode);
          fd.append("prompt", this.gen.prompt); fd.append("negative_prompt", this.gen.negativePrompt);
          fd.append("frames", this.gen.frames); fd.append("fps", this.gen.fps);
          fd.append("steps", this.gen.steps); fd.append("guidance", this.gen.guidance); fd.append("seed", this.gen.seed);
          if (this.gen.mode !== "video2video") { fd.append("width", this.gen.width); fd.append("height", this.gen.height); }
          else { fd.append("strength", this.gen.strength); }
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
    async cancelJob(id) { try { await fetch(`/api/generate/jobs/${id}`, { method: "DELETE" }); } catch (_) {} },
    async clearHistory() { try { await fetch("/api/generate/jobs", { method: "DELETE" }); this.genJobs = []; } catch (_) {} },
    useInGenerate(repo) { this.gen.repo = repo; this.applyModelDefaults(); this.tab = "generate"; },

    // ──────── Downloads ────────
    async startDownload(repo) {
      try {
        const r = await fetch("/api/downloads", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo }),
        });
        if (!r.ok) { const err = await r.json().catch(() => ({})); this.pushToast(err.detail || "Download failed to start", "error"); }
        else { this.pushToast("Download started: " + repo, "info"); this.tab = "downloads"; }
      } catch (e) { this.pushToast("Download error: " + e, "error"); }
    },
    async cancelDownload(id) { try { await fetch(`/api/downloads/${id}`, { method: "DELETE" }); } catch (_) {} },
    async clearDownloads() { try { await fetch("/api/downloads", { method: "DELETE" }); this.downloads = []; } catch (_) {} },
    async revealInFolder(path) {
      if (!path) return;
      try {
        const r = await fetch("/api/reveal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
        if (!r.ok) { const err = await r.json().catch(() => ({})); this.pushToast(err.detail || "Couldn't open in Finder", "error"); }
      } catch (e) { this.pushToast("Couldn't open in Finder: " + e, "error"); }
    },

    // ──────── Settings ────────
    async saveToken() {
      this.settings.busy = true; this.settings.message = "";
      try {
        const s = await (await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hf_token: this.settings.tokenInput }) })).json();
        this.settings.hf_token_set = !!s.hf_token_set; this.settings.hf_token_masked = s.hf_token_masked || "";
        this.settings.tokenInput = ""; this.settings.message = "Saved."; this.settings.messageKind = "success";
      } catch (e) { this.settings.message = "Save failed: " + e; this.settings.messageKind = "error"; }
      finally { this.settings.busy = false; }
    },
    async testToken() {
      this.settings.busy = true; this.settings.message = "Testing…"; this.settings.messageKind = "info";
      try {
        const body = this.settings.tokenInput ? { hf_token: this.settings.tokenInput } : {};
        const r = await fetch("/api/settings/test-hf-token", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const d = await r.json();
        if (r.ok) { this.settings.message = "Valid — " + (d.name || "ok"); this.settings.messageKind = "success"; }
        else { this.settings.message = d.detail || "Invalid token"; this.settings.messageKind = "error"; }
      } catch (e) { this.settings.message = "Test failed: " + e; this.settings.messageKind = "error"; }
      finally { this.settings.busy = false; }
    },

    // ──────── formatters / chip helpers ────────
    // Decimal (SI, ÷/×1000) — NOT binary ÷1024. Must match the catalog's static
    // `size_gb` values (HF's decimal byte counts) and downloads.py's own `/1e9`
    // log line, or the same repo shows two different "GB" numbers: one static
    // on the model card, one live while downloading (same bug class fixed in
    // Voice Studio KH v1.7.2/v1.7.3).
    fmtBytes(n) {
      n = Number(n) || 0;
      const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
      while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
      return n.toFixed(n < 10 ? 2 : 1) + " " + u[i];
    },
    formatGb(gb) { gb = Number(gb) || 0; return gb < 1 ? Math.round(gb * 1000) + " MB" : gb.toFixed(1) + " GB"; },
    cacheChipLabel(s) { return { cached: "cached", partial: "partial", absent: "not downloaded" }[s] || s; },
    cacheChipClass(s) { return { cached: "ok", partial: "warn", absent: "" }[s] || ""; },
    chipExplain(s) {
      return {
        cached: "All files are on disk and ready to generate from.",
        partial: "Some files downloaded; not usable yet. Download again to resume.",
        absent: "No files on disk. Click Download to fetch them.",
      }[s] || "";
    },
    fitChipLabel(fit) {
      if (!fit) return "";
      return { ok: "✓ fits", tight: "⚠ tight", risky: "✗ may not fit", unknown: "? unknown" }[fit.state] || "";
    },
    useCaseIcon(kind) { return { good: "✅", weak: "⚠️", avoid: "❌" }[kind] || "•"; },
    capabilityLabel(c) { return { txt2video: "text → video", img2video: "image → video", video2video: "video → video" }[c] || c; },
    capabilityHint(c) {
      return {
        txt2video: "Generate a clip from a text prompt alone.",
        img2video: "Animate a still image into a clip (first-frame / image-to-video).",
        video2video: "Transform an existing clip guided by your prompt.",
      }[c] || "";
    },

    pushToast(text, kind = "info") {
      const id = ++this._toastSeq;
      this.toasts.push({ id, text, kind });
      setTimeout(() => { this.toasts = this.toasts.filter((t) => t.id !== id); }, 4500);
    },
  };
}
