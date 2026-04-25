// ═══════════════════════════════════════════════════════════════
// pages/settings.js — Export / Import configuration
// ═══════════════════════════════════════════════════════════════

const Settings = {
  selectedFile: null,
  buildInfo: null,

  async init() {
    await this.loadBuildInfo();
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));
    this.refreshSpoofPreview();
    // Redraw spoof-pool preview whenever min/max change
    document.querySelectorAll(
      '[data-config="browser.spoof_chrome_min"], ' +
      '[data-config="browser.spoof_chrome_max"]'
    ).forEach(el => {
      el.addEventListener("input",  () => this.refreshSpoofPreview());
      el.addEventListener("change", () => this.refreshSpoofPreview());
    });

    // Blocking card — save button + live counter
    this.refreshBlockStatus();
    document
      .querySelectorAll('[data-config^="browser.block_"]')
      .forEach(el => {
        el.addEventListener("input",  () => this.refreshBlockStatus());
        el.addEventListener("change", () => this.refreshBlockStatus());
      });
    const saveBtn = document.getElementById("btn-save-blocking");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        const orig = saveBtn.textContent;
        saveBtn.textContent = "⏳ Saving…";
        try {
          // Flush the debounced save right now — don't wait the full timeout
          await saveConfig();
          toast("✓ Blocking rules saved. Stop and restart the monitor to apply.");
        } catch (e) {
          toast("Save failed: " + e.message, true);
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = orig;
        }
      });
    }

    this.bindExport();
    this.bindImport();
    this.bindDangerZone();
  },

  /** Danger zone — Reset stats counters (was previously on Overview).
   *  Wipes historical tables but keeps every config row. */
  bindDangerZone() {
    const btn = document.getElementById("btn-reset-stats");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Reset stats counters?",
        message:
          "This wipes events, runs, traffic, action history, logs, and warmup "
          + "history. Profiles, scripts, proxies, vault, and all settings are "
          + "preserved.\n\nThis cannot be undone. Continue?",
        confirmText: "Reset stats",
        confirmStyle: "danger",
      });
      if (!ok) return;
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "⏳ Resetting…";
      try {
        await api("/api/stats/reset", { method: "POST" });
        toast("✓ Stats counters reset");
      } catch (e) {
        toast("Reset failed: " + e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  },

  /** Live count of how many URL patterns would be blocked with the
   *  current settings. Updates on every toggle change. */
  refreshBlockStatus() {
    // Mirror of backend buckets — just for UI count. If backend adds
    // new ones, update here too (or refactor to a /api/blocked_urls/count).
    const buckets = {
      "browser.block_youtube_video":      4,
      "browser.block_google_images":      3,
      "browser.block_google_maps_tiles":  5,
      "browser.block_fonts":              3,
      "browser.block_analytics":          5,
      "browser.block_social_widgets":     5,
      "browser.block_video_everywhere":   5,
    };
    let total = 0;
    let enabledBuckets = 0;
    for (const [key, count] of Object.entries(buckets)) {
      const el = document.querySelector(`[data-config="${key}"]`);
      if (el?.checked) { total += count; enabledBuckets++; }
    }
    const customEl = document.querySelector(
      '[data-config="browser.block_custom_patterns"]'
    );
    const customCount = customEl
      ? customEl.value.split("\n").map(s => s.trim()).filter(Boolean).length
      : 0;
    total += customCount;

    const status = document.getElementById("block-status");
    if (!status) return;
    if (total === 0) {
      status.textContent = "No blocking rules active";
      status.className = "block-status block-status-off";
    } else {
      const parts = [];
      if (enabledBuckets) parts.push(`${enabledBuckets} bucket${enabledBuckets === 1 ? "" : "s"}`);
      if (customCount)    parts.push(`${customCount} custom`);
      status.textContent =
        `${total} URL pattern${total === 1 ? "" : "s"} will be blocked  ·  ${parts.join(" + ")}`;
      status.className = "block-status block-status-on";
    }
  },

  async loadBuildInfo() {
    try {
      const s = await api("/api/stats");
      this.buildInfo = s?.build_info || {};
      const eng = $("#bi-engine");
      const spf = $("#bi-spoof");
      if (eng) {
        eng.textContent = this.buildInfo.chromium_build_full
          || this.buildInfo.chromium_build
          || "—";
      }
      if (spf) {
        const pool = this.buildInfo.chrome_pool || [];
        if (pool.length) {
          const lo = pool[pool.length - 1];
          const hi = pool[0];
          spf.textContent = lo === hi ? `${hi}` : `${lo} – ${hi}`;
        } else {
          spf.textContent = "—";
        }
      }
    } catch (e) {
      console.warn("Build info load failed:", e);
    }
  },

  refreshSpoofPreview() {
    const preview = $("#spoof-pool-preview");
    if (!preview) return;
    const pool = (this.buildInfo?.chrome_pool_full || []);
    if (!pool.length) { preview.textContent = "—"; return; }

    // Read current bounds from the input fields
    const minEl = document.querySelector('[data-config="browser.spoof_chrome_min"]');
    const maxEl = document.querySelector('[data-config="browser.spoof_chrome_max"]');
    const lo = minEl && minEl.value !== "" ? parseInt(minEl.value, 10) : null;
    const hi = maxEl && maxEl.value !== "" ? parseInt(maxEl.value, 10) : null;

    // Filter pool by the bounds
    const filtered = pool.filter(v => {
      const major = parseInt(v.split(".")[0], 10);
      if (lo !== null && major < lo) return false;
      if (hi !== null && major > hi) return false;
      return true;
    });

    if (!filtered.length) {
      preview.innerHTML =
        `<span style="color: #f87171;">No versions in range — revert to pool default</span>`;
    } else {
      preview.textContent = filtered.join(", ");
    }
  },

  // ── Export ───────────────────────────────────────────────────

  bindExport() {
    const btn = document.getElementById("btn-export-config");
    if (!btn) return;

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = "⏳ Preparing…";

      try {
        // Hit the endpoint — server returns Content-Disposition attachment.
        // We can't use the normal api() helper because we need the raw
        // Response object (headers, blob) — it'll just serialise to JSON.
        const res = await fetch("/api/export-config");
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || `HTTP ${res.status}`);
        }

        // Derive filename from Content-Disposition header; fall back to
        // a date-stamped default if the server didn't send one.
        const cd = res.headers.get("Content-Disposition") || "";
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match
          ? match[1]
          : `ghost-shell-config-${Date.now()}.json`;

        // Trigger browser download
        const blob = await res.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement("a");
        a.href     = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);

        toast(`✓ Downloaded ${filename}`);
      } catch (e) {
        toast("Export failed: " + e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    });
  },

  // ── Import ───────────────────────────────────────────────────

  bindImport() {
    const fileInput = document.getElementById("import-file-input");
    const info      = document.getElementById("import-file-info");
    const btn       = document.getElementById("btn-import-config");

    if (!fileInput || !btn) return;

    fileInput.addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (!file) {
        this.selectedFile = null;
        info.textContent = "No file selected";
        btn.disabled = true;
        return;
      }

      // Quick sanity — size cap + suffix check. Bundles are small
      // (usually <200 KB); anything over 5 MB is suspicious.
      if (file.size > 5 * 1024 * 1024) {
        toast("File is too large (>5 MB)", true);
        this.selectedFile = null;
        btn.disabled = true;
        return;
      }
      if (!file.name.toLowerCase().endsWith(".json")) {
        toast("File must have a .json extension", true);
        this.selectedFile = null;
        btn.disabled = true;
        return;
      }

      this.selectedFile = file;
      info.innerHTML = `
        <strong>${escapeHtml(file.name)}</strong>
        <span class="muted"> · ${(file.size / 1024).toFixed(1)} KB</span>
      `;
      btn.disabled = false;
    });

    btn.addEventListener("click", () => this.doImport());
  },

  async doImport() {
    if (!this.selectedFile) return;

    const mode = document.querySelector('input[name="import-mode"]:checked')?.value
                 || "merge";

    // Danger confirmation for Replace mode
    if (mode === "replace") {
      const ok = await confirmDialog({
        title: "Replace all configuration?",
        message:
          "This will DELETE every dashboard setting, profile definition, "
          + "and action pipeline on this installation, then load the bundle "
          + "in their place.\n\nMachine-local keys (proxy rotation counters, "
          + "first-run timestamp) are preserved.\n\nThis cannot be undone. "
          + "Continue?",
        confirmText: "Replace everything",
        confirmStyle: "danger",
      });
      if (!ok) return;
    }

    const btn = document.getElementById("btn-import-config");
    const resultDiv = document.getElementById("import-result");
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = "⏳ Importing…";
    resultDiv.style.display = "none";

    try {
      // Parse JSON in the browser so we can show a nice error before
      // the server even sees malformed data.
      const text = await this.selectedFile.text();
      let bundle;
      try {
        bundle = JSON.parse(text);
      } catch (e) {
        throw new Error("File is not valid JSON: " + e.message);
      }

      // Spot-check before uploading
      if (typeof bundle !== "object" || bundle === null) {
        throw new Error("Bundle root must be an object");
      }
      if (!bundle.format_version) {
        throw new Error("Missing format_version — not a Ghost Shell bundle");
      }

      const resp = await api("/api/import-config", {
        method: "POST",
        body: JSON.stringify({ bundle, mode }),
      });

      if (resp.error) throw new Error(resp.error);

      const imp = resp.imported || {};
      resultDiv.className = "settings-import-result settings-import-ok";
      resultDiv.innerHTML = `
        ✓ Imported successfully (mode: <code>${escapeHtml(resp.mode || mode)}</code>)
        <div class="settings-import-details">
          <span>${imp.config_keys ?? 0} config keys</span>
          <span>${imp.profiles ?? 0} profiles</span>
          <span>${imp.pipelines ?? 0} pipelines</span>
        </div>
        <div class="muted" style="margin-top: 10px; font-size: 11px;">
          Reloading pages to pick up new values in a moment…
        </div>
      `;
      resultDiv.style.display = "block";

      toast("✓ Configuration imported");

      // Nuke the cached config so other pages refetch it
      if (typeof configCache !== "undefined") {
        configCache = null;
      }

      // Give the user a beat to see the result, then reload to fully
      // refresh every page's state (sidebar, pipelines, search config
      // etc all read through the already-cached config object).
      setTimeout(() => window.location.reload(), 2200);

    } catch (e) {
      resultDiv.className = "settings-import-result settings-import-err";
      resultDiv.innerHTML = `✗ Import failed: ${escapeHtml(e.message)}`;
      resultDiv.style.display = "block";
      toast("Import failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  },
};
