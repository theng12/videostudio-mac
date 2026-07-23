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
    advancedFiltersOpen: false,

    gen: {
      available: false,
      repo: "",
      mode: "txt2video",
      prompt: "",
      negativePrompt: "",
      frames: 97, fps: 24, steps: 40, guidance: 3.0,
      width: 704, height: 480, seed: -1, strength: 0.7,
      duration: 5, resolution: "", aspectRatio: "",
      inputFile: null, inputUrl: "", inputName: "",
      submitting: false,
    },
    genFilters: { capability: "all", minDuration: 0, resolution: "all" },

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
    autoUpdate: {
      loaded:false, busy:false, message:"", messageKind:"info", state:"idle",
      installed_version:"", latest_version:null, last_checked:null, next_check:null,
      last_update_result:null, defer_reason:null, rollback:null, details:[],
      update_available:false, scheduler:{installed:false}, release_notes_url:"",
      settings:{mode:"off",frequency:"daily",maintenance_hour:6,idle_only:true},
      draft:{mode:"off",frequency:"daily",maintenance_hour:6,idle_only:true},
      dirty:false,
    },
    conn: { bind_port: 47872 },

    // cloud providers + spend guardrails
    providers: [],
    spend: null,
    providerKeyInput: {},
    providerSaving: {},
    providerMsg: {},
    caps: { global: { daily: 0, monthly: 0 } },
    capsProvider: {},
    capsMsg: "", capsMsgKind: "info",

    toasts: [],
    _toastSeq: 0,
    _doneRepos: {},

    // outputs list: per-clip actions + disk management
    deleteArmed: null,       // job.id currently armed for a two-click single delete
    pruneArmed: null,        // prune mode ("keep50" | "old30") armed for a two-click confirm
    outputStats: { bytes: 0, count: 0, loaded: false },
    storagePolicy: { enabled: true, retention_days: 30, max_gb: 80, used_bytes: 0, over_limit: false, loaded: false, busy: false, message: "" },
    memoryPolicy: {
      mode:"performance", default_mode:"performance", idle_seconds:null,
      loaded_pipeline:null, pipeline_idle_seconds:null, next_release_at:null,
      last_release_at:null, last_release_reason:null, release_count:0,
      process_title:"Video Studio Mac", process_title_applied:false,
      loaded:false, busy:false, message:"", messageKind:"info",
      draft:{mode:"performance"}, dirty:false,
    },
    _lastDoneCount: 0,

    // ──────── Generate computed ────────
    get cachedModels() {
      // Cloud models need no download, so keep them visible in Generate even
      // before linking a key; the dropdown disables unlinked providers. Local
      // models must be cached before they can appear here.
      return this.models.filter((m) => m.is_cloud || (m.cache && m.cache.state === "cached"));
    },
    get selectableGenerationModels() {
      return this.generationModels.filter((m) => this.isModelSelectable(m));
    },
    get anyCloudUsable() {
      return this.models.some((m) => m.is_cloud && m.key_set && m.paid_on);
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
    get outputSizeLabel() {
      return this.fmtBytes(this.outputStats.bytes || 0);
    },
    get generationResolutionOptions() {
      const values = new Set();
      for (const m of this.cachedModels) {
        for (const value of (m.resolutions || [])) values.add(value);
        const d = m.video_defaults || {};
        if (d.width && d.height) values.add(`${d.width}×${d.height}`);
      }
      return Array.from(values).sort();
    },
    get generationModels() {
      const f = this.genFilters;
      return this.cachedModels.filter((m) => {
        if (f.capability !== "all" && !(m.capabilities || []).includes(f.capability)) return false;
        const duration = m.is_cloud ? Number(m.max_duration_s || 0) : this.modelDurationSeconds(m);
        if (Number(f.minDuration || 0) > 0 && duration < Number(f.minDuration)) return false;
        if (f.resolution !== "all") {
          const values = new Set(m.resolutions || []);
          const d = m.video_defaults || {};
          if (d.width && d.height) values.add(`${d.width}×${d.height}`);
          if (!values.has(f.resolution)) return false;
        }
        return true;
      });
    },
    get generationModelGroups() {
      const groups = [];
      const local = this.generationModels.filter((m) => !m.is_cloud);
      if (local.length) groups.push({ id: "local", label: "Local · this Mac", models: local });
      const byProvider = {};
      for (const model of this.generationModels.filter((m) => m.is_cloud)) {
        (byProvider[model.provider] ||= []).push(model);
      }
      for (const provider of Object.keys(byProvider).sort((a, b) => this.providerName(a).localeCompare(this.providerName(b)))) {
        groups.push({ id: `cloud-${provider}`, label: `Cloud · ${this.providerName(provider)}`, models: byProvider[provider] });
      }
      return groups;
    },
    get estimatedCloudCost() {
      const m = this.selectedModel;
      if (!m?.is_cloud || !m.price || m.price.usd == null) return null;
      if (m.price.unit === "per_second") return Math.round(Number(m.price.usd) * Number(this.gen.duration || 0) * 10000) / 10000;
      if (m.price.unit === "per_video") return Number(m.price.usd);
      return null;
    },
    get cloudCapBlockMessage() {
      const m = this.selectedModel;
      const estimate = this.estimatedCloudCost;
      if (!m?.is_cloud || estimate == null) return "";
      if (!this.spend) return "Loading spend guardrails…";
      const global = this.spend.global || {};
      const provider = (this.spend.per_provider || {})[m.provider] || {};
      const checks = [
        ["global daily", global.today, global.cap_daily],
        ["global monthly", global.month, global.cap_monthly],
        [`${m.provider} daily`, provider.today, provider.cap_daily],
        [`${m.provider} monthly`, provider.month, provider.cap_monthly],
      ];
      for (const [label, current, cap] of checks) {
        if (Number(cap || 0) > 0 && Number(current || 0) + estimate > Number(cap) + 1e-9) {
          return `${label} cap blocks this job: $${Number(current || 0).toFixed(2)} spent + $${estimate.toFixed(4)} estimate exceeds $${Number(cap).toFixed(2)}.`;
        }
      }
      return "";
    },
    get spendHistoryMax() {
      return Math.max(0.01, ...(this.spend?.daily_history || []).map((d) => Number(d.total || 0)));
    },

    // ──────── lifecycle ────────
    async init() {
      await this.refreshHealth();
      await this.loadSystem();
      await this.loadCatalog();
      await this.loadDiagnostics();
      await this.loadSettings();
      await this.loadAutoUpdate(true);
      this.loadConnectivity();
      this._initRamPlanner();
      this.openDownloadsStream();
      this.openGenerateStream();
      this.refreshOutputStats();
      this.refreshStoragePolicy();
      await this.refreshMemoryPolicy(true, true);
      this.loadProviders().then(() => this.loadSpend());
      setInterval(() => this.refreshHealth(), 8000);
      setInterval(() => this.loadDiagnostics(), 15000);
      setInterval(() => { if (this.tab === "settings") this.loadSpend(); }, 15000);
      setInterval(() => {
        if (this.tab === "settings" || ["checking","updating","restarting","deferred"].includes(this.autoUpdate.state)) this.loadAutoUpdate(true);
        if (this.tab === "settings") this.refreshMemoryPolicy(true);
      }, 5000);
    },

    // ──────── cloud providers + spend ────────
    async loadProviders() {
      try {
        const d = await (await fetch("/api/providers")).json();
        const providers = d.providers || [];
        // Build every provider-keyed object before exposing the provider rows to
        // Alpine. Otherwise x-for can render a row while capsProvider[p.key] is
        // still undefined, which aborts expressions elsewhere in Settings.
        const keyInput = { ...this.providerKeyInput };
        const capsProvider = { ...this.capsProvider };
        for (const p of providers) {
          if (!(p.key in keyInput)) keyInput[p.key] = "";
          if (!(p.key in capsProvider)) capsProvider[p.key] = { daily: 0, monthly: 0 };
        }
        this.providerKeyInput = keyInput;
        this.capsProvider = capsProvider;
        this.providers = providers;
      } catch (_) {}
    },
    async loadSpend() {
      try {
        const d = await (await fetch("/api/spend")).json();
        this.spend = d;
        this.caps.global = { daily: d.caps?.global?.daily || 0, monthly: d.caps?.global?.monthly || 0 };
        const pp = d.caps?.per_provider || {};
        const cp = {};
        for (const p of this.providers) { const v = pp[p.key] || {}; cp[p.key] = { daily: v.daily || 0, monthly: v.monthly || 0 }; }
        this.capsProvider = cp;
      } catch (_) {}
    },
    async saveProviderKey(key) {
      const value = (this.providerKeyInput[key] || "").trim();
      const existing = this.providers.find((p) => p.key === key);
      if (!value) {
        this.providerMsg = { ...this.providerMsg,
          [key]: existing?.key_set ? "Paste a replacement key first." : "Paste an API key first." };
        return;
      }
      this.providerSaving = { ...this.providerSaving, [key]: true };
      this.providerMsg = { ...this.providerMsg, [key]: "Saving…" };
      try {
        const r = await fetch(`/api/providers/${key}/key`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: value }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        this.providers = d.providers || [];
        this.providerKeyInput = { ...this.providerKeyInput, [key]: "" };
        this.providerMsg = { ...this.providerMsg, [key]: "Key saved." };
        this.pushToast(`Saved ${key} key`, "success");
        this.loadCatalog();
      } catch (e) {
        this.providerMsg = { ...this.providerMsg, [key]: "Save failed: " + e };
        this.pushToast("Save failed: " + e, "error");
      } finally {
        this.providerSaving = { ...this.providerSaving, [key]: false };
      }
    },
    async toggleProviderPaid(key, on) {
      this.providerMsg = { ...this.providerMsg, [key]: "Updating…" };
      try {
        const r = await fetch(`/api/providers/${key}/paid`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paid: on }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        this.providers = d.providers || [];
        this.providerMsg = { ...this.providerMsg, [key]: on ? "Paid generation enabled." : "Paid generation disabled." };
        this.loadCatalog();
      } catch (e) {
        this.providerMsg = { ...this.providerMsg, [key]: "Update failed: " + e };
        this.pushToast("Update failed: " + e, "error");
        await this.loadProviders();
      }
    },
    async refreshProvider(key) {
      try {
        const d = await (await fetch(`/api/providers/${key}/refresh`, { method: "POST" })).json();
        this.pushToast(`${key}: ${d.model_count} models`, "info");
        await this.loadProviders(); this.loadCatalog();
      } catch (e) { this.pushToast("Refresh failed: " + e, "error"); }
    },
    async saveCaps() {
      try {
        const body = { global: this.caps.global, per_provider: this.capsProvider };
        const d = await (await fetch("/api/spend/caps", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
        this.spend = d; this.capsMsg = "Saved"; this.capsMsgKind = "success"; this.loadProviders();
        setTimeout(() => { this.capsMsg = ""; }, 2500);
      } catch (e) { this.capsMsg = "Save failed"; this.capsMsgKind = "error"; }
    },
    cloudPriceLabel(m) {
      if (!m.price || m.price.usd == null) return "usage-based";
      return "$" + m.price.usd + (m.price.unit === "per_second" ? "/sec" : "/video");
    },
    cloudStatusLabel(m) {
      return m.status === "deprecated" ? "deprecated" : (m.status === "new" ? "new" : "cloud");
    },
    providerName(key) {
      const linked = this.providers.find((p) => p.key === key)?.name;
      if (linked) return linked;
      const family = Object.values(this.families || {}).find((f) => f.provider === key);
      return String(family?.label || key || "Cloud").replace(/\s*·\s*cloud$/i, "");
    },
    modelSourceLabel(model) {
      return model?.is_cloud ? this.providerName(model.provider) : "Local";
    },
    modelOptionLabel(model) {
      const access = model.is_cloud && !model.key_set ? " · API key required" : "";
      return `${model.label} · ${this.modelSourceLabel(model)}${access}`;
    },
    isModelSelectable(model) {
      return !!model && (!model.is_cloud || !!model.key_set);
    },
    cloudAccessLabel(model) {
      if (!model?.is_cloud) return "Local model";
      if (!model.key_set) return `Add ${this.providerName(model.provider)} API key`;
      if (!model.paid_on) return `Enable ${this.providerName(model.provider)} paid use`;
      return `${this.providerName(model.provider)} ready`;
    },

    async refreshHealth() {
      try {
        const response = await fetch("/api/health");
        const data = response.ok ? await response.json() : {};
        this.health = { ...data, ok: response.ok && data.ok !== false };
      }
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
        this._initFamilyLibrary();
        this._syncDownloadsToModels();
        if (!this.selectableGenerationModels.some((m) => m.repo === this.gen.repo)) {
          this.gen.repo = this.selectableGenerationModels[0]?.repo || "";
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

    async loadAutoUpdate(silent=false) {
      try {
        const r = await fetch("/api/auto-update/status", {cache:"no-store"});
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        this.applyAutoUpdateStatus(data);
      } catch (e) {
        if (!silent) { this.autoUpdate.message=String(e.message||e); this.autoUpdate.messageKind="error"; }
      }
    },
    applyAutoUpdateStatus(data, forceDraft=false) {
      const savedSettings = data.settings ? {...data.settings} : null;
      Object.assign(this.autoUpdate, data, {loaded:true});
      if (savedSettings && (forceDraft || !this.autoUpdate.dirty)) {
        this.autoUpdate.draft = savedSettings;
        this.autoUpdate.dirty = false;
      }
    },
    markAutoUpdateDirty() {
      this.autoUpdate.dirty = true;
      this.autoUpdate.message = "";
      this.autoUpdate.messageKind = "info";
    },
    autoUpdateTime(value) {
      if (!value) return "Not yet";
      const date=new Date(value); return Number.isNaN(date.getTime()) ? "Not yet" : date.toLocaleString();
    },
    async saveAutoUpdate() {
      this.autoUpdate.busy=true; this.autoUpdate.message="Saving and validating the schedule…"; this.autoUpdate.messageKind="info";
      try {
        const r=await fetch("/api/auto-update/settings",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(this.autoUpdate.draft)});
        const data=await r.json(); if(!r.ok) throw new Error(data.detail||`HTTP ${r.status}`);
        this.applyAutoUpdateStatus(data, true);
        this.autoUpdate.message=data.settings.mode==="off"?"Saved. Automatic updates are off and the schedule is unloaded.":"Saved. The updater schedule is installed and verified.";
        this.autoUpdate.messageKind="success";
      } catch(e) { this.autoUpdate.message=String(e.message||e); this.autoUpdate.messageKind="error"; }
      finally { this.autoUpdate.busy=false; }
    },
    async autoUpdateAction(action,body={}) {
      this.autoUpdate.busy=true; this.autoUpdate.message=action==="check"?"Checking safely…":"Starting the update helper…"; this.autoUpdate.messageKind="info";
      try {
        const r=await fetch(`/api/auto-update/${action}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
        const data=await r.json(); if(!r.ok) throw new Error(data.detail||`HTTP ${r.status}`);
        this.applyAutoUpdateStatus(data);
        this.autoUpdate.message=body.after_current?"Queued. The updater will retry when Video Studio is idle.":(action==="check"?"Check started. Status refreshes automatically.":"Update started. This page may reconnect during restart.");
        this.autoUpdate.messageKind="success";
      } catch(e) { this.autoUpdate.message=String(e.message||e); this.autoUpdate.messageKind="error"; }
      finally { this.autoUpdate.busy=false; }
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
        try {
          this.genJobs = (JSON.parse(e.data).jobs || []).slice().reverse();
          // A finished clip just landed on disk — refresh the disk-usage figure.
          const done = this.genJobs.filter((j) => j.state === "done").length;
          if (done !== this._lastDoneCount) { this._lastDoneCount = done; this.refreshOutputStats(); }
        } catch (_) {}
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
      const localModels = this.models.filter((m) => !m.is_cloud);
      const pickHeavy = (pred) => {
        const c = localModels.filter((m) => fits(m) && pred(m));
        return c.length ? c.slice().sort((a, b) => heavy(b) - heavy(a))[0] : null;
      };
      const pickLight = (pred) => {
        const c = localModels.filter((m) => fits(m) && pred(m));
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
        if (!m.is_cloud && f.fitLevel && f.fitLevel !== "all") {
          const st = this.fitFor(m.min_unified_memory_gb).state;
          if (f.fitLevel === "ok" && st !== "ok") return false;
          if (f.fitLevel === "tight" && st !== "tight") return false;
          if (f.fitLevel === "over" && st !== "risky") return false;
        }
        if (q) {
          const useCases = (m.use_cases || []).map((item) => item.text || "").join(" ");
          const hay = [m.label, m.variant_label, m.role, m.repo, m.family_label, m.best_for,
            m.is_cloud ? this.providerName(m.provider) : "local", useCases]
            .filter(Boolean).join(" ").toLowerCase();
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
    get visibleFamilyGroups() {
      return this.familiesWithResults().map((family) => {
        const models = this.filteredModelsByFamily[family.id] || [];
        const capabilities = Array.from(new Set(models.flatMap((m) => m.capabilities || [])));
        return {
          ...family,
          models,
          capabilities,
          cachedCount: models.filter((m) => m.cache?.state === "cached").length,
          minRam: Math.min(...models.map((m) => Number(m.min_unified_memory_gb) || 0)),
          maxRam: Math.max(...models.map((m) => Number(m.min_unified_memory_gb) || 0)),
          minSize: Math.min(...models.map((m) => Number(m.size_gb) || 0)),
          maxSize: Math.max(...models.map((m) => Number(m.size_gb) || 0)),
        };
      });
    },
    get visibleModelLanes() {
      const groups = this.visibleFamilyGroups;
      const lanes = [
        {
          id: "local", eyebrow: "Runs on this Mac", label: "Local models",
          summary: "Downloaded once, rendered privately with this Mac's GPU and unified memory.",
          families: groups.filter((family) => !family.is_cloud),
        },
        {
          id: "cloud", eyebrow: "Runs on a provider", label: "Cloud models",
          summary: "No download or local RAM required. Unlinked providers stay visible but unavailable until an API key is added.",
          families: groups.filter((family) => family.is_cloud),
        },
      ];
      return lanes.filter((lane) => lane.families.length);
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
    _initFamilyLibrary() {
      if (this.modelFilters.collapsedFamilies.size || !this.models.length) return;
      const cached = this.models.find((m) => m.cache?.state === "cached");
      const firstFamily = cached?.family || (this.families["cogvideox"] ? "cogvideox" : this.models[0].family);
      this.modelFilters.collapsedFamilies = new Set(
        Object.keys(this.families).filter((id) => id !== firstFamily),
      );
    },
    isFamilyCollapsed(id) {
      if (this.modelFilters.search.trim() || this.modelFilters.families.has(id)) return false;
      return this.modelFilters.collapsedFamilies.has(id);
    },
    toggleFamilyCollapsed(id) { const s = this.modelFilters.collapsedFamilies; s.has(id) ? s.delete(id) : s.add(id); this.modelFilters.collapsedFamilies = new Set(s); },

    // Family-first Models tab presentation. All values are derived from the
    // catalog contract so adding a model automatically updates comparisons.
    familyStyle(family) { return `--family-accent: ${family.accent || "#59d6c7"}`; },
    familyModeSummary(family) {
      return (family.capabilities || []).map((c) => this.shortCapabilityLabel(c)).join(" · ");
    },
    familyRamSummary(family) {
      return family.minRam === family.maxRam ? `${family.minRam} GB+` : `${family.minRam}–${family.maxRam} GB`;
    },
    familySizeSummary(family) {
      return family.minSize === family.maxSize
        ? this.formatGb(family.minSize)
        : `${this.formatGb(family.minSize)}–${this.formatGb(family.maxSize)}`;
    },
    familyResolutionSummary(family) {
      const values = Array.from(new Set((family.models || []).map((m) => this.modelResolution(m))));
      return values.join(" · ");
    },
    familyDurationSummary(family) {
      const values = (family.models || []).map((m) => this.modelDurationSeconds(m)).filter((v) => v > 0);
      if (!values.length) return "—";
      const min = Math.min(...values); const max = Math.max(...values);
      return min === max ? this.formatDuration(min) : `${this.formatDuration(min)}–${this.formatDuration(max)}`;
    },
    modelResolution(model) {
      if (model.is_cloud) return (model.resolutions || []).join(", ") || "Provider default";
      const d = model.video_defaults || {};
      return d.width && d.height ? `${d.width}×${d.height}` : "Custom";
    },
    modelDurationSeconds(model) {
      if (model.is_cloud) return Number(model.max_duration_s || 0);
      const d = model.video_defaults || {};
      return d.frames && d.fps ? Number(d.frames) / Number(d.fps) : 0;
    },
    formatDuration(seconds) {
      return seconds >= 10 ? `${Math.round(seconds)} sec` : `${seconds.toFixed(1)} sec`;
    },
    spendDayLabel(day) {
      return new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    },
    spendDayTitle(day) {
      const parts = Object.entries(day.providers || {}).map(([provider, usd]) => `${provider}: $${Number(usd).toFixed(4)}`);
      return `${day.day} · $${Number(day.total || 0).toFixed(4)}${parts.length ? " · " + parts.join(" · ") : ""}`;
    },
    modelClipProfile(model) {
      if (model.is_cloud) return `${this.formatDuration(this.modelDurationSeconds(model))} max · provider managed`;
      const d = model.video_defaults || {};
      return `${this.formatDuration(this.modelDurationSeconds(model))} · ${d.frames || "—"} frames · ${d.fps || "—"} fps`;
    },
    modelRuntimeLabel(model) {
      if (model.is_cloud) return `${this.providerName(model.provider)} · Cloud API`;
      return model.engine?.startsWith("mlx")
        ? "Local · Native MLX · Apple Silicon"
        : "Local · PyTorch · MPS";
    },
    modelRowClass(model) {
      return [model.cache?.state || "absent", model.is_cloud ? "cloud" : "local",
        model.engine?.startsWith("mlx") ? "mlx" : "",
        model.is_cloud && !model.key_set ? "cloud-unlinked" : "",
        !model.is_cloud && this.isModelReady(model.repo) ? "ready" : ""].filter(Boolean).join(" ");
    },
    shortCapabilityLabel(c) {
      return { txt2video: "Text", img2video: "Image", video2video: "Video" }[c] || c;
    },

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
    modelFrameBase(model) {
      return { "lance-mlx": 4, "ltx-video": 8, "wan22": 4, "hunyuanvideo": 4, "cogvideox": 8 }[model?.family] || 8;
    },
    jobStageLabel(job) {
      if (job.state === "queued" && job.queue_position) return `queued #${job.queue_position}`;
      return ({ preparing: "preparing", loading: "loading model", generating: "generating frames",
        encoding: "encoding video", provider: "provider processing", cancelling: "cancelling", interrupted: "interrupted",
        completed: "completed", failed: "failed", cancelled: "cancelled" })[job.stage] || job.state;
    },
    onModelChange() { this.applyModelDefaults(); },
    onGenerationFilterChange() {
      if (!this.selectableGenerationModels.some((m) => m.repo === this.gen.repo)) {
        this.gen.repo = this.selectableGenerationModels[0]?.repo || "";
        this.applyModelDefaults();
      }
    },
    applyModelDefaults() {
      const m = this.selectedModel; if (!m) return;
      const d = m.video_defaults || {};
      this.gen.frames = d.frames ?? this.gen.frames;
      this.gen.fps = d.fps ?? this.gen.fps;
      this.gen.steps = d.steps ?? this.gen.steps;
      this.gen.guidance = d.guidance ?? this.gen.guidance;
      this.gen.width = d.width ?? this.gen.width;
      this.gen.height = d.height ?? this.gen.height;
      if (m.is_cloud) {
        this.gen.duration = Math.min(Number(m.max_duration_s || 5), 5);
        this.gen.resolution = (m.resolutions || [])[0] || "";
        this.gen.aspectRatio = (m.aspect_ratios || [])[0] || "";
      }
      if (!m.capabilities.includes(this.gen.mode)) this.gen.mode = m.capabilities[0];
    },
    frameHint() {
      const m = this.selectedModel; if (!m) return "";
      if (m.is_cloud) {
        if (this.estimatedCloudCost == null) return "This cloud model has no verified price and cannot be submitted yet.";
        const reconciliation = m.price?.unit === "per_second"
          ? "Final cost is reconciled from the downloaded clip duration."
          : "This model uses a fixed per-video price.";
        return `Estimated provider cost: $${this.estimatedCloudCost.toFixed(4)}. ${reconciliation}`;
      }
      const base = this.modelFrameBase(m);
      return `Frames are rounded to ${base}·n+1 for this model. Bigger frames/steps = much longer generation.`;
    },
    get canSubmit() {
      if (this.gen.submitting || !this.gen.repo || !this.gen.prompt.trim()) return false;
      if (this.selectedModel?.is_cloud) {
        if (!this.selectedModel.key_set || !this.selectedModel.paid_on || this.estimatedCloudCost == null || this.cloudCapBlockMessage) return false;
      } else if (!this.isModelReady(this.gen.repo)) return false;
      if (this.gen.mode !== "txt2video" && !this.gen.inputFile) return false;
      return true;
    },
    get submitHint() {
      if (!this.gen.repo) return "Choose a downloaded model.";
      if (this.selectedModel?.is_cloud) {
        if (!this.selectedModel.key_set) return "Add this provider's API key in Settings.";
        if (!this.selectedModel.paid_on) return "Enable paid generation for this provider in Settings.";
        if (this.estimatedCloudCost == null) return "This cloud model has no verified price and is blocked for safety.";
        if (this.cloudCapBlockMessage) return this.cloudCapBlockMessage;
      } else if (!this.isModelReady(this.gen.repo)) return "This model's video pipeline is not ready. Run Update or reinstall Generation.";
      if (!this.gen.prompt.trim()) return "Enter a prompt to continue.";
      if (this.gen.mode !== "txt2video" && !this.gen.inputFile) {
        return "Choose an input " + (this.gen.mode === "img2video" ? "image." : "video.");
      }
      return "";
    },
    onFile(event) {
      const f = event.target.files && event.target.files[0]; if (!f) return;
      const wantsImage = this.gen.mode === "img2video";
      const validType = wantsImage ? f.type.startsWith("image/") : f.type.startsWith("video/");
      const maxBytes = wantsImage ? 20 * 1024 * 1024 : 500 * 1024 * 1024;
      if (!validType) {
        this.pushToast("Choose a valid " + (wantsImage ? "image" : "video") + " file.", "error");
        event.target.value = "";
        return;
      }
      if (f.size > maxBytes) {
        this.pushToast((wantsImage ? "Images must be 20 MB or smaller." : "Videos must be 500 MB or smaller."), "error");
        event.target.value = "";
        return;
      }
      if (this.gen.inputUrl) URL.revokeObjectURL(this.gen.inputUrl);
      this.gen.inputFile = f; this.gen.inputName = f.name; this.gen.inputUrl = URL.createObjectURL(f);
    },
    randomPrompt() {
      const list = window.VIDEO_PROMPTS || []; if (!list.length) return;
      this.gen.prompt = list[Math.floor(Math.random() * list.length)];
    },
    async submitGenerate() {
      if (!this.canSubmit) return;
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
              duration: this.selectedModel?.is_cloud ? this.gen.duration : null,
              resolution: this.selectedModel?.is_cloud ? (this.gen.resolution || null) : null,
              aspect_ratio: this.selectedModel?.is_cloud ? (this.gen.aspectRatio || null) : null,
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
          if (this.selectedModel?.is_cloud) {
            fd.append("duration", this.gen.duration);
            if (this.gen.resolution) fd.append("resolution", this.gen.resolution);
            if (this.gen.aspectRatio) fd.append("aspect_ratio", this.gen.aspectRatio);
          }
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
    async repairJob(id) {
      try {
        const r = await fetch(`/api/generate/jobs/${encodeURIComponent(id)}/repair`, { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        this.pushToast(d.message || "Saved provider task repaired.", "success");
      } catch (e) {
        this.pushToast("Repair failed: " + e, "error");
      }
    },
    reuseJob(job) {
      const p = job.params || {};
      if (p.repo && this.models.some((m) => m.repo === p.repo)) this.gen.repo = p.repo;
      this.applyModelDefaults();
      this.gen.mode = p.mode || job.mode || this.gen.mode;
      this.gen.prompt = p.prompt || "";
      this.gen.negativePrompt = p.negative_prompt || "";
      for (const [target, source] of [["frames","frames"],["fps","fps"],["steps","steps"],
        ["guidance","guidance"],["width","width"],["height","height"],["seed","seed"],
        ["strength","strength"],["duration","duration"],["resolution","resolution"],
        ["aspectRatio","aspect_ratio"]]) {
        if (p[source] !== null && p[source] !== undefined) this.gen[target] = p[source];
      }
      if (job.resolved_seed !== null && job.resolved_seed !== undefined) this.gen.seed = job.resolved_seed;
      this.tab = "generate";
      if (this.gen.mode !== "txt2video") {
        this.gen.inputFile = null; this.gen.inputName = "";
        if (this.gen.inputUrl) URL.revokeObjectURL(this.gen.inputUrl);
        this.gen.inputUrl = "";
        this.pushToast("Settings restored. Choose the source file again before generating.", "info");
      } else {
        this.pushToast("Generation settings restored.", "success");
      }
    },
    async clearHistory() { try { await fetch("/api/generate/jobs", { method: "DELETE" }); this.genJobs = []; } catch (_) {} },
    /** Open the outputs folder (all generated clips) in Finder, derived from a job's absolute path. */
    openOutputsFolder() {
      const withPath = (this.genJobs || []).find(j => j.output_path);
      if (withPath && withPath.output_path) {
        this.revealInFolder(withPath.output_path.replace(/[/\\][^/\\]+$/, ""));
      } else {
        this.pushToast("No clips yet. Generate one first, then this opens the output folder.", "info");
      }
    },
    useInGenerate(repo) { this.gen.repo = repo; this.applyModelDefaults(); this.tab = "generate"; },

    /** Delete one finished clip (removes it from history AND deletes the .mp4).
     *  Two-click confirm — first click arms this row, second deletes. */
    deleteGeneration(job) {
      if (this.deleteArmed !== job.id) {
        this.deleteArmed = job.id;
        clearTimeout(this._deleteArmTimer);
        this._deleteArmTimer = setTimeout(() => { this.deleteArmed = null; }, 3000);
        return;
      }
      clearTimeout(this._deleteArmTimer);
      this.deleteArmed = null;
      this._doDeleteGeneration(job);
    },
    async _doDeleteGeneration(job) {
      try {
        const r = await fetch("/api/generate/history/" + encodeURIComponent(job.id), { method: "DELETE" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.genJobs = (this.genJobs || []).filter((j) => j.id !== job.id);
        this.refreshOutputStats();
        this.pushToast("Clip deleted.", "info");
      } catch (e) {
        this.pushToast("Couldn't delete — run Update once from the Pinokio sidebar for the latest backend.", "error");
      }
    },

    // ──────── outputs folder disk usage ────────
    async refreshOutputStats() {
      try {
        const r = await fetch("/api/output/stats");
        if (!r.ok) return;                         // endpoint not live until next Update
        const d = await r.json();
        this.outputStats = { bytes: d.bytes || 0, count: d.count || 0, loaded: true };
      } catch (_) { /* keep last */ }
    },
    async refreshStoragePolicy() {
      try {
        const r = await fetch("/api/storage-policy");
        if (!r.ok) return;
        this.storagePolicy = { ...this.storagePolicy, ...(await r.json()), loaded: true, busy: false };
      } catch (_) { /* keep last */ }
    },
    async saveStoragePolicy() {
      this.storagePolicy.busy = true; this.storagePolicy.message = "Saving policy…";
      try {
        const r = await fetch("/api/storage-policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !!this.storagePolicy.enabled, retention_days: Number(this.storagePolicy.retention_days), max_gb: Number(this.storagePolicy.max_gb) }) });
        if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
        const d = await r.json();
        this.storagePolicy = { ...this.storagePolicy, ...d, loaded: true, busy: false, message: "Saved. This Mac will enforce the policy automatically." };
        this.pushToast(`Storage policy saved · ${d.retention_days} days · ${d.max_gb} GB cap`, "success");
      } catch (e) {
        this.storagePolicy.busy = false; this.storagePolicy.message = String(e);
        this.pushToast("Couldn't save storage policy: " + e, "error");
      }
    },
    async cleanStoragePolicyNow() {
      this.storagePolicy.busy = true; this.storagePolicy.message = "Checking completed clips…";
      try {
        const r = await fetch("/api/storage-policy/cleanup", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
        const d = await r.json();
        this.storagePolicy = { ...this.storagePolicy, ...d, loaded: true, busy: false, message: `Cleanup complete · ${d.deleted || 0} removed · ${this.fmtBytes(d.freed_bytes || 0)} freed.` };
        await this.refreshOutputStats();
        this.pushToast(`Cleanup complete · ${d.deleted || 0} clip${d.deleted === 1 ? "" : "s"} removed`, "success");
      } catch (e) {
        this.storagePolicy.busy = false; this.storagePolicy.message = String(e);
        this.pushToast("Couldn't clean video outputs: " + e, "error");
      }
    },
    async refreshMemoryPolicy(silent=false, forceDraft=false) {
      try {
        const r=await fetch("/api/memory-policy",{cache:"no-store"});
        const d=await r.json(); if(!r.ok) throw new Error(d.detail||`HTTP ${r.status}`);
        const saved=d.mode;
        Object.assign(this.memoryPolicy,d,{loaded:true});
        if(forceDraft || !this.memoryPolicy.dirty){this.memoryPolicy.draft={mode:saved};this.memoryPolicy.dirty=false;}
      } catch(e){if(!silent){this.memoryPolicy.message=String(e.message||e);this.memoryPolicy.messageKind="error";}}
    },
    markMemoryPolicyDirty(){this.memoryPolicy.dirty=true;this.memoryPolicy.message="";this.memoryPolicy.messageKind="info";},
    memoryPolicyTime(value){if(!value)return "Not scheduled";const n=Number(value);const d=new Date(n<1e12?n*1000:n);return Number.isNaN(d.getTime())?"Not scheduled":d.toLocaleString();},
    memoryPipelineLabel(){const p=this.memoryPolicy.loaded_pipeline;return Array.isArray(p)&&p.length?String(p[0]).split("/").pop()+" · "+p[1]:"None loaded";},
    async saveMemoryPolicy(){
      this.memoryPolicy.busy=true;this.memoryPolicy.message="Saving memory mode…";this.memoryPolicy.messageKind="info";
      try{
        const r=await fetch("/api/memory-policy",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(this.memoryPolicy.draft)});
        const d=await r.json();if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
        Object.assign(this.memoryPolicy,d,{loaded:true,draft:{mode:d.mode},dirty:false,message:"Memory mode saved.",messageKind:"success"});
      }catch(e){this.memoryPolicy.message=String(e.message||e);this.memoryPolicy.messageKind="error";}
      finally{this.memoryPolicy.busy=false;}
    },
    async releaseMemory(){
      this.memoryPolicy.busy=true;this.memoryPolicy.message="Releasing local video memory…";this.memoryPolicy.messageKind="info";
      try{
        const r=await fetch("/api/memory/release",{method:"POST"});
        const d=await r.json();if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
        Object.assign(this.memoryPolicy,d,{loaded:true,message:d.last_release_details?.released?"Local video pipeline unloaded and accelerator caches cleared.":"Allocator caches cleared; no local pipeline was loaded.",messageKind:"success"});
        this.pushToast(this.memoryPolicy.message,"success");
      }catch(e){this.memoryPolicy.message=String(e.message||e);this.memoryPolicy.messageKind="error";this.pushToast(this.memoryPolicy.message,"error");}
      finally{this.memoryPolicy.busy=false;}
    },
    /** mode: "keep50" keeps the newest 50; "old30" deletes clips older than 30 days. */
    async pruneOutputs(mode) {
      const body = mode === "old30" ? { older_than_days: 30 } : { keep_last: 50 };
      if (this.pruneArmed !== mode) {
        this.pruneArmed = mode;
        clearTimeout(this._pruneArmTimer);
        this._pruneArmTimer = setTimeout(() => { this.pruneArmed = null; }, 3000);
        return;
      }
      clearTimeout(this._pruneArmTimer);
      this.pruneArmed = null;
      try {
        const r = await fetch("/api/output/prune", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        await this.refreshOutputStats();
        this.pushToast(`Pruned ${d.deleted} clip${d.deleted === 1 ? "" : "s"} (${this.fmtBytes(d.freed_bytes || 0)} freed).`, "info");
      } catch (e) {
        this.pushToast("Couldn't prune — run Update once from the Pinokio sidebar for the latest backend.", "error");
      }
    },

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
