// ═══════════════════════════════════════════════════════════════
// pages/proxy.js — proxy library (dense-table IDE aesthetic)
//
// Main view is a table of proxies with inline Test buttons. Each row
// shows cached diagnostics (flag, city, IP type, latency) so the user
// sees status at a glance. Add/Edit/Delete via modals, bulk-import
// accepts a paste of many URLs.
//
// Global options (auto-rotate, geo lock, self-check cadence) live in
// a collapsible section at the bottom — they apply to every run
// regardless of which proxy is assigned.
// ═══════════════════════════════════════════════════════════════

const ProxyPage = {
  proxies: [],
  filtered: [],
  editingId: null,     // null = Add; number = Edit
  _search: "",

  async init() {
    await this.loadProxies();
    this.wireHeader();
    this.wireSearch();
    this.wireEditModal();
    this.wireBulkModal();

    // Config bindings for global options — reuse the generic helper
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));
  },

  teardown() { /* no timers */ },

  // ── Loading ──────────────────────────────────────────────────

  async loadProxies() {
    try {
      const resp = await api("/api/proxies");
      this.proxies = resp.proxies || [];
      this._applyFilter();
      this.render();
    } catch (e) {
      console.error("load proxies:", e);
      toast("Failed to load proxies", true);
    }
  },

  _applyFilter() {
    const q = this._search.trim().toLowerCase();
    if (!q) { this.filtered = this.proxies.slice(); return; }
    this.filtered = this.proxies.filter(p => {
      const hay = [
        p.name, p.host, p.port, p.login, p.type,
        p.last_country, p.last_city, p.last_provider, p.last_ip_type,
      ].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
  },

  // ── Rendering ────────────────────────────────────────────────

  render() {
    const tbody = $("#proxy-tbody");
    if (!this.proxies.length) {
      tbody.innerHTML = `
        <tr><td colspan="12" class="proxy-empty-cell">
          No proxies yet. Click <strong>+ Add proxy</strong> above.
        </td></tr>`;
      this._renderStats();
      return;
    }
    if (!this.filtered.length) {
      tbody.innerHTML = `
        <tr><td colspan="12" class="proxy-empty-cell">
          No matches for "${escapeHtml(this._search)}".
        </td></tr>`;
      this._renderStats();
      return;
    }

    tbody.innerHTML = this.filtered.map(p => this._renderRow(p)).join("");
    this._wireRowButtons();
    this._renderStats();
  },

  _renderStats() {
    const all = this.proxies;
    $("#stat-total").textContent     = all.length;
    $("#stat-active").textContent    = all.filter(p => p.last_status === "ok").length;
    $("#stat-error").textContent     = all.filter(p => p.last_status === "error").length;
    $("#stat-untested").textContent  = all.filter(p =>
      !p.last_status || p.last_status === "untested").length;
  },

  _renderRow(p) {
    const status = p.last_status || "untested";
    const statusLabel = status === "ok" ? "ACTIVE"
                      : status === "error" ? "ERROR"
                      : "UNTESTED";
    const type = (p.type || "http").toUpperCase();
    const loginShown = p.login ? escapeHtml(p.login) : `<span class="muted">—</span>`;
    const flag = p.last_country_code
      ? this._flagEmoji(p.last_country_code)
      : "🌐";
    const geoLabel = p.last_country
      ? `${flag} ${escapeHtml(p.last_country)}${p.last_city ? ` · ${escapeHtml(p.last_city)}` : ""}`
      : `<span class="muted">—</span>`;
    const ipType = p.last_ip_type && p.last_ip_type !== "unknown"
      ? `<span class="iptype-badge iptype-${p.last_ip_type}">${escapeHtml(p.last_ip_type)}</span>`
      : `<span class="muted">—</span>`;
    const latency = p.last_latency_ms != null
      ? `<span class="latency ${this._latencyClass(p.last_latency_ms)}">${p.last_latency_ms}ms</span>`
      : `<span class="muted">—</span>`;
    const checkedAt = p.last_checked_at
      ? this._formatRelative(p.last_checked_at)
      : `<span class="muted">never</span>`;
    const name = p.name || `${p.host || "?"}:${p.port || "?"}`;

    return `
      <tr data-proxy-id="${p.id}" class="proxy-row proxy-row-${status}">
        <td class="col-name">
          <div class="proxy-name-cell">
            <span class="proxy-name-label">${escapeHtml(name)}</span>
            ${p.is_default ? `<span class="proxy-default-badge">DEFAULT</span>` : ""}
            ${p.is_rotating ? `<span class="proxy-rot-badge" title="Rotation API configured">↻</span>` : ""}
          </div>
        </td>
        <td class="col-status">
          <span class="status-badge status-${status}">${statusLabel}</span>
        </td>
        <td class="col-type">
          <span class="type-badge type-${(p.type || 'http').toLowerCase()}">${type}</span>
        </td>
        <td class="col-host" title="${escapeHtml(p.host || '')}">${escapeHtml(p.host || "—")}</td>
        <td class="col-port">${p.port ?? "—"}</td>
        <td class="col-login">${loginShown}</td>
        <td class="col-geo">${geoLabel}</td>
        <td class="col-iptype">${ipType}</td>
        <td class="col-latency">${latency}</td>
        <td class="col-profiles">
          <span class="profile-count-badge" title="${p.profile_count} profile(s) using this">
            ${p.profile_count || 0}
          </span>
        </td>
        <td class="col-checked">${checkedAt}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button class="btn-icon row-test-btn" title="Test this proxy"
                    data-action="test">🔬</button>
            <button class="btn-icon row-edit-btn" title="Edit"
                    data-action="edit">✎</button>
            <button class="btn-icon row-delete-btn" title="Delete"
                    data-action="delete"
                    ${p.is_default ? "disabled" : ""}>✕</button>
          </div>
        </td>
      </tr>
      ${p.last_error && status === "error" ? `
        <tr class="proxy-error-row">
          <td colspan="12">
            <div class="proxy-error-detail">
              <strong>Error:</strong> ${escapeHtml(p.last_error)}
            </div>
          </td>
        </tr>` : ""}`;
  },

  _wireRowButtons() {
    const tbody = $("#proxy-tbody");
    tbody.querySelectorAll(".row-actions button").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const row = btn.closest("tr");
        const id = Number(row.dataset.proxyId);
        const action = btn.dataset.action;
        if (action === "test") this.testOne(id, btn);
        else if (action === "edit") this.openEditModal(id);
        else if (action === "delete") this.deleteProxy(id);
      });
    });
  },

  // ── Header wiring ────────────────────────────────────────────

  wireHeader() {
    $("#proxy-add-btn").addEventListener("click", () => this.openEditModal(null));
    $("#proxy-bulk-import-btn").addEventListener("click", () => this.openBulkModal());
    $("#proxy-test-all-btn").addEventListener("click", () => this.testAll());
  },

  wireSearch() {
    $("#proxy-search").addEventListener("input", (e) => {
      this._search = e.target.value;
      this._applyFilter();
      this.render();
    });
  },

  // ── Test one row ─────────────────────────────────────────────

  async testOne(id, btn) {
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-testing");
      btn.innerHTML = `<span class="spinner">⟳</span>`;
    }
    try {
      const resp = await api(`/api/proxies/${id}/test`, { method: "POST" });
      const diag = resp.diag || {};
      if (diag.ok) {
        toast(`✓ ${diag.country || "Connected"} · ${diag.latency_ms}ms`);
      } else {
        toast(`✗ ${diag.error || "Test failed"}`, true);
      }
      await this.loadProxies();
    } catch (e) {
      toast(`Test failed: ${e.message}`, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("is-testing");
        btn.innerHTML = "🔬";
      }
    }
  },

  // ── Test all ─────────────────────────────────────────────────

  async testAll() {
    const btn = $("#proxy-test-all-btn");
    if (!this.proxies.length) {
      toast("No proxies to test", true);
      return;
    }
    if (!confirm(`Test all ${this.proxies.length} proxies? Takes up to ${this.proxies.length * 5}s.`)) return;
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = `⏳ Testing…`;
    try {
      const resp = await api("/api/proxies/test-all", { method: "POST" });
      const okCount = (resp.results || []).filter(r => r.status === "ok").length;
      toast(`✓ Tested ${resp.count}: ${okCount} ok, ${resp.count - okCount} errors`);
      await this.loadProxies();
    } catch (e) {
      toast(`Bulk test failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  },

  // ── Edit modal ───────────────────────────────────────────────

  wireEditModal() {
    document.querySelectorAll('[data-close="proxy-edit-modal"]').forEach(el =>
      el.addEventListener("click", () => this.closeEditModal())
    );
    $("#edit-proxy-is-rotating").addEventListener("change", (e) => {
      $("#edit-rotation-fields").style.display = e.target.checked ? "" : "none";
    });
    $("#proxy-edit-save-btn").addEventListener("click", () => this.saveProxy());
  },

  async openEditModal(id) {
    this.editingId = id;
    const modal = $("#proxy-edit-modal");
    const title = $("#proxy-edit-title");
    if (id) {
      title.textContent = "Edit proxy";
      try {
        const resp = await api(`/api/proxies/${id}`);
        const p = resp.proxy;
        $("#edit-proxy-name").value        = p.name || "";
        $("#edit-proxy-url").value         = p.url || "";
        $("#edit-proxy-is-rotating").checked = !!p.is_rotating;
        $("#edit-rotation-url").value      = p.rotation_api_url || "";
        $("#edit-rotation-provider").value = p.rotation_provider || "none";
        $("#edit-rotation-key").value      = p.rotation_api_key || "";
        $("#edit-proxy-is-default").checked = !!p.is_default;
        $("#edit-proxy-notes").value       = p.notes || "";
        // Auto-test disabled by default on edit — avoid extra request
        // unless user opts in
        $("#edit-proxy-auto-test").checked = false;
        $("#edit-rotation-fields").style.display = p.is_rotating ? "" : "none";
      } catch (e) {
        toast("Could not load proxy", true);
        return;
      }
    } else {
      title.textContent = "Add proxy";
      $("#edit-proxy-name").value = "";
      $("#edit-proxy-url").value = "";
      $("#edit-proxy-is-rotating").checked = false;
      $("#edit-rotation-url").value = "";
      $("#edit-rotation-provider").value = "none";
      $("#edit-rotation-key").value = "";
      $("#edit-proxy-is-default").checked = false;
      $("#edit-proxy-notes").value = "";
      $("#edit-proxy-auto-test").checked = true;
      $("#edit-rotation-fields").style.display = "none";
    }
    modal.style.display = "";
    setTimeout(() => $("#edit-proxy-url").focus(), 30);
  },

  closeEditModal() {
    $("#proxy-edit-modal").style.display = "none";
    this.editingId = null;
  },

  async saveProxy() {
    const url = $("#edit-proxy-url").value.trim();
    if (!url) { toast("URL is required", true); return; }
    const payload = {
      name:             $("#edit-proxy-name").value.trim() || null,
      url,
      is_rotating:      $("#edit-proxy-is-rotating").checked,
      rotation_api_url: $("#edit-rotation-url").value.trim() || null,
      rotation_provider: $("#edit-rotation-provider").value || null,
      rotation_api_key: $("#edit-rotation-key").value.trim() || null,
      is_default:       $("#edit-proxy-is-default").checked,
      notes:            $("#edit-proxy-notes").value.trim() || null,
      auto_test:        $("#edit-proxy-auto-test").checked,
    };
    const btn = $("#proxy-edit-save-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Saving…";
    try {
      if (this.editingId) {
        // PUT (partial update) — don't send auto_test since only create
        // supports that side-effect path
        const { auto_test, ...updateFields } = payload;
        await api(`/api/proxies/${this.editingId}`, {
          method: "PUT",
          body: JSON.stringify(updateFields),
        });
        // If user wants test after edit, run it separately
        if (auto_test) {
          await api(`/api/proxies/${this.editingId}/test`, { method: "POST" });
        }
        toast("✓ Saved");
      } else {
        await api("/api/proxies", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        toast("✓ Created");
      }
      this.closeEditModal();
      await this.loadProxies();
    } catch (e) {
      toast(`Save failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Save";
    }
  },

  // ── Delete ───────────────────────────────────────────────────

  async deleteProxy(id) {
    const p = this.proxies.find(x => x.id === id);
    if (!p) return;
    if (p.is_default) {
      toast("Cannot delete the default proxy — set another as default first.", true);
      return;
    }
    const used = p.profile_count || 0;
    const warn = used > 0
      ? `\n\n⚠ ${used} profile(s) use this proxy. They will fall back to the default.`
      : "";
    if (!confirm(`Delete "${p.name}"?${warn}`)) return;
    try {
      await api(`/api/proxies/${id}`, { method: "DELETE" });
      toast("✓ Deleted");
      await this.loadProxies();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, true);
    }
  },

  // ── Bulk import modal (smart multi-format parser) ───────────
  //
  // UX: user pastes → we debounce → POST /api/proxies/parse-preview
  // → show per-line parse result. Import button enables only when
  // there's at least one valid NEW (non-duplicate) entry. This
  // matches Dolphin's workflow — see what you'll create before you
  // commit.

  wireBulkModal() {
    document.querySelectorAll('[data-close="proxy-bulk-modal"]').forEach(el =>
      el.addEventListener("click", () => this.closeBulkModal())
    );
    const ta = $("#bulk-proxy-text");
    const scheme = $("#bulk-default-scheme");
    // Debounce preview — parser is fast server-side but we don't want
    // a request per keystroke when someone is pasting 200 lines.
    ta.addEventListener("input", () => this._schedulePreview());
    scheme.addEventListener("change", () => this._schedulePreview());
    $("#proxy-bulk-save-btn").addEventListener("click", () => this.bulkImport());
  },

  openBulkModal() {
    $("#proxy-bulk-modal").style.display = "";
    $("#bulk-proxy-text").value = "";
    $("#bulk-preview-stats").textContent = "Paste lines to see preview";
    $("#bulk-preview-list").innerHTML = `
      <div class="bulk-preview-empty">
        Start typing on the left — the parser detects the format
        automatically.
      </div>`;
    $("#proxy-bulk-save-btn").disabled = true;
    this._bulkPreviewData = null;
    setTimeout(() => $("#bulk-proxy-text").focus(), 30);
  },

  closeBulkModal() {
    $("#proxy-bulk-modal").style.display = "none";
    if (this._previewTimer) clearTimeout(this._previewTimer);
  },

  _schedulePreview() {
    if (this._previewTimer) clearTimeout(this._previewTimer);
    this._previewTimer = setTimeout(() => this._fetchPreview(), 400);
  },

  async _fetchPreview() {
    const text = $("#bulk-proxy-text").value;
    if (!text.trim()) {
      $("#bulk-preview-stats").textContent = "Paste lines to see preview";
      $("#bulk-preview-list").innerHTML = `
        <div class="bulk-preview-empty">
          Start typing on the left — the parser detects the format
          automatically.
        </div>`;
      $("#proxy-bulk-save-btn").disabled = true;
      this._bulkPreviewData = null;
      return;
    }
    try {
      const resp = await api("/api/proxies/parse-preview", {
        method: "POST",
        body: JSON.stringify({
          text,
          default_scheme: $("#bulk-default-scheme").value,
        }),
      });
      this._bulkPreviewData = resp;
      this._renderPreview(resp);
    } catch (e) {
      $("#bulk-preview-list").innerHTML = `
        <div class="bulk-preview-empty" style="color: #fca5a5;">
          Preview failed: ${escapeHtml(e.message)}
        </div>`;
    }
  },

  _renderPreview(data) {
    const { valid = [], errors = [], total = 0 } = data;
    const newCount = valid.filter(v => !v.duplicate).length;
    const dupCount = valid.length - newCount;

    // Stats line (above preview list)
    const statsHtml = [];
    if (newCount) {
      statsHtml.push(`<strong class="stat-ok">${newCount}</strong> new`);
    }
    if (dupCount) {
      statsHtml.push(`<strong class="stat-dup">${dupCount}</strong> duplicate`);
    }
    if (errors.length) {
      statsHtml.push(`<strong class="stat-err">${errors.length}</strong> error`);
    }
    if (!statsHtml.length) {
      statsHtml.push("No recognized lines");
    }
    $("#bulk-preview-stats").innerHTML = statsHtml.join(" · ");

    // List: valid entries first, then errors at the bottom
    const rows = [];

    for (const v of valid) {
      const credsChip = (v.login || v.password)
        ? `<span class="prev-chip prev-chip-creds">${escapeHtml(v.login || '')}${v.password ? ':•••' : ''}</span>`
        : "";
      const fmtLabel = this._formatLabel(v.format);
      rows.push(`
        <div class="bulk-preview-row ${v.duplicate ? 'is-duplicate' : 'is-new'}">
          <div class="prev-mark">${v.duplicate ? '↺' : '+'}</div>
          <div class="prev-body">
            <div class="prev-url">
              <code>${escapeHtml(v.url)}</code>
            </div>
            <div class="prev-meta">
              <span class="prev-chip prev-chip-type">${escapeHtml(v.type || '')}</span>
              <span class="prev-chip prev-chip-host">${escapeHtml(v.host || '')}:${v.port || ''}</span>
              ${credsChip}
              <span class="prev-chip prev-chip-fmt" title="Detected format: ${escapeHtml(v.format)}">${escapeHtml(fmtLabel)}</span>
              ${v.duplicate ? '<span class="prev-chip prev-chip-dup">already exists</span>' : ''}
            </div>
          </div>
        </div>`);
    }

    for (const e of errors) {
      rows.push(`
        <div class="bulk-preview-row is-error">
          <div class="prev-mark">✕</div>
          <div class="prev-body">
            <div class="prev-url prev-url-error">
              <code>${escapeHtml(e.raw || '').slice(0, 120)}</code>
            </div>
            <div class="prev-meta">
              <span class="prev-chip prev-chip-error">line ${e.line}: ${escapeHtml((e.error || '').slice(0, 80))}</span>
            </div>
          </div>
        </div>`);
    }

    if (!rows.length) {
      $("#bulk-preview-list").innerHTML = `
        <div class="bulk-preview-empty">
          Nothing parsed from the input.
        </div>`;
      $("#proxy-bulk-save-btn").disabled = true;
      return;
    }
    $("#bulk-preview-list").innerHTML = rows.join("");

    // Enable import button if at least 1 new entry
    $("#proxy-bulk-save-btn").disabled = newCount === 0;
    $("#proxy-bulk-save-btn").textContent = newCount > 0
      ? `Import ${newCount}`
      : "Nothing to import";
  },

  /** Human-friendly label for the detected format */
  _formatLabel(fmt) {
    const map = {
      canonical:              "URL",
      host_port:              "host:port",
      host_port_user_pass:    "host:port:user:pass",
      host_port_user_pass_ambiguous: "host:port:user:pass?",
      user_pass_host_port:    "user:pass:host:port",
      creds_at_host_port:     "user:pass@host:port",
      host_port_at_creds:     "host:port@user:pass",
      ipv6_colon:             "IPv6",
      host_port_user:         "host:port:user",
    };
    return map[fmt] || fmt;
  },

  async bulkImport() {
    const text = $("#bulk-proxy-text").value;
    if (!text.trim()) return;
    const autoTest = $("#bulk-auto-test").checked;
    const btn = $("#proxy-bulk-save-btn");
    btn.disabled = true;
    btn.textContent = `⏳ Importing…`;
    try {
      const resp = await api("/api/proxies/bulk-import", {
        method: "POST",
        body: JSON.stringify({
          text,
          default_scheme: $("#bulk-default-scheme").value,
          auto_test: autoTest,
        }),
      });
      // Compose a compact summary for the toast
      const parts = [`✓ Created ${resp.created}`];
      if (resp.skipped_duplicates) parts.push(`${resp.skipped_duplicates} dup`);
      if (resp.parse_errors) parts.push(`${resp.parse_errors} error`);
      toast(parts.join(", "));
      this.closeBulkModal();
      await this.loadProxies();
    } catch (e) {
      toast(`Import failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Import";
    }
  },

  // ── Helpers ──────────────────────────────────────────────────

  /** Two-letter country code → flag emoji.
   *  ISO 3166-1 alpha-2 codes start at regional indicator 'A' = U+1F1E6. */
  _flagEmoji(cc) {
    if (!cc || cc.length !== 2) return "🌐";
    const up = cc.toUpperCase();
    const A = 0x1F1E6;
    return String.fromCodePoint(
      A + (up.charCodeAt(0) - 65),
      A + (up.charCodeAt(1) - 65)
    );
  },

  _latencyClass(ms) {
    if (ms < 300) return "latency-fast";
    if (ms < 1000) return "latency-mid";
    return "latency-slow";
  },

  _formatRelative(ts) {
    try {
      const dt = new Date(ts.replace(" ", "T") + "Z");
      const diff = (Date.now() - dt.getTime()) / 1000;
      if (diff < 60) return "just now";
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d ago`;
      return dt.toLocaleDateString(undefined,
        { month: "short", day: "numeric" });
    } catch { return ts; }
  },
};

// Shorthand alias — do NOT name this `Proxy` (that's the built-in
// JS constructor used by Chart.js internals; shadowing it globally
// breaks unrelated pages).
const ProxyLibrary = ProxyPage;
