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
    { key: "lastRun",     label: "Last run",   default: false },
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
      case "lastRun":
        return `<span class="muted">—</span>`;
      case "actions":
        return `
          <div class="profile-row-actions">
            <button class="profile-row-btn start"
                    onclick="Profiles.startProfile('${escapeHtml(p.name)}')">▶ Start</button>
            <button class="profile-menu-btn"
                    onclick="Profiles.showMenu(event, '${escapeHtml(p.name)}')">⋮</button>
          </div>
        `;
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
    } catch (e) {
      toast("Failed to start: " + e.message, true);
    }
  },

  showMenu(event, name) {
    event.stopPropagation();
    // Remove any existing menu
    $$(".context-menu").forEach(m => m.remove());

    const menu = document.createElement("div");
    menu.className = "context-menu";
    menu.innerHTML = `
      <div class="context-menu-item" data-action="start">▶ Start monitoring</div>
      <div class="context-menu-item" data-action="stop">■ Stop if running</div>
      <div class="context-menu-divider"></div>
      <div class="context-menu-item" data-action="setactive">★ Set as active profile</div>
      <div class="context-menu-item" data-action="view">🪪 View details</div>
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
        if (action === "start")     await this.startProfile(name);
        if (action === "stop")      await stopRun();
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
      toast(`✓ "${name}" is now active`);
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

      // Populate template dropdown
      try {
        const templates = await api("/api/profile-templates");
        const sel = $("#np-template");
        for (const t of templates) {
          const opt = document.createElement("option");
          opt.value = t.name;
          opt.textContent = `${t.name} — ${t.platform || "?"}`;
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
