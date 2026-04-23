// ═══════════════════════════════════════════════════════════════
// pages/profiles.js — Dolphin-style profile list
// - Per-row context menu (⋮) with Start / Stop / Delete / Set active
// - Per-row Start button
// - Column picker to show/hide columns
// - Search box
// ═══════════════════════════════════════════════════════════════

const Profiles = {
  // Available columns (rendered in order)
  ALL_COLUMNS: [
    { key: "name",        label: "Name",       default: true },
    { key: "status",      label: "Status",     default: true },
    { key: "template",    label: "Template",   default: true },
    { key: "searches24h", label: "Searches 24h", default: true },
    { key: "captchas24h", label: "Captchas 24h", default: true },
    { key: "selfcheck",   label: "Self-check", default: true },
    { key: "fingerprint", label: "Fingerprint", default: false },
    { key: "languages",   label: "Languages",  default: false },
    { key: "lastRun",     label: "Last run",   default: true },
    { key: "actions",     label: "Actions",    default: true },
  ],

  visibleColumns: null,
  allProfiles: [],
  searchFilter: "",

  async init() {
    // Load visible columns preference from localStorage
    try {
      const stored = localStorage.getItem("profiles.visible_columns");
      this.visibleColumns = stored
        ? JSON.parse(stored)
        : this.ALL_COLUMNS.filter(c => c.default).map(c => c.key);
    } catch {
      this.visibleColumns = this.ALL_COLUMNS.filter(c => c.default).map(c => c.key);
    }

    $("#reload-profiles-btn").addEventListener("click", () => this.reload());
    $("#btn-create-profile").addEventListener("click", () => CreateProfileModal.open());
    $("#column-picker-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const dd = $("#column-picker-dropdown");
      dd.style.display = dd.style.display === "none" ? "block" : "none";
    });
    document.addEventListener("click", () => {
      const dd = $("#column-picker-dropdown");
      if (dd) dd.style.display = "none";
    });
    $("#profiles-search").addEventListener("input", (e) => {
      this.searchFilter = e.target.value.toLowerCase();
      this.renderTable();
    });

    this.renderColumnPicker();
    await this.reload();

    // Start polling run status — flips Start ↔ Stop on the row where
    // the running profile lives. Cheap GET every 2s.
    if (this._statusTimer) clearInterval(this._statusTimer);
    await this.refreshRunStatus();   // first update immediately
    this._statusTimer = setInterval(() => this.refreshRunStatus(), 2000);
  },

  /** Called by the router when the user leaves this page. */
  teardown() {
    if (this._statusTimer) {
      clearInterval(this._statusTimer);
      this._statusTimer = null;
    }
  },

  async reload() {
    try {
      this.allProfiles = await api("/api/profiles");
      $("#profiles-count").textContent = this.allProfiles.length;
      this.renderTable();
    } catch (e) {
      toast("Failed to load profiles: " + e.message, true);
    }
  },

  renderColumnPicker() {
    const dd = $("#column-picker-dropdown");
    dd.innerHTML = this.ALL_COLUMNS.map(col => `
      <label>
        <input type="checkbox" data-col="${col.key}"
               ${this.visibleColumns.includes(col.key) ? "checked" : ""}>
        ${escapeHtml(col.label)}
      </label>
    `).join("");

    dd.querySelectorAll("input[type=checkbox]").forEach(input => {
      input.addEventListener("click", (e) => e.stopPropagation());
      input.addEventListener("change", (e) => {
        const key = e.target.dataset.col;
        if (e.target.checked) {
          if (!this.visibleColumns.includes(key)) this.visibleColumns.push(key);
        } else {
          this.visibleColumns = this.visibleColumns.filter(k => k !== key);
        }
        localStorage.setItem("profiles.visible_columns", JSON.stringify(this.visibleColumns));
        this.renderTable();
      });
    });
  },

  renderTable() {
    const visibleCols = this.ALL_COLUMNS.filter(c => this.visibleColumns.includes(c.key));

    // Head
    const thead = $("#profiles-thead");
    thead.innerHTML = "<tr>" +
      visibleCols.map(c => `<th>${escapeHtml(c.label)}</th>`).join("") +
      "</tr>";

    // Rows
    const filtered = this.allProfiles.filter(p =>
      !this.searchFilter || p.name.toLowerCase().includes(this.searchFilter)
    );

    const tbody = $("#profiles-tbody");
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="${visibleCols.length}" class="empty-state">
        No profiles found
      </td></tr>`;
      return;
    }

    tbody.innerHTML = filtered.map(p => "<tr>" +
      visibleCols.map(c => `<td>${this.renderCell(p, c.key)}</td>`).join("") +
      "</tr>").join("");
  },

  renderCell(p, key) {
    switch (key) {
      case "name":
        return `<strong>${escapeHtml(p.name)}</strong>`;
      case "status":
        return `<span class="pill pill-${p.status}">${p.status}</span>`;
      case "template":
        return `<span class="muted">${escapeHtml(p.fingerprint?.template || "—")}</span>`;
      case "searches24h":
        return p.searches_24h ?? 0;
      case "captchas24h":
        return p.captchas_24h ?? 0;
      case "selfcheck":
        return p.selfcheck
          ? `<span class="muted">${p.selfcheck.passed}/${p.selfcheck.total}</span>`
          : `<span class="muted">—</span>`;
      case "fingerprint":
        return `<span class="muted" style="font-size:11px;">${
          escapeHtml(p.fingerprint?.timestamp || "—")}</span>`;
      case "languages":
        return `<span class="muted">${
          Array.isArray(p.fingerprint?.languages)
            ? p.fingerprint.languages.slice(0, 2).join(", ")
            : "—"}</span>`;
      case "lastRun": {
        if (!p.last_run_at) return `<span class="muted">never</span>`;
        // If this profile is the one currently running, show live marker
        const running = this._runStatus?.is_running
                        && this._runStatus?.profile_name === p.name;
        if (running) {
          return `<span class="run-status-live">
            <span class="run-dot"></span> running now
          </span>`;
        }
        const cls = p.last_run_status === "failed" ? "status-failed"
                  : p.last_run_status === "running" ? "status-running"
                  : "";
        return `<span class="last-run ${cls}" title="${escapeHtml(p.last_run_at)}">
          ${escapeHtml(timeAgo(p.last_run_at))}
        </span>`;
      }
      case "actions": {
        // If a run is in progress AND it's for THIS profile, show Stop
        // instead of Start. The runStatus cache is refreshed by
        // runner.js every 3s — we just read it here.
        const running = !!(this._runStatus && this._runStatus.is_running);
        const runningThis = running && this._runStatus.profile_name === p.name;
        const isDefault = this._defaultProfileName === p.name;

        // Show a subtle marker if this is the default profile
        const defaultMark = isDefault
          ? `<span class="profile-default-star"
                   title="Default profile — the sidebar Start button launches this">★</span>`
          : "";

        let mainBtn;
        if (runningThis) {
          mainBtn = `<button class="profile-row-btn stop"
                             onclick="Profiles.stopThis('${escapeHtml(p.name)}')">
                       ■ Stop
                     </button>`;
        } else if (running) {
          // Another profile is running. Disable start on this row —
          // the runner is single-slot.
          mainBtn = `<button class="profile-row-btn start" disabled
                             title="Another profile (${escapeHtml(this._runStatus.profile_name || '?')}) is running. Stop it first.">
                       ▶ Start
                     </button>`;
        } else {
          mainBtn = `<button class="profile-row-btn start"
                             onclick="Profiles.startProfile('${escapeHtml(p.name)}')">
                       ▶ Start
                     </button>`;
        }

        return `
          <div class="profile-row-actions">
            ${defaultMark}
            ${mainBtn}
            <button class="profile-menu-btn"
                    onclick="Profiles.showMenu(event, '${escapeHtml(p.name)}')">⋮</button>
          </div>
        `;
      }
      default:
        return "—";
    }
  },

  async startProfile(name) {
    // First set as active in config, then start
    try {
      configCache.browser = configCache.browser || {};
      configCache.browser.profile_name = name;
      await api("/api/config", {
        method: "POST",
        body: JSON.stringify(configCache),
      });
      await startRun();
      // Immediately bump status — don't wait for the 3s poll tick so
      // the user sees the Start button flip to Stop right away.
      setTimeout(() => this.refreshRunStatus(), 500);
    } catch (e) {
      toast("Failed to start: " + e.message, true);
    }
  },

  async stopThis(name) {
    // Use the shared stopRun() — it has the confirm dialog + kill logic.
    await stopRun();
    setTimeout(() => this.refreshRunStatus(), 500);
  },

  /** Pull the latest run status + default profile name, then re-render
   *  the Actions column so Start ↔ Stop reflects reality. Called on a
   *  timer while the user is looking at the Profiles page. */
  async refreshRunStatus() {
    try {
      const status = await api("/api/run/status");
      this._runStatus = status;
      // Default profile is read from the already-cached config — no need
      // to hit the backend every 2s. setActive() below mutates the cache
      // in place, so this stays in sync with UI actions.
      this._defaultProfileName = configCache?.browser?.profile_name || null;
      // Light refresh — only rebuild if there's a meaningful change,
      // to avoid flashing the table every 3s.
      const snapshot = `${status.is_running}|${status.profile_name}|${this._defaultProfileName}`;
      if (snapshot !== this._lastStatusSnapshot) {
        this._lastStatusSnapshot = snapshot;
        this.renderTable();
      }
    } catch {
      // Endpoint might be briefly unavailable — ignore
    }
  },

  showMenu(event, name) {
    event.stopPropagation();
    // Remove any existing menu
    $$(".context-menu").forEach(m => m.remove());

    const menu = document.createElement("div");
    menu.className = "context-menu";
    // Start/Stop live on the row itself now — cleaner menu.
    menu.innerHTML = `
      <div class="context-menu-item" data-action="setactive">★ Set as default</div>
      <div class="context-menu-item" data-action="view">🪪 Edit profile</div>
      <div class="context-menu-divider"></div>
      <div class="context-menu-item danger" data-action="delete">🗑 Delete profile</div>
    `;

    // Position under the button
    const rect = event.target.getBoundingClientRect();
    menu.style.top  = `${rect.bottom + window.scrollY + 4}px`;
    menu.style.left = `${rect.right - 180 + window.scrollX}px`;
    document.body.appendChild(menu);

    // Handlers
    menu.querySelectorAll(".context-menu-item").forEach(item => {
      item.addEventListener("click", async () => {
        const action = item.dataset.action;
        menu.remove();
        if (action === "setactive") await this.setActive(name);
        if (action === "view")      this.viewDetail(name);
        if (action === "delete")    await this.deleteProfile(name);
      });
    });

    // Close on outside click
    setTimeout(() => {
      document.addEventListener("click", () => menu.remove(), { once: true });
    }, 50);
  },

  async setActive(name) {
    try {
      configCache.browser = configCache.browser || {};
      configCache.browser.profile_name = name;
      await api("/api/config", {
        method: "POST",
        body: JSON.stringify(configCache),
      });
      // Keep local state in sync and re-render so the ★ moves
      // to the new default row without waiting for the 2s poll.
      this._defaultProfileName = name;
      this._lastStatusSnapshot = null;    // force next poll to rerender too
      this.renderTable();
      toast(`✓ "${name}" is now the default profile`);
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  viewDetail(name) {
    // Set as active so profile-detail.js picks it up, then navigate
    if (configCache.browser) configCache.browser.profile_name = name;
    navigate("profile");
  },

  async deleteProfile(name) {
    const ok = await confirmDialog({
      title: "Delete profile",
      message:
        `Delete profile "${name}"?\n\n` +
        `This removes the folder on disk AND purges DB records for ` +
        `events, fingerprints, self-checks.\n\n` +
        `Run history is kept. This cannot be undone.`,
      confirmText: "Delete",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
      toast(`✓ Deleted "${name}"`);
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },
};


// ═══════════════════════════════════════════════════════════════
// CreateProfileModal — "✨ Create profile" dialog
// ═══════════════════════════════════════════════════════════════

const CreateProfileModal = {
  async open() {
    const modal = $("#profile-create-modal");
    if (!modal) return;

    // Wire up once
    if (!this._wired) {
      this._wired = true;

      // Close handlers
      modal.querySelectorAll("[data-close]").forEach(el => {
        el.addEventListener("click", () => this.close());
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && modal.style.display !== "none") this.close();
      });

      $("#np-name-random").addEventListener("click", () => {
        $("#np-name").value = this._randomName();
        $("#np-preview-panel").style.display = "none";
      });
      $("#np-preview-btn").addEventListener("click",  () => this.preview());
      $("#np-create-btn").addEventListener("click",   () => this.create());

      // Live preview reset if any field changes
      ["np-name", "np-template", "np-language"].forEach(id => {
        $("#" + id).addEventListener("change", () => {
          $("#np-preview-panel").style.display = "none";
        });
      });

      // Populate template dropdown with rich specs (CPU / RAM / GPU)
      try {
        const templates = await api("/api/profile-templates");
        const sel = $("#np-template");
        for (const t of templates) {
          const opt = document.createElement("option");
          opt.value = t.name;

          // Compose label: "gaming_nvidia_mid — 12c · 16GB · RTX 4060"
          const parts = [];
          if (t.cpu_cores) parts.push(`${t.cpu_cores}c`);
          if (t.ram_gb)    parts.push(`${Math.round(t.ram_gb)} GB`);
          if (t.gpu_model) parts.push(t.gpu_model);
          if (t.screen_w && t.screen_h) {
            // Only show resolution if it's unusual (not the common 1920×1080)
            if (!(t.screen_w === 1920 && t.screen_h === 1080)) {
              parts.push(`${t.screen_w}×${t.screen_h}`);
            }
          }
          if (t.is_laptop) parts.push("laptop");

          opt.textContent = parts.length
            ? `${t.name} — ${parts.join(" · ")}`
            : t.name;

          // Full tooltip on hover (native <option> supports title)
          opt.title = [
            `Template: ${t.name}`,
            t.cpu_cores ? `CPU: ${t.cpu_cores} threads` : "",
            t.ram_gb    ? `RAM: ${t.ram_gb} GB`         : "",
            t.gpu_model ? `GPU: ${t.gpu_model}`         : "",
            (t.screen_w && t.screen_h) ? `Display: ${t.screen_w}×${t.screen_h}` : "",
            t.is_laptop ? "Form factor: laptop (battery emulation on)"
                        : "Form factor: desktop (plugged in)",
          ].filter(Boolean).join("\n");

          sel.appendChild(opt);
        }
      } catch (e) {
        console.warn("Could not load templates", e);
      }
    }

    // Suggest next name
    $("#np-name").value = await this._suggestNextName();
    $("#np-template").value = "auto";
    $("#np-language").value = "uk-UA";
    $("#np-enrich").checked = true;
    $("#np-preview-panel").style.display = "none";
    $("#np-preview-panel").innerHTML = "";

    modal.style.display = "flex";
    setTimeout(() => $("#np-name").focus(), 50);
  },

  close() {
    const modal = $("#profile-create-modal");
    if (modal) modal.style.display = "none";
  },

  async preview() {
    const name     = $("#np-name").value.trim();
    const template = $("#np-template").value;
    const language = $("#np-language").value;

    if (!name) {
      toast("Please enter a profile name first", true);
      $("#np-name").focus();
      return;
    }

    const btn = $("#np-preview-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Generating…";

    try {
      const fp = await api("/api/fingerprint/preview", {
        method: "POST",
        body: JSON.stringify({ name, template, language }),
      });
      this._renderPreview(fp);
    } catch (e) {
      toast("Preview failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "🔮 Preview";
    }
  },

  _renderPreview(fp) {
    const panel = $("#np-preview-panel");
    panel.style.display = "block";
    panel.innerHTML = `
      <div class="fp-row"><span class="fp-label">Template</span><span class="fp-value">${escapeHtml(fp.template || "?")}</span></div>
      <div class="fp-row"><span class="fp-label">Chrome</span><span class="fp-value">${escapeHtml(fp.chrome_version || "?")}</span></div>
      <div class="fp-row"><span class="fp-label">Platform</span><span class="fp-value">${escapeHtml(fp.platform || "?")}</span></div>
      <div class="fp-row"><span class="fp-label">CPU / RAM</span><span class="fp-value">${fp.cpu_cores || "?"} cores · ${fp.ram_gb || "?"} GB</span></div>
      <div class="fp-row"><span class="fp-label">Screen</span><span class="fp-value">${escapeHtml(fp.screen || "?")} @ ${fp.pixel_ratio || "1"}x</span></div>
      <div class="fp-row"><span class="fp-label">GPU</span><span class="fp-value">${escapeHtml((fp.gpu_renderer || "").slice(0, 60))}</span></div>
      <div class="fp-row"><span class="fp-label">Timezone</span><span class="fp-value">${escapeHtml(fp.timezone || "?")}</span></div>
      <div class="fp-row"><span class="fp-label">Languages</span><span class="fp-value">${escapeHtml((fp.languages || []).join(", "))}</span></div>
      <div class="fp-row"><span class="fp-label">Fonts / plugins / WebGL exts</span><span class="fp-value">${fp.fonts_count} / ${fp.plugins_count} / ${fp.webgl_exts}</span></div>
      <div class="fp-row fp-row-ua">
        <span class="fp-label">User-Agent</span>
        <span class="fp-value fp-ua" title="${escapeHtml(fp.user_agent || "")}">${escapeHtml(fp.user_agent || "?")}</span>
      </div>
    `;
  },

  async create() {
    const name     = $("#np-name").value.trim();
    const template = $("#np-template").value;
    const language = $("#np-language").value;
    const enrich   = $("#np-enrich").checked;

    if (!name) {
      toast("Name is required", true);
      $("#np-name").focus();
      return;
    }
    if (!/^[A-Za-z0-9_\-]+$/.test(name)) {
      toast("Invalid name: letters, digits, _ and - only", true);
      return;
    }

    const btn = $("#np-create-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Creating…";

    try {
      const r = await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify({ name, template, language, enrich }),
      });
      if (r.ok) {
        toast(`✓ Created "${name}" (${r.template})`);
        this.close();
        await Profiles.reload();
      } else {
        toast(r.error || "create failed", true);
      }
    } catch (e) {
      toast(e.message || "create failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Create profile";
    }
  },

  // Helpers ────────────────────────────────────────────────────

  _randomName() {
    const adjs = ["swift", "quiet", "crisp", "brave", "lucky", "nimble",
                  "cozy", "bright", "calm", "solid", "sharp", "gentle"];
    const nouns = ["fox", "owl", "wolf", "hawk", "cat", "lynx",
                   "raven", "eagle", "panda", "otter", "seal", "tiger"];
    const a = adjs[Math.floor(Math.random() * adjs.length)];
    const n = nouns[Math.floor(Math.random() * nouns.length)];
    const num = Math.floor(Math.random() * 900) + 100;
    return `${a}_${n}_${num}`;
  },

  async _suggestNextName() {
    try {
      const profiles = await api("/api/profiles");
      let max = 0;
      for (const p of profiles) {
        const m = (p.name || "").match(/^profile_(\d+)$/);
        if (m) max = Math.max(max, parseInt(m[1], 10));
      }
      return `profile_${String(max + 1).padStart(2, "0")}`;
    } catch {
      return "profile_02";
    }
  },
};
