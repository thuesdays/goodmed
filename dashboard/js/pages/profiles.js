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
    { key: "select",      label: "",           default: true  },   // checkbox
    { key: "name",        label: "Name",       default: true  },
    { key: "status",      label: "Status",     default: true  },
    { key: "tags",        label: "Tags",       default: true  },
    { key: "template",    label: "Template",   default: true  },
    { key: "proxy",       label: "Proxy",      default: false },
    { key: "searches24h", label: "Searches 24h", default: true  },
    { key: "captchas24h", label: "Captchas 24h", default: true  },
    { key: "selfcheck",   label: "Self-check", default: true  },
    { key: "fingerprint", label: "Fingerprint", default: false },
    { key: "languages",   label: "Languages",  default: false },
    { key: "lastRun",     label: "Last run",   default: true  },
    { key: "actions",     label: "Actions",    default: true  },
  ],

  visibleColumns: null,
  allProfiles:   [],
  searchFilter:  "",
  tagFilter:     null,     // if set, only rows containing this tag show
  selectedNames: new Set(),   // multi-selection for bulk actions

  async init() {
    // Load visible columns preference from localStorage. The "select"
    // (checkbox) column was added later — older saved preferences don't
    // include it, so users with cached state never see the bulk-action
    // checkboxes. Force-add it back if it's missing. Same forward-compat
    // pattern can rescue any future required column.
    const REQUIRED_COLS = ["select"];
    try {
      const stored = localStorage.getItem("profiles.visible_columns");
      this.visibleColumns = stored
        ? JSON.parse(stored)
        : this.ALL_COLUMNS.filter(c => c.default).map(c => c.key);
    } catch {
      this.visibleColumns = this.ALL_COLUMNS.filter(c => c.default).map(c => c.key);
    }
    // Migrate stored prefs: prepend any required col that's absent.
    let migrated = false;
    for (const col of REQUIRED_COLS) {
      if (!this.visibleColumns.includes(col)) {
        this.visibleColumns.unshift(col);
        migrated = true;
      }
    }
    if (migrated) {
      try {
        localStorage.setItem(
          "profiles.visible_columns",
          JSON.stringify(this.visibleColumns)
        );
      } catch {}
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

    // Tag-filter clear button
    const clearTagBtn = $("#btn-clear-tag-filter");
    if (clearTagBtn) {
      clearTagBtn.addEventListener("click", () => this.clearTagFilter());
    }

    // Bulk toolbar buttons
    $("#btn-bulk-clear")?.addEventListener("click",  () => this.clearSelection());
    $("#btn-bulk-start")?.addEventListener("click",  () => this.bulkStart());
    $("#btn-bulk-stop")?.addEventListener("click",   () => this.bulkStop());
    $("#btn-bulk-delete")?.addEventListener("click", () => this.bulkDelete());
    $("#btn-bulk-tag")?.addEventListener("click",    () => this.bulkTag());
    $("#btn-bulk-group")?.addEventListener("click",  () => this.bulkAddToGroup());

    // Tag editor modal wiring — shared between single-row edit and
    // bulk-tag-add. All modal close buttons use data-close="tag-editor-modal".
    document.querySelectorAll('[data-close="tag-editor-modal"]').forEach(el => {
      el.addEventListener("click", () => this._closeTagEditor());
    });
    $("#tag-editor-add-btn")?.addEventListener("click",
      () => this._tagEditorAdd());
    $("#tag-editor-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this._tagEditorAdd(); }
    });
    $("#tag-editor-save-btn")?.addEventListener("click",
      () => this._tagEditorSave());

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

    // Head — select column gets a "select all" checkbox instead of label
    const thead = $("#profiles-thead");
    thead.innerHTML = "<tr>" + visibleCols.map(c => {
      if (c.key === "select") {
        // Indeterminate when SOME but not ALL visible are selected
        return `<th style="width:28px">
          <input type="checkbox" id="profiles-select-all">
        </th>`;
      }
      return `<th>${escapeHtml(c.label)}</th>`;
    }).join("") + "</tr>";

    // Rows — search + tag filter
    const filtered = this.allProfiles.filter(p => {
      if (this.searchFilter && !p.name.toLowerCase().includes(this.searchFilter)) {
        return false;
      }
      if (this.tagFilter) {
        const tags = Array.isArray(p.tags) ? p.tags : [];
        if (!tags.map(t => t.toLowerCase()).includes(this.tagFilter.toLowerCase())) {
          return false;
        }
      }
      return true;
    });

    const tbody = $("#profiles-tbody");
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="${visibleCols.length}" class="empty-state">
        No profiles found
      </td></tr>`;
      this._updateBulkBar();
      this._updateSelectAllState();
      return;
    }

    tbody.innerHTML = filtered.map(p => "<tr>" +
      visibleCols.map(c => `<td>${this.renderCell(p, c.key)}</td>`).join("") +
      "</tr>").join("");

    // Wire row-level checkboxes — change event flips selectedNames
    // and triggers bulk-bar visibility + header select-all state refresh.
    tbody.querySelectorAll(".profile-select-cb").forEach(cb => {
      cb.addEventListener("change", (e) => {
        const name = e.target.dataset.name;
        if (e.target.checked) this.selectedNames.add(name);
        else                  this.selectedNames.delete(name);
        this._updateBulkBar();
        this._updateSelectAllState();
      });
    });

    // Header "select all visible" — toggles every currently-rendered row
    const selectAll = $("#profiles-select-all");
    if (selectAll) {
      selectAll.addEventListener("change", (e) => {
        const visibleNames = filtered.map(p => p.name);
        if (e.target.checked) {
          visibleNames.forEach(n => this.selectedNames.add(n));
        } else {
          visibleNames.forEach(n => this.selectedNames.delete(n));
        }
        this.renderTable();     // re-render to sync checkboxes
      });
    }

    this._updateBulkBar();
    this._updateSelectAllState();
  },

  /** Sync the header checkbox's state to the currently-rendered rows.
   *  Three states: none / all / indeterminate (some). */
  _updateSelectAllState() {
    const cb = $("#profiles-select-all");
    if (!cb) return;
    const visibleNames = this.allProfiles
      .filter(p =>
        (!this.searchFilter || p.name.toLowerCase().includes(this.searchFilter))
        && (!this.tagFilter
            || (p.tags || []).map(t => t.toLowerCase()).includes(this.tagFilter.toLowerCase()))
      )
      .map(p => p.name);
    const chosen = visibleNames.filter(n => this.selectedNames.has(n));
    cb.checked       = chosen.length > 0 && chosen.length === visibleNames.length;
    cb.indeterminate = chosen.length > 0 && chosen.length <  visibleNames.length;
  },

  /** Show/hide the bulk action bar based on how many profiles are selected. */
  _updateBulkBar() {
    const bar = $("#bulk-bar");
    const cnt = $("#bulk-selected-count");
    if (!bar || !cnt) return;
    const n = this.selectedNames.size;
    bar.style.display = n > 0 ? "" : "none";
    cnt.textContent = String(n);
  },

  renderCell(p, key) {
    switch (key) {
      case "select": {
        const checked = this.selectedNames.has(p.name) ? "checked" : "";
        return `<input type="checkbox" class="profile-select-cb"
                       data-name="${escapeHtml(p.name)}" ${checked}>`;
      }
      case "name":
        return `<strong>${escapeHtml(p.name)}</strong>`;
      case "status":
        return `<span class="pill pill-${p.status}">${p.status}</span>`;
      case "tags": {
        const tags = Array.isArray(p.tags) ? p.tags : [];
        if (!tags.length) {
          return `<span class="tag-add-inline"
                        onclick="Profiles.openTagEditor('${escapeHtml(p.name)}')">
            + add tags
          </span>`;
        }
        const visible = tags.slice(0, 3).map(t =>
          `<span class="profile-tag-chip"
                 onclick="event.stopPropagation(); Profiles.filterByTag('${escapeHtml(t)}')">
             ${escapeHtml(t)}
           </span>`
        ).join("");
        const more = tags.length > 3
          ? `<span class="profile-tag-more">+${tags.length - 3}</span>`
          : "";
        return `<span class="profile-tags"
                      onclick="Profiles.openTagEditor('${escapeHtml(p.name)}')">
          ${visible}${more}
        </span>`;
      }
      case "template":
        return `<span class="muted">${escapeHtml(p.fingerprint?.template || "—")}</span>`;
      case "proxy": {
        const url = p.proxy_url;
        if (!url) return `<span class="muted">global</span>`;
        // Show first 30 chars of the proxy URL so users can distinguish
        // different endpoints at a glance without leaking the whole token
        const short = url.length > 30 ? url.slice(0, 27) + "…" : url;
        return `<code class="proxy-cell" title="${escapeHtml(url)}">${escapeHtml(short)}</code>`;
      }
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
        // If this profile is one of the currently-running slots, show live marker
        const running = this._runningProfiles?.has(p.name);
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
        // Multi-run: each profile is its own slot. Show Stop if THIS
        // profile is running; Start otherwise. No disabling based on
        // other runs — the backend enforces the concurrency cap.
        const runningThis = this._runningProfiles?.has(p.name);
        const isDefault = this._defaultProfileName === p.name;

        const defaultMark = isDefault
          ? `<span class="profile-default-star"
                   title="Default profile — the sidebar Start button launches this">★</span>`
          : "";

        const mainBtn = runningThis
          ? `<button class="profile-row-btn stop"
                     onclick="Profiles.stopThis('${escapeHtml(p.name)}')">
               ■ Stop
             </button>`
          : `<button class="profile-row-btn start"
                     onclick="Profiles.startProfile('${escapeHtml(p.name)}')">
               ▶ Start
             </button>`;

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
    // Multi-run world: each row spawns its own slot via /api/runs with
    // an explicit profile_name. We DO NOT mutate browser.profile_name
    // anymore — that's the "sidebar default", and changing it just
    // because the user clicked a row Start button would surprise them.
    try {
      await api("/api/runs", {
        method: "POST",
        body:   JSON.stringify({ profile_name: name }),
      });
      toast(`✓ Started "${name}"`);
      // Immediately bump status — don't wait for the 2s poll tick so
      // the user sees the Start button flip to Stop right away.
      setTimeout(() => this.refreshRunStatus(), 500);
    } catch (e) {
      toast("Failed to start: " + e.message, true);
    }
  },

  async stopThis(name) {
    // Find the specific run for this profile and stop only that one —
    // legacy stopRun() targets the most-recent active slot, which may
    // not be what the user clicked on when multiple profiles are active.
    try {
      const active = await api("/api/runs/active");
      const slot = (active.runs || []).find(r => r.profile_name === name);
      if (!slot) {
        toast("Not running anymore");
        setTimeout(() => this.refreshRunStatus(), 300);
        return;
      }
      await stopSpecificRun(slot.run_id, name);
      setTimeout(() => this.refreshRunStatus(), 500);
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  /** Pull the latest run status + default profile name, then re-render
   *  the Actions column so Start ↔ Stop reflects reality. Called on a
   *  timer while the user is looking at the Profiles page. */
  async refreshRunStatus() {
    try {
      // Multi-run aware: we need the full list of currently-active
      // profiles, not just "most recent". Each row checks membership
      // in this set.
      const [active] = await Promise.all([
        api("/api/runs/active"),
      ]);
      const runs = active.runs || [];
      this._activeRuns          = runs;
      this._runningProfiles     = new Set(runs.map(r => r.profile_name));
      this._runningProfileToId  = {};
      runs.forEach(r => this._runningProfileToId[r.profile_name] = r.run_id);
      this._defaultProfileName  = configCache?.browser?.profile_name || null;

      // Rebuild only when something meaningful changed — avoid flashing
      // the table every 2s.
      const snapshot = [
        runs.length,
        runs.map(r => `${r.profile_name}#${r.run_id}`).sort().join(","),
        this._defaultProfileName,
      ].join("|");
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
      <div class="context-menu-item" data-action="fingerprint">🧬 Fingerprint…</div>
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
        if (action === "setactive")    await this.setActive(name);
        if (action === "view")         this.viewDetail(name);
        if (action === "fingerprint")  this.openFingerprint(name);
        if (action === "delete")       await this.deleteProfile(name);
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

  // Context-menu shortcut — jumps to the Fingerprint editor pre-scoped
  // to this profile. The editor reads ?profile=… from the hash on init,
  // so setting the hash BEFORE navigate() makes it land on the right one.
  // We don't regenerate here — the editor has three regen modes (full /
  // template-only / reshuffle) and a self-test tab, which is the right
  // place to make that choice rather than silently rolling a new fp.
  openFingerprint(name) {
    location.hash = `#fingerprint?profile=${encodeURIComponent(name)}`;
    navigate("fingerprint");
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

  // ─── BULK ACTIONS ───────────────────────────────────────────

  clearSelection() {
    this.selectedNames.clear();
    this.renderTable();
  },

  /** Start every selected profile. Backend enforces max_parallel —
   *  we just send requests in parallel and let it reject overflows. */
  async bulkStart() {
    const names = Array.from(this.selectedNames);
    if (!names.length) return;

    // Warn if starting more than the configured comfort threshold
    const warnAt = Number(getByPath(configCache, "runner.warn_at_parallel") || 3);
    if (names.length > warnAt) {
      const ok = await confirmDialog({
        title: "Lots of runs",
        message:
          `You're about to start ${names.length} Chrome instances. ` +
          `Each uses roughly 500 MB–1 GB RAM + a CPU core. ` +
          `Backend cap is runner.max_parallel (overflow will be rejected).\n\n` +
          `Continue?`,
        confirmText: "Start all",
      });
      if (!ok) return;
    }

    // Fire requests in parallel — collect outcomes for the toast summary.
    const results = await Promise.allSettled(names.map(n =>
      api("/api/runs", {
        method: "POST",
        body:   JSON.stringify({ profile_name: n }),
      })
    ));
    const ok  = results.filter(r => r.status === "fulfilled").length;
    const err = results.length - ok;
    toast(err
      ? `Started ${ok}/${results.length} — ${err} rejected (cap or already running)`
      : `✓ Started ${ok} run${ok === 1 ? "" : "s"}`,
      err > 0
    );
    setTimeout(() => this.refreshRunStatus(), 600);
  },

  async bulkStop() {
    const names = Array.from(this.selectedNames);
    if (!names.length) return;
    // Find active run_ids matching these profiles
    try {
      const active = await api("/api/runs/active");
      const toStop = (active.runs || []).filter(r => names.includes(r.profile_name));
      if (!toStop.length) {
        toast("None of the selected profiles are currently running");
        return;
      }
      await Promise.allSettled(toStop.map(r =>
        api(`/api/runs/${r.run_id}/stop`, { method: "POST" })
      ));
      toast(`✓ Stopped ${toStop.length} run${toStop.length === 1 ? "" : "s"}`);
      setTimeout(() => this.refreshRunStatus(), 600);
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async bulkDelete() {
    const names = Array.from(this.selectedNames);
    if (!names.length) return;
    const ok = await confirmDialog({
      title: `Delete ${names.length} profile${names.length === 1 ? "" : "s"}`,
      message:
        `This removes folders on disk AND purges DB records for ` +
        `events, fingerprints, self-checks.\n\n` +
        names.slice(0, 10).join(", ") +
        (names.length > 10 ? `, … +${names.length - 10} more` : "") +
        `\n\nRun history is kept. This cannot be undone.`,
      confirmText:  "Delete all",
      confirmStyle: "danger",
    });
    if (!ok) return;
    const results = await Promise.allSettled(names.map(n =>
      api(`/api/profiles/${encodeURIComponent(n)}`, { method: "DELETE" })
    ));
    const done = results.filter(r => r.status === "fulfilled").length;
    toast(`✓ Deleted ${done}/${names.length}`, done < names.length);
    this.selectedNames.clear();
    await this.reload();
  },

  /** Open the tag editor seeded with tags from the first selected
   *  profile, or (if selection is empty) from the given name. On save,
   *  new tags are APPENDED to every selected profile — i.e. bulk-tag
   *  means "add these tags to everyone", not "replace". */
  async bulkTag() {
    const names = Array.from(this.selectedNames);
    if (!names.length) return;
    this._openTagEditorBulk(names);
  },

  async bulkAddToGroup() {
    const names = Array.from(this.selectedNames);
    if (!names.length) return;

    // Load groups list, show a picker modal
    let groups;
    try {
      groups = await api("/api/groups");
    } catch (e) {
      toast("Failed to load groups: " + e.message, true);
      return;
    }

    if (!groups.length) {
      const createNow = await confirmDialog({
        title: "No groups yet",
        message: "You haven't created any profile groups. Go to the Groups page to create one?",
        confirmText: "Open Groups",
      });
      if (createNow) navigate("groups");
      return;
    }

    // Simple prompt-style picker — one group at a time
    const groupList = groups.map((g, i) =>
      `${i + 1}. ${g.name} (${g.member_count} member${g.member_count === 1 ? "" : "s"})`
    ).join("\n");
    const pick = prompt(
      `Add ${names.length} profile${names.length === 1 ? "" : "s"} to which group?\n\n${groupList}\n\nEnter group number:`
    );
    if (!pick) return;
    const idx = parseInt(pick, 10) - 1;
    if (isNaN(idx) || idx < 0 || idx >= groups.length) {
      toast("Invalid selection", true);
      return;
    }
    const group = groups[idx];

    // Merge existing members + new names
    try {
      const full = await api(`/api/groups/${group.id}`);
      const merged = Array.from(new Set([...(full.members || []), ...names]));
      await api(`/api/groups/${group.id}`, {
        method: "POST",
        body:   JSON.stringify({ members: merged }),
      });
      toast(`✓ Added ${names.length} to "${group.name}"`);
      this.selectedNames.clear();
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  // ─── TAG FILTER ─────────────────────────────────────────────

  filterByTag(tag) {
    this.tagFilter = tag;
    const el = $("#profiles-tag-filter");
    if (el) {
      el.style.display = "";
      $("#active-tag-label").textContent = tag;
    }
    this.renderTable();
  },

  clearTagFilter() {
    this.tagFilter = null;
    const el = $("#profiles-tag-filter");
    if (el) el.style.display = "none";
    this.renderTable();
  },

  // ─── TAG EDITOR MODAL ───────────────────────────────────────
  // Two modes:
  //  a) single-profile edit — seeded with that profile's tags,
  //     save REPLACES the list.
  //  b) bulk edit — starts empty, save APPENDS to each selected.

  openTagEditor(name) {
    const profile = this.allProfiles.find(p => p.name === name);
    if (!profile) return;
    this._tagEditorMode      = "single";
    this._tagEditorTargets   = [name];
    this._tagEditorWorkingSet = new Set(profile.tags || []);
    $("#tag-editor-profile-name").textContent = name;
    this._renderTagEditorChips();
    $("#tag-editor-input").value = "";
    $("#tag-editor-modal").style.display = "flex";
    setTimeout(() => $("#tag-editor-input").focus(), 50);
  },

  _openTagEditorBulk(names) {
    this._tagEditorMode      = "bulk";
    this._tagEditorTargets   = names.slice();
    this._tagEditorWorkingSet = new Set();
    $("#tag-editor-profile-name").textContent =
      `${names.length} profile${names.length === 1 ? "" : "s"} (bulk-append)`;
    this._renderTagEditorChips();
    $("#tag-editor-input").value = "";
    $("#tag-editor-modal").style.display = "flex";
    setTimeout(() => $("#tag-editor-input").focus(), 50);
  },

  _closeTagEditor() {
    const m = $("#tag-editor-modal");
    if (m) m.style.display = "none";
  },

  _renderTagEditorChips() {
    const container = $("#tag-editor-chips");
    if (!container) return;
    const tags = Array.from(this._tagEditorWorkingSet || []);
    if (!tags.length) {
      container.innerHTML = `<span class="muted" style="font-size: 12px;">
        No tags yet — add some below.
      </span>`;
      return;
    }
    container.innerHTML = tags.map(t => `
      <span class="profile-tag-chip editor">
        ${escapeHtml(t)}
        <span class="profile-tag-chip-x" data-tag="${escapeHtml(t)}">×</span>
      </span>
    `).join("");
    container.querySelectorAll(".profile-tag-chip-x").forEach(x => {
      x.addEventListener("click", (e) => {
        this._tagEditorWorkingSet.delete(e.target.dataset.tag);
        this._renderTagEditorChips();
      });
    });
  },

  _tagEditorAdd() {
    const inp = $("#tag-editor-input");
    const raw = (inp?.value || "").trim();
    if (!raw) return;
    // Allow comma-separated batch: "a, b, c"
    raw.split(",").forEach(t => {
      const clean = t.trim();
      if (clean) this._tagEditorWorkingSet.add(clean);
    });
    inp.value = "";
    this._renderTagEditorChips();
  },

  async _tagEditorSave() {
    const tags = Array.from(this._tagEditorWorkingSet || []);
    const targets = this._tagEditorTargets || [];
    if (!targets.length) {
      this._closeTagEditor();
      return;
    }

    try {
      if (this._tagEditorMode === "single") {
        // Single-profile edit — REPLACE tag list
        await api(`/api/profiles/${encodeURIComponent(targets[0])}/tags`, {
          method: "POST",
          body:   JSON.stringify({ tags }),
        });
      } else {
        // Bulk edit — APPEND to existing tags on each profile.
        // Do this sequentially to avoid racing the upserts.
        for (const name of targets) {
          const existing = (this.allProfiles.find(p => p.name === name)?.tags) || [];
          const merged = Array.from(new Set([...existing, ...tags]));
          await api(`/api/profiles/${encodeURIComponent(name)}/tags`, {
            method: "POST",
            body:   JSON.stringify({ tags: merged }),
          });
        }
      }
      toast(`✓ Saved tags for ${targets.length} profile${targets.length === 1 ? "" : "s"}`);
      this._closeTagEditor();
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

    // Wire on every open. The router replaces #content's innerHTML on
    // every page navigation, which means the modal DOM (it lives inside
    // profiles.html) is rebuilt fresh each time. A previously-attached
    // listener was on the OLD button element, which is now garbage —
    // hence the user-reported "Create profile click does nothing" bug.
    // We use _replaceWith on each element so re-binding doesn't pile up
    // duplicate handlers across rapid open/close cycles within the same
    // page lifetime.
    const rebind = (id, ev, fn) => {
      const el = document.getElementById(id);
      if (!el) return;
      const clone = el.cloneNode(true);
      el.parentNode.replaceChild(clone, el);
      clone.addEventListener(ev, fn);
    };

    // Close handlers — re-attached fresh
    modal.querySelectorAll("[data-close]").forEach(el => {
      el.addEventListener("click", () => this.close());
    });
    if (!this._escWired) {
      // The Esc-key handler lives on document, which IS persistent
      // across navigations, so only attach it once for the whole
      // dashboard session.
      this._escWired = true;
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && modal.style.display !== "none") this.close();
      });
    }

    rebind("np-name-random", "click", () => {
      $("#np-name").value = this._randomName();
      $("#np-preview-panel").style.display = "none";
    });
    rebind("np-preview-btn", "click", () => this.preview());
    rebind("np-create-btn",  "click", () => this.create());

    // Live preview reset if any field changes
    ["np-name", "np-template", "np-language"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", () => {
        $("#np-preview-panel").style.display = "none";
      });
    });

    // Populate template dropdown — only once per page-load. Re-running it
    // would append duplicate <option> entries, since rebind() above
    // doesn't recreate the <select>.
    if (!this._templatesLoaded) {
      this._templatesLoaded = true;
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
    const name      = $("#np-name").value.trim();
    const template  = $("#np-template").value;
    const language  = $("#np-language").value;
    const enrich    = $("#np-enrich").checked;
    const openAfter = $("#np-open-after")?.checked ?? true;
    const proxyUrl  = ($("#np-proxy")?.value || "").trim();

    if (!name) {
      toast("Name is required", true);
      $("#np-name").focus();
      return;
    }
    if (!/^[A-Za-z0-9_\-]+$/.test(name)) {
      toast("Invalid name: letters, digits, _ and - only", true);
      return;
    }
    // Light client-side validation of the proxy URL — server does the
    // authoritative check. This catches the most common typos before we
    // round-trip a creation.
    if (proxyUrl && !/^(https?|socks5):\/\//i.test(proxyUrl)) {
      toast("Proxy URL must start with http://, https:// or socks5://", true);
      $("#np-proxy").focus();
      return;
    }

    const btn = $("#np-create-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Creating…";

    try {
      const r = await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify({ name, template, language, enrich,
                               proxy_url: proxyUrl || undefined }),
      });
      if (!r.ok) {
        toast(r.error || "create failed", true);
        return;
      }

      toast(`✓ Created "${name}" (${r.template})`);
      this.close();

      if (openAfter) {
        // Set this as the active profile for the Edit Profile page,
        // then navigate. profile-detail.js reads configCache.browser.profile_name
        // on init to pick which profile to populate.
        if (typeof configCache !== "undefined" && configCache) {
          configCache.browser = configCache.browser || {};
          configCache.browser.profile_name = name;
        }
        location.hash = `#profile?name=${encodeURIComponent(name)}`;
        if (typeof navigate === "function") navigate("profile");
      } else {
        await Profiles.reload();
      }
    } catch (e) {
      toast(e.message || "create failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Create profile";
    }
  },

  // Helpers ───────────────────────────────────────────────────────────

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
